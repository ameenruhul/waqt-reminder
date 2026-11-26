# Waqt / ওয়াক্ত — personal Salah reminder

A lightweight, single-user web app and notification scheduler for ntfy on iOS and Android. No AI, mobile app development, database server, or IFTTT subscription. Python 3.12 standard library only; SQLite stores the timetable, notification jobs, snoozes and prayer confirmations.

## Quick deployment: Coolify (Shohid)

1. Extract the ZIP and push its contents to a **private Git repository**, without `.env` or real secrets. In the existing Coolify project/environment add a **new Application** from that repo. Use **Dockerfile** build pack, base directory `/`, Dockerfile `/Dockerfile` (or `Dockerfile`, depending on the UI), exposed port **8080**. One replica, no host port mapping. Do not merge the process into an existing app container. It can share the same server and Coolify project.
2. Assign an HTTPS domain such as `https://salah.example.com` and point DNS at the server. Set `PUBLIC_URL` to that exact origin, without trailing slash. Keep TLS enabled. Do not expose the direct container port publicly.
3. Set runtime environment variables from `.env.example`. Generate the two independent secrets locally:
   ```sh
   python3 -c 'import secrets; print(secrets.token_urlsafe(32)); print(secrets.token_hex(20))'
   ```
   First output = `ADMIN_PASSWORD`; second output = `NTFY_TOPIC`. Never commit or publicly share these. Share the topic/password privately with the owner. Default username: `admin`.
4. Add a **persistent volume** mounted at `/data`. The service runs as UID/GID `10001:10001`. If a bind mount is used, provision the directory with that ownership and restrictive permissions first. Back up the SQLite database using SQLite backup tooling or stop the service before copying it.
5. Resource Limits: CPU `1`, memory `512m`; single replica. These are proposed limits, not measured production usage. The app uses one small Python process with an internal scheduler. Make sure the host has spare capacity.
6. Health check HTTP GET `/health`, port 8080; allow 90 seconds or longer for the initial external API calls. Deploy and check the logs. `/health` means the scheduler is ticking, not that a notification was delivered. The dashboard warns if timetable fetching fails. Avoid running two replicas even during upgrades: duplicate pushes are possible; use stop/start if necessary.
7. Open the HTTPS domain on your phone, log in with `admin` and the password, and subscribe in the official ntfy app to the `NTFY_URL` server and exact `NTFY_TOPIC`. Tap **Send test notification** from the dashboard. Test locked phone, sound, Focus settings, network, and the action opening the dashboard. Bookmark the page on the home screen if desired.
8. Verify the displayed prayer times and cutoffs against the actual selected local mosque before relying on reminders. Start with a real mosque CSV if exact mosque times are essential. No live MAWAQIT connection is included.

