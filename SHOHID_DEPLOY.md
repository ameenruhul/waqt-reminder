# Message to Shohid

Hi Shohid,

Please deploy the attached **Waqt Reminder** Python application on our existing Bangladesh server using Coolify. A separate small application/container within the existing Coolify project is preferable to merging it into another app's runtime.

- Build pack: Dockerfile (repo root), port 8080, one replica.
- CPU limit: 1 vCPU; RAM limit: 512 MB (verify actual usage after deployment).
- Persistent storage: `/data` (UID/GID 10001), SQLite database; enable backups.
- Assign an HTTPS subdomain and set `PUBLIC_URL` to it.
- Configure `.env.example` as runtime environment variables, generating a strong `ADMIN_PASSWORD` and a long random `NTFY_TOPIC`. Do not commit secrets. Send me the URL, password and topic privately.
- Use `NTFY_URL=https://ntfy.sh`, default generic notification mode, and the Manchester/Europe-London/Hanafi configuration for now. The calculation method is provisional until we match my mosque's timetable.
- Check `/health`, logs, and verify the dashboard loads. I will subscribe on the ntfy iOS app and test the notification from the dashboard.

No Codex, LLM API, GPU, Redis or separate database server is required. Please follow README.md for full deployment and security instructions. Thanks!
