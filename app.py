"""Waqt reminder: single-user, dependency-free Python service."""
import os, re, json, csv, sqlite3, time, threading, logging, secrets, hmac, hashlib, base64, html
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlsplit, parse_qs, quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager

UTC = timezone.utc
NAMES = ('Fajr','Dhuhr','Asr','Maghrib','Isha')
FIELDS = ('Fajr','Sunrise','Dhuhr','Asr','Maghrib','Isha')
LOCK = threading.RLock()
LOG = logging.getLogger('waqt')
BANGLA = dict(zip(NAMES, ('ফজর','যোহর','আসর','মাগরিব','এশা')))

def env(k, default=''): return os.environ.get(k, default)
def now_utc(): return datetime.now(UTC)
def ts(d): return int(d.timestamp())
def read_time(day, value, zone):
    match = re.fullmatch(r'(\d{2}):(\d{2})(?:\s*\([^)]*\))?', str(value).strip())
    if not match: raise ValueError('Invalid timetable time')
    return datetime.combine(day, datetime.min.time(), zone).replace(hour=int(match[1]), minute=int(match[2]))

def prayer_windows(day, timings, next_timings, zone, isha_end='fajr'):
    t = {k: read_time(day,timings[k],zone) for k in FIELDS}
    if any(ts(t[a]) >= ts(t[b]) for a,b in zip(FIELDS,FIELDS[1:])): raise ValueError('Non-increasing timetable')
    next_fajr = read_time(day+timedelta(days=1),next_timings['Fajr'],zone)
    end = next_fajr
    if isha_end == 'midnight': end = datetime.fromtimestamp((ts(t['Maghrib'])+ts(next_fajr))//2,zone)
    if ts(end) <= ts(t['Isha']): raise ValueError('Isha cutoff earlier than Isha')
    return [(p, ts(t[p]), ts(e)) for p,e in zip(NAMES,(t['Sunrise'],t['Asr'],t['Maghrib'],t['Isha'],end))]

def schedule(start,end):
    # Merge closely spaced notifications. The final warning is normally 5 minutes before expiry.
    span=end-start
    candidates=[(start-300,'pre'),(start,'start'),(start+span//2,'mid'),(end-1800,'warning'),(end-300,'final')]
    selected=[(start-300,'pre'),(start,'start')]
    for at,kind in sorted(candidates[2:], key=lambda x: (x[0], x[1])):
        if at>start and at<end and at-selected[-1][0]>=180:
            selected.append((at,kind))
    return selected

class Service:
    def __init__(self, db_path=None):
        self.db_path=Path(db_path or env('DATA_DIR','./data')+'/waqt.sqlite3')
        self.db_path.parent.mkdir(parents=True,exist_ok=True)
        self.zone=ZoneInfo(env('PRAYER_TIMEZONE','Europe/London'))
        self.city=env('PRAYER_CITY','Manchester'); self.country=env('PRAYER_COUNTRY','GB')
        self.school=env('PRAYER_SCHOOL','1'); self.method=env('PRAYER_METHOD','3')
        self.isha_end=env('ISHA_END','fajr'); self.csv_path=env('MOSQUE_CSV','')
        self.base=env('PUBLIC_URL').rstrip('/'); self.ntfy=env('NTFY_URL','https://ntfy.sh').rstrip('/')
        self.topic=env('NTFY_TOPIC'); self.token=env('NTFY_TOKEN'); self.password=env('ADMIN_PASSWORD')
        self.lang=env('LANGUAGE','bn'); self.private=env('PRIVATE_MESSAGES','1')=='1'
        self.repeat=int(env('REPEAT_MINUTES','30')); self.limit=int(env('DAILY_LIMIT','80'))
        self.csrf=hmac.new(self.password.encode(),b'waqt-csrf-v1',hashlib.sha256).hexdigest()
        self.last_tick=0; self.error=''; self.next_refresh=0; self.push_retry_at=0
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS timetable(day TEXT PRIMARY KEY, timings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prayers(id TEXT PRIMARY KEY, day TEXT, name TEXT, start INTEGER, end INTEGER, status TEXT DEFAULT 'pending', snooze INTEGER DEFAULT 0, last_sent INTEGER DEFAULT 0, sent INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs(pid TEXT, kind TEXT, due INTEGER, state TEXT DEFAULT 'pending', PRIMARY KEY(pid,kind));
CREATE TABLE IF NOT EXISTS deliveries(at INTEGER);''')
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.db_path,timeout=15); c.row_factory=sqlite3.Row
        try:
            yield c; c.commit()
        except Exception:
            c.rollback(); raise
        finally: c.close()
    def validate(self):
        if len(self.password)<16: raise ValueError('ADMIN_PASSWORD must have at least 16 characters')
        if not re.fullmatch(r'[A-Za-z0-9_-]{24,64}',self.topic): raise ValueError('NTFY_TOPIC must be a random 24–64-character topic')
        for url in (self.base,self.ntfy):
            u=urlsplit(url)
            if u.scheme!='https' or not u.netloc or u.query or u.fragment or u.username or u.password or u.path: raise ValueError('PUBLIC_URL and NTFY_URL must be HTTPS origins without paths')
        if self.school not in ('0','1') or self.isha_end not in ('fajr','midnight') or self.lang not in ('bn','en') or not 10<=self.repeat<=120 or not 10<=self.limit<=200: raise ValueError('Invalid settings')
    def fetch(self,day):
        if self.csv_path:
            with open(self.csv_path,encoding='utf-8-sig',newline='') as f:
                for row in csv.DictReader(f):
                    if row['date']==day.isoformat(): return {k:row[k] for k in FIELDS}
            raise ValueError('Mosque CSV missing '+day.isoformat())
        params=urlencode(dict(city=self.city,country=self.country,school=self.school,method=self.method,timezonestring=self.zone.key))
        url='https://api.aladhan.com/v1/timingsByCity/'+day.strftime('%d-%m-%Y')+'?'+params
        with urlopen(Request(url,headers={'User-Agent':'WaqtReminder/1.0'}),timeout=12) as r: obj=json.load(r)
        if obj.get('code')!=200: raise ValueError('Prayer API error')
        data=obj['data']; reported=data.get('date',{}).get('gregorian',{}).get('date')
        if reported!=day.strftime('%d-%m-%Y'): raise ValueError('API returned wrong date')
        return {k:data['timings'][k] for k in FIELDS}
    def refresh(self, moment):
        today=moment.astimezone(self.zone).date()
        # Keep previous day for Isha crossing midnight and next day for pre-Fajr.
        days=[today+timedelta(days=i) for i in (-1,0,1,2)]
        # Cache by settings fingerprint so config changes don't silently reuse other city data.
        key=hashlib.sha256(repr((self.city,self.country,self.school,self.method,self.zone.key,self.csv_path,self.isha_end)).encode()).hexdigest()[:16]
        data={}
        for d in days:
            cachekey=key+':'+d.isoformat()
            with self.db() as c: row=c.execute('SELECT timings FROM timetable WHERE day=?',(cachekey,)).fetchone()
            if row and not self.csv_path: data[d]=json.loads(row[0])
            else:
                data[d]=self.fetch(d)
                for k in FIELDS: read_time(d,data[d][k],self.zone)
                with self.db() as c: c.execute('INSERT OR REPLACE INTO timetable VALUES (?,?)',(cachekey,json.dumps(data[d])))
        with LOCK,self.db() as c:
            for d in days[:-1]:
                for name,start,end in prayer_windows(d,data[d],data[d+timedelta(days=1)],self.zone,self.isha_end):
                    pid=d.isoformat()+'-'+name
                    old=c.execute('SELECT * FROM prayers WHERE id=?',(pid,)).fetchone()
                    c.execute('INSERT INTO prayers(id,day,name,start,end) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET start=excluded.start,end=excluded.end',(pid,d.isoformat(),name,start,end))
                    if old and (old['start']!=start or old['end']!=end): c.execute("DELETE FROM jobs WHERE pid=? AND state='pending'",(pid,))
                    for at,kind in schedule(start,end): c.execute('INSERT OR IGNORE INTO jobs(pid,kind,due) VALUES(?,?,?)',(pid,kind,at))
                    c.execute('INSERT OR IGNORE INTO jobs(pid,kind,due) VALUES(?,?,?)',(pid,'check',end))
            c.execute('DELETE FROM deliveries WHERE at<?',(ts(moment)-172800,))
    def fmt(self, at): return datetime.fromtimestamp(at,self.zone).strftime('%H:%M')
    def message(self,p,kind,at):
        url=self.base+'/prayer/'+quote(p['id']); name=BANGLA[p['name']] if self.lang=='bn' else p['name']
        if self.private:
            url=self.base+'/'
            title='Waqt'; message='সময় হয়েছে। স্ট্যাটাস দেখুন।' if self.lang=='bn' else 'Time for your reminder. Open to respond.'
        elif self.lang=='bn':
            title=name+' — '+('ওয়াক্ত শেষ' if kind=='check' else 'মনে করিয়ে দেওয়া')
            message=(f'ওয়াক্ত {self.fmt(p["start"])}–{self.fmt(p["end"])}। '+('নামাজ পড়া হয়েছিল?' if kind=='check' else ('ওয়াক্ত শুরু হতে ৫ মিনিট বাকি।' if kind=='pre' else f'বাকি {max(0,(p["end"]-at+59)//60)} মিনিট। নামাজ পড়েছ?')))
        else:
            title=name+' reminder'; message=f'Window {self.fmt(p["start"])}–{self.fmt(p["end"])}. '+('Did you pray?' if kind=='check' else ('Starts in 5 minutes.' if kind=='pre' else f'{max(0,(p["end"]-at+59)//60)} min left.'))
        return dict(topic=self.topic,title=title,message=message,priority=4 if kind in ('final','warning') else 3,click=url,actions=[dict(action='view',label='Open / respond',url=url)])
    def publish(self,payload):
        headers={'Content-Type':'application/json'}
        if self.token: headers['Authorization']='Bearer '+self.token
        req=Request(self.ntfy+'/',data=json.dumps(payload,ensure_ascii=False).encode(),headers=headers,method='POST')
        with urlopen(req,timeout=10) as r:
            if r.status!=200: raise RuntimeError('ntfy non-200 response')
    def tick(self,moment=None):
        moment=moment or now_utc(); at=ts(moment)
        if at>=self.next_refresh:
            try: self.refresh(moment); self.next_refresh=at+1800; self.error=''
            except Exception as e: self.next_refresh=at+180; self.error='Timetable refresh failed; cached schedule only'; LOG.warning('Timetable refresh failed: %s',type(e).__name__)
        with LOCK,self.db() as c:
            c.execute("UPDATE prayers SET status='unconfirmed',snooze=0 WHERE status='pending' AND end<=?",(at,))
            c.execute("UPDATE jobs SET state='skipped' WHERE state='pending' AND due<? AND kind!='check'",(at-180,))
            c.execute("UPDATE jobs SET state='skipped' WHERE state='pending' AND due<? AND kind='check'",(at-1800,))
            count=c.execute('SELECT count(*) FROM deliveries WHERE at>?',(at-86400,)).fetchone()[0]
            rows=c.execute("SELECT * FROM prayers WHERE (status='pending' AND start-300<=? AND end>?) OR (status='unconfirmed' AND end>? AND end<=?) ORDER BY start",(at,at,at-1800,at)).fetchall()
            for p in rows:
                if count>=self.limit or at<self.push_retry_at: break
                pid=p['id']; kind=None
                jobs=c.execute("SELECT * FROM jobs WHERE pid=? AND state='pending' AND due<=? ORDER BY due DESC",(pid,at)).fetchall()
                if p['status']=='unconfirmed':
                    if any(j['kind']=='check' for j in jobs): kind='check'
                elif p['snooze']>at: continue
                elif p['snooze'] and p['snooze']<=at: kind='snooze'
                elif jobs: kind=jobs[0]['kind']
                elif p['start']<=at and at-p['last_sent']>=self.repeat*60 and at-p['start']>=180 and p['sent']<12: kind='repeat'
                if not kind: continue
                if kind not in ('check','final','snooze','start') and p['last_sent'] and at-p['last_sent']<120: continue
                if kind=='repeat' and p['sent']>=12: continue
                try: self.publish(self.message(p,kind,at))
                except Exception as e: LOG.warning('Push failed: %s',type(e).__name__); self.push_retry_at=at+120; break
                count+=1; c.execute('INSERT INTO deliveries VALUES (?)',(at,))
                c.execute('UPDATE prayers SET last_sent=?,sent=sent+1,snooze=0 WHERE id=?',(at,pid))
                c.execute("UPDATE jobs SET state='sent' WHERE pid=? AND kind=? AND state='pending'",(pid,kind))
                c.execute("UPDATE jobs SET state='skipped' WHERE pid=? AND due<=? AND state='pending'",(pid,at))
        self.last_tick=at
    def action(self,pid,action,value='',at=None):
        at=at if at is not None else ts(now_utc())
        with LOCK,self.db() as c:
            p=c.execute('SELECT * FROM prayers WHERE id=?',(pid,)).fetchone()
            if not p: raise ValueError('Unknown prayer')
            if action=='prayed':
                if at<p['start']: raise ValueError('Prayer window has not opened')
                status='prayed' if at<p['end'] else 'prayed_unconfirmed_time'
                if p['status'] in ('prayed','prayed_unconfirmed_time','qada'): return
            elif action=='on_time':
                if at<p['end']: raise ValueError('Window still open')
                status='prayed'
            elif action=='missed':
                if at<p['end']: raise ValueError('Window still open')
                status='missed'
            elif action=='qada':
                if at<p['end']: raise ValueError('Window still open')
                status='qada'
            elif action in ('snooze','custom'):
                if p['status']!='pending' or at>=p['end']: raise ValueError('Prayer is not pending')
                if action=='snooze':
                    mins=int(value)
                    if not 1<=mins<=180: raise ValueError('Choose 1–180 minutes')
                    due=at+mins*60
                else:
                    local=datetime.fromtimestamp(at,self.zone)
                    target=read_time(local.date(),value,self.zone)
                    if ts(target)<=at: target=read_time(local.date()+timedelta(days=1),value,self.zone)
                    due=ts(target)
                if not max(at,p['start'])<due<p['end']: raise ValueError('Choose a future time inside the prayer window')
                due=min(due,p['end']-300) if at<p['end']-300 else due
                if due<=at: raise ValueError('Too close to end of window')
                c.execute('UPDATE prayers SET snooze=? WHERE id=?',(due,pid)); return
            else: raise ValueError('Invalid action')
            c.execute('UPDATE prayers SET status=?,snooze=0 WHERE id=?',(status,pid))
            c.execute("UPDATE jobs SET state='skipped' WHERE pid=? AND state='pending'",(pid,))

CSS='''body{font:16px system-ui,sans-serif;background:#f5f4ef;color:#172b29;margin:0}main{max-width:720px;margin:auto;padding:24px 16px 48px}h1{font-size:32px;margin:0}h2{font-size:20px;margin-top:28px}p{line-height:1.5}.sub{color:#536965}.card{background:white;border:1px solid #d8dfd9;border-radius:16px;padding:18px;margin:12px 0}.row{display:flex;justify-content:space-between;align-items:center;gap:12px}.status{color:#38645a;font-size:14px}.buttons{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}button,input{font:inherit;padding:11px 14px;border-radius:9px;border:1px solid #becbc3}button{background:#164c40;color:#fff;cursor:pointer}button.alt{background:white;color:#164c40}a{color:#164c40}small{color:#536965}form{margin:0}.alert{color:#8c410d}.big{font-size:24px;font-weight:600}.foot{margin-top:25px;font-size:13px}'''
def esc(s): return html.escape(str(s),quote=True)
def page(title,body): return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+'</title><style>'+CSS+'</style><main>'+body+'</main></html>').encode()

def handler(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass # Avoid logging private URLs or auth headers.
        def send(self,code,body=b'',headers=None):
            self.send_response(code); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer'); self.send_header('X-Frame-Options','DENY'); self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            for k,v in (headers or {}).items(): self.send_header(k,v)
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def authorized(self):
            expected='Basic '+base64.b64encode(('admin:'+service.password).encode()).decode()
            if hmac.compare_digest(self.headers.get('Authorization',''),expected): return True
            self.send(401,b'Authentication required',{'WWW-Authenticate':'Basic realm="Waqt", charset="UTF-8"'}); return False
        def form(self,pid,action,label,value=None,extra='',alt=False):
            return '<form method="post" action="/prayer/'+esc(pid)+'"><input type="hidden" name="csrf" value="'+service.csrf+'"><input type="hidden" name="action" value="'+esc(action)+'">'+('' if value is None else '<input type="hidden" name="value" value="'+esc(value)+'">')+extra+'<button'+(' class="alt"' if alt else '')+'>'+esc(label)+'</button></form>'
        def get_prayer(self,pid):
            with service.db() as c: return c.execute('SELECT * FROM prayers WHERE id=?',(pid,)).fetchone()
        def do_GET(self):
            path=urlsplit(self.path).path
            if path=='/health':
                healthy=service.last_tick and ts(now_utc())-service.last_tick<180
                return self.send(200 if healthy else 503,b'ok' if healthy else b'starting or stale')
            if not self.authorized(): return
            at=ts(now_utc()); today=now_utc().astimezone(service.zone).date().isoformat()
            if path=='/':
                with service.db() as c: rows=c.execute('SELECT * FROM prayers WHERE day=? OR (day<? AND end>?) ORDER BY start',(today,today,at)).fetchall()
                body='<h1>Waqt <span class="sub">/ ওয়াক্ত</span></h1><p class="sub">'+esc(service.city)+' · '+esc(service.zone.key)+' · '+today+'</p>'
                if service.error: body+='<p class="alert">'+esc(service.error)+'</p>'
                body+='<p><small>Source: '+('Mosque CSV' if service.csv_path else 'AlAdhan calculated timetable')+' · '+('Hanafi' if service.school=='1' else 'Standard Asr')+' · Isha cutoff: '+esc(service.isha_end)+'</small></p>'
                for p in rows:
                    status=p['status']; status='upcoming' if status=='pending' and at<p['start'] else status
                    body+='<section class="card"><div class="row"><div><div class="big">'+esc(p['name'])+' · '+BANGLA[p['name']]+'</div><small>'+service.fmt(p['start'])+' – '+service.fmt(p['end'])+' · '+esc(p['day'])+'</small></div><div class="status">'+esc(status)+'</div></div><p><a href="/prayer/'+esc(p['id'])+'">Open / respond →</a></p></section>'
                if not rows: body+='<p>No timetable yet. Check settings and logs.</p>'
                body+='<h2>Notification setup</h2><p>Subscribe in ntfy to the configured server and topic provided privately by your administrator.</p><form method="post" action="/test"><input type="hidden" name="csrf" value="'+service.csrf+'"><button>Send test notification</button></form><p class="foot">Unconfirmed is not the same as missed. Verify prayer windows with your mosque.</p>'
                return self.send(200,page('Waqt',body))
            if path.startswith('/prayer/'):
                pid=path.split('/')[-1]; p=self.get_prayer(pid)
                if not p: return self.send(404,page('Not found','<p>Unknown prayer.</p>'))
                body='<p><a href="/">← Today</a></p><h1>'+esc(p['name'])+' · '+BANGLA[p['name']]+'</h1><p>'+esc(p['day'])+' · '+service.fmt(p['start'])+'–'+service.fmt(p['end'])+'</p><div class="card"><div class="big">'+esc(p['status'])+'</div>'
                if p['snooze']>at: body+='<p>Next reminder: '+service.fmt(p['snooze'])+'</p>'
                if at<p['start']: body+='<p>Prayer has not started.</p>'
                elif at<p['end'] and p['status']=='pending':
                    body+='<p>'+str((p['end']-at+59)//60)+' minutes remaining.</p><div class="buttons">'+self.form(pid,'prayed','✓ পড়েছি / Prayed')+self.form(pid,'snooze','10 min','10',alt=True)+self.form(pid,'snooze','20 min','20',alt=True)+self.form(pid,'snooze','30 min','30',alt=True)+'</div><h2>Custom time</h2>'+self.form(pid,'custom','Remind me',extra='<input type="time" name="value" required aria-label="Reminder time"> ')
                elif at>=p['end']:
                    body+='<p>Window ended. Record what actually happened.</p><div class="buttons">'+self.form(pid,'on_time','Prayed in time')+self.form(pid,'missed','Missed',alt=True)+self.form(pid,'qada','Qada completed',alt=True)+'</div>'
                else: body+='<p>Reminders stopped.</p>'
                return self.send(200,page('Prayer',body+'</div>'))
            return self.send(404,b'Not found')
        def do_POST(self):
            if not self.authorized(): return
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=4096: raise ValueError('Invalid request size')
                if self.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded': raise ValueError('Invalid content type')
                f={k:v[0] for k,v in parse_qs(self.rfile.read(n).decode(),max_num_fields=6).items()}
                if not hmac.compare_digest(f.get('csrf',''),service.csrf): return self.send(403,b'Forbidden')
                path=urlsplit(self.path).path
                if path=='/test':
                    with LOCK,service.db() as c:
                        at=ts(now_utc()); count=c.execute('SELECT count(*) FROM deliveries WHERE at>?',(at-86400,)).fetchone()[0]
                        if count>=service.limit: raise ValueError('Daily cap reached')
                        service.publish(dict(topic=service.topic,title='Waqt test',message='Notifications are working.',click=service.base+'/')); c.execute('INSERT INTO deliveries VALUES (?)',(at,))
                    return self.send(303,b'',{'Location':'/'})
                if not re.fullmatch(r'/prayer/\d{4}-\d{2}-\d{2}-(?:Fajr|Dhuhr|Asr|Maghrib|Isha)',path): return self.send(404,b'Not found')
                service.action(path.split('/')[-1],f.get('action',''),f.get('value',''))
                return self.send(303,b'',{'Location':path})
            except ValueError as e: return self.send(400,page('Invalid request','<p class="alert">'+esc(e)+'</p><p><a href="/">Back</a></p>'))
            except Exception: return self.send(503,page('Unavailable','<p>Request failed. Try again.</p><p><a href="/">Back</a></p>'))
    return Handler

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    s=Service(); s.validate()
    def loop():
        while True:
            try: s.tick()
            except Exception as e: LOG.error('Scheduler failure: %s',type(e).__name__)
            time.sleep(15)
    threading.Thread(target=loop,daemon=True).start()
    LOG.info('Waqt listening on port 8080')
    ThreadingHTTPServer(('0.0.0.0',8080),handler(s)).serve_forever()
if __name__=='__main__': main()
