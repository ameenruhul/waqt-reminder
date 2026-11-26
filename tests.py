import unittest, tempfile, os, json, base64, threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo
from app import Service, prayer_windows, schedule, ts, handler

T={'Fajr':'05:47','Sunrise':'07:54','Dhuhr':'11:56','Asr':'14:04','Maghrib':'16:01','Isha':'17:31'}
D=date(2026,11,26)
class Test(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'PRAYER_TIMEZONE':'Europe/London','PUBLIC_URL':'https://example.org','NTFY_URL':'https://ntfy.sh','NTFY_TOPIC':'x'*32,'ADMIN_PASSWORD':'a'*24,'PRIVATE_MESSAGES':'1','ISHA_END':'fajr','REPEAT_MINUTES':'30','DAILY_LIMIT':'80','MOSQUE_CSV':'','LANGUAGE':'bn'})
        self.env.start(); self.s=Service(Path(self.temp.name)/'db.sqlite3'); self.s.validate()
        self.s.fetch=lambda d:T.copy(); self.sent=[]; self.s.publish=lambda p:self.sent.append(p)
        self.s.refresh(datetime(2026,11,26,11,0,tzinfo=timezone.utc)); self.s.next_refresh=10**12
        self.pid='2026-11-26-Dhuhr'; self.start=ts(datetime(2026,11,26,11,56,tzinfo=timezone.utc)); self.end=ts(datetime(2026,11,26,14,4,tzinfo=timezone.utc))
    def tearDown(self): self.env.stop(); self.temp.cleanup()
    def tick(self,t): self.s.tick(datetime.fromtimestamp(t,timezone.utc))
    def row(self):
        with self.s.db() as c: return dict(c.execute('SELECT * FROM prayers WHERE id=?',(self.pid,)).fetchone())
    def test_windows(self):
        windows=prayer_windows(D,T,T,ZoneInfo('Europe/London'))
        self.assertEqual(windows[1],('Dhuhr',self.start,self.end)); self.assertEqual(windows[-1][2],ts(datetime(2026,11,27,5,47,tzinfo=timezone.utc)))
        mid=prayer_windows(D,T,T,ZoneInfo('Europe/London'),'midnight')[-1][2]; self.assertLess(mid,windows[-1][2])
    def test_dst(self):
        w=prayer_windows(date(2027,6,1),T,T,ZoneInfo('Europe/London'))
        self.assertEqual(datetime.fromtimestamp(w[0][1],timezone.utc).hour,4)
    def test_short_window(self):
        jobs=schedule(10000,10600); self.assertTrue(all(t<10600 for t,k in jobs)); self.assertEqual(jobs[0],(9700,'pre'))
    def test_start_and_complete(self):
        self.tick(self.start-300); self.tick(self.start); self.assertEqual(self.row()['sent'],2)
        self.s.action(self.pid,'prayed',at=self.start+60); self.tick(self.start+3600); self.assertEqual(self.row()['sent'],2)
        self.assertEqual(self.row()['status'],'prayed')
    def test_snooze(self):
        self.tick(self.start); self.s.action(self.pid,'snooze','20',at=self.start+60); self.tick(self.start+600); self.assertEqual(self.row()['sent'],1)
        self.tick(self.start+1260); self.assertEqual(self.row()['sent'],2)
        with self.assertRaises(ValueError): self.s.action(self.pid,'snooze','180',at=self.start+1300)
    def test_custom(self):
        self.s.action(self.pid,'custom','12:30',at=self.start+60); self.assertEqual(self.row()['snooze'],ts(datetime(2026,11,26,12,30,tzinfo=timezone.utc)))
    def test_unconfirmed(self):
        self.tick(self.end); self.assertEqual(self.row()['status'],'unconfirmed'); self.s.action(self.pid,'on_time',at=self.end+60); self.assertEqual(self.row()['status'],'prayed')
    def test_restart(self):
        self.tick(self.start); self.s.action(self.pid,'prayed',at=self.start+1); other=Service(self.s.db_path); other.fetch=lambda d:T.copy(); other.publish=lambda p:self.fail('unexpected repeat for Dhuhr') if 'Dhuhr' in p['message'] else None
        other.refresh(datetime.fromtimestamp(self.start,timezone.utc)); self.assertEqual(self.row()['status'],'prayed')
    def test_privacy(self):
        self.tick(self.start); self.assertEqual(self.sent[-1]['click'],'https://example.org/'); self.assertNotIn('Dhuhr',json.dumps(self.sent[-1]))
    def test_dashboard_auth_and_csrf(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.s)); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start(); url='http://127.0.0.1:'+str(server.server_port)
        auth='Basic '+base64.b64encode(('admin:'+self.s.password).encode()).decode()
        try:
            with self.assertRaises(HTTPError) as e: urlopen(url+'/'); self.assertEqual(e.exception.code,401)
            with urlopen(Request(url+'/',headers={'Authorization':auth})) as r: self.assertIn(b'Waqt',r.read())
            with self.assertRaises(HTTPError) as e: urlopen(Request(url+'/test',data=b'csrf=invalid',headers={'Authorization':auth,'Content-Type':'application/x-www-form-urlencoded'})); self.assertEqual(e.exception.code,403)
        finally: server.shutdown(); server.server_close(); thread.join()
if __name__=='__main__': unittest.main(verbosity=2)