Coolify references: https://coolify.io/docs/applications/builds/dockerfile ; https://coolify.io/docs/applications/configuration/persistent-storage ; https://coolify.io/docs/applications/configuration/resource-limits

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PUBLIC_URL` | required | Public HTTPS origin, no path |
| `ADMIN_PASSWORD` | required | At least 16 chars, preferably random 32+ |
| `NTFY_TOPIC` | required | Random 24–64 alphanumeric/underscore/hyphen chars |
| `NTFY_URL` | `https://ntfy.sh` | HTTPS origin for hosted or self-hosted ntfy |
| `NTFY_TOKEN` | empty | Optional ntfy access token; required if configured private server uses token auth |
| `PRIVATE_MESSAGES` | `1` | Generic notification with dashboard link; `0` includes prayer name, timings and direct prayer-page link. Neither mode sends credentials or confirmation history to ntfy. Generic mode still exposes domain, timestamps and a generic reminder to ntfy. |
| `PRAYER_CITY`, `PRAYER_COUNTRY` | `Manchester`, `GB` | AlAdhan location |
| `PRAYER_TIMEZONE` | `Europe/London` | IANA timezone; UK daylight saving handled via zoneinfo |
| `PRAYER_SCHOOL` | `1` | Hanafi Asr; `0` Standard Asr (Shafi'i etc.) |
| `PRAYER_METHOD` | `3` | Muslim World League; **provisional**, match mosque method |
| `ISHA_END` | `fajr` | Operational reminder cutoff: next Fajr, or `midnight` (midpoint from Maghrib to next Fajr). Verify religious interpretation with your mosque. |
| `LANGUAGE` | `bn` | Bengali notifications, `en` for English; dashboard bilingual labels |
| `REPEAT_MINUTES` | `30` | Repeat interval while pending, 10–120 |
| `DAILY_LIMIT` | `80` | Maximum successful pushes in rolling 24h, including test pushes, 10–200 |
| `MOSQUE_CSV` | empty | Optional local authoritative timetable file; no calculated fallback if configured CSV is missing/invalid |
| `DATA_DIR` | `/data` in Docker | Persistent database location |

Restart/redeploy to apply environment settings. Changing timing settings mid-day preserves recorded prayer status and previously delivered jobs for that date. Review changes before relying on that day's schedule.

### Mosque data

Default is [AlAdhan](https://aladhan.com/prayer-times-api) daily timings API, cached in SQLite. This is a calculated timetable, **not** MAWAQIT and not verified against a particular Manchester mosque. For exact mosque data, obtain an authorized timetable and write a CSV into the mounted data directory, set `MOSQUE_CSV=/data/mosque.csv`. Header: `date,Fajr,Sunrise,Dhuhr,Asr,Maghrib,Isha`. Dates ISO YYYY-MM-DD; times 24-hour HH:MM in configured local timezone. Supply continuous dates covering yesterday, today and the next two days (preferably a full year plus boundary dates). `mosque.example.csv` is illustrative, not validated religious data; do not use it as a real timetable. Use prayer START times, not iqamah/jamaat times. The service rereads CSV every 30 minutes. An approved MAWAQIT provider adapter could be added later; do not scrape or use private endpoints without permission.

### Reminder policy

- Five minutes before start, at start, midpoint, 30 minutes before end, and five minutes before end. Closely spaced reminders may be combined. Repeats every 30 minutes (configurable) until confirmed, with a cap on extra repeats per prayer and a daily cap. High priority for final/deadline warnings. Notification delivery depends on internet, iOS/Android settings, provider uptime and device policy.
- Open notification (or its action link) and mark **Prayed**. This stops subsequent reminders. Previously delivered messages cannot be recalled.
- Snooze 10, 20 or 30 minutes, or enter a custom local time. Requests beyond the window are rejected; a requested reminder may move to five minutes before the end to preserve a warning. The confirmation page displays the actual next reminder. While snoozed, regular reminders are suppressed. No snoozes after the window closes.
- At expiry, unconfirmed prayer becomes `unconfirmed`, not automatically `missed`. Record `Prayed in time`, `Missed`, or `Qada completed` yourself. This is a personal log, not a religious ruling. No separate prohibited-time or Jumu'ah scheduler in this MVP.
- Fajr ends at sunrise, Dhuhr at selected Asr, Asr at Maghrib, Maghrib at Isha. Isha uses selected operational cutoff. These are simplified windows, not a full jurisprudential model.
- On restart the scheduler resumes from SQLite. Very stale jobs are skipped to avoid notification floods; active windows can resume repeats. Failed push calls retry after a short backoff. Delivery is best effort: network timeouts/crashes can duplicate or lose alerts. There is no offline local alarm fallback or delivery receipt. Use an independent alarm for critical prayers.

### Security and privacy

The default free ntfy.sh unreserved topic is public to anyone who knows/guesses it. A random name reduces discoverability but is not authentication. Default generic notifications and no credentials in push payloads reduce exposure. For personal prayer-specific notification text, prefer a genuinely access-controlled ntfy instance/topic and set `PRIVATE_MESSAGES=0`; do not put secrets into notification action URLs. Self-hosted ntfy on iOS needs upstream APNs relay configuration for prompt push delivery (see https://docs.ntfy.sh/config/). The web dashboard uses HTTPS + HTTP Basic auth and CSRF-protected POST forms. Do not enable plain public HTTP. Keep secrets outside the repository, rotate them if exposed, and configure rate limiting at the reverse proxy for repeated authentication failures. SQLite is not encrypted at rest: protect backups, filesystem and server access. This single-user MVP does not have multi-user registration or an administrative user management system.

### Optional local / Compose setup

`cp .env.example .env` and edit secrets. `docker compose up -d --build`. Compose binds only localhost; use a separate HTTPS reverse proxy with the configured public origin. On Coolify use the Dockerfile steps above instead of exposing a host port. No Redis, Postgres, Node or external package install needed.

### Tests

`python3 tests.py` runs mocked timetable/push tests plus a local HTTP authentication/CSRF test. This package was tested in Python, not deployed on a live Coolify instance or real iPhone. Real AlAdhan availability, ntfy delivery, Docker image build, limits and mosque timetable still need deployment smoke tests. Use the dashboard's test notification and check several days of timings before daily use.

### Future (not included)

Authorized MAWAQIT integration, Google Calendar backup sync, native notification direct HTTP actions with safe authentication, history browsing/export, WhatsApp Cloud API, travel profiles, and per-prayer settings. This version intentionally uses a secure dashboard link instead of exposing mutation credentials in notification payloads.
