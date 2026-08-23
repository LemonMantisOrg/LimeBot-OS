# 24/7 process supervision

LimeBot is a single-process asyncio daemon. Jobs survive a crash only when
they are written to `data/jobs.sqlite` *before* the agent runs. Supervision
is what brings the process back so those jobs can be claimed again.

## Heartbeat

`GET /api/live` returns `{ "status": "live" }` as soon as the web channel is
up. It does not wait for skills, MCP, or the LLM.

```bash
curl -fsS http://127.0.0.1:8000/api/live
```

Use that URL from systemd `OnFailure` units, Docker health checks, or an
external monitor. Do not treat `/api/ready` as the liveness signal; readiness
can stay degraded when optional MCP servers fail.

## systemd (recommended on a Linux host)

User session (survives logout if lingering is enabled):

```bash
limebot autorun enable
# or:
mkdir -p ~/.config/systemd/user
cp deploy/systemd/limebot.user.service ~/.config/systemd/user/limebot.service
# edit WorkingDirectory / ExecStart to your checkout
systemctl --user daemon-reload
systemctl --user enable --now limebot
loginctl enable-linger "$USER"
```

System-wide:

```bash
sudo cp deploy/systemd/limebot.service /etc/systemd/system/limebot.service
sudo systemctl edit limebot   # set WorkingDirectory and ExecStart
sudo systemctl enable --now limebot
```

`bin/gateway.sh` is the documented start path. It stays in the foreground and
execs `npm run lime-bot start -- --quick`.

## Docker

`docker-compose.yml` already sets `restart: unless-stopped` on backend and
frontend. Combined with `/api/live` this is the container equivalent of the
systemd unit.

```bash
docker compose up --build -d
docker compose ps
curl -fsS http://127.0.0.1:3000/api/live
```

## Unattended scheduled work

Live chat stays confirmation-gated (`APPROVAL_POLICY_PROFILE=manual`).
Scheduled and queued jobs use a separate allowlist:

```env
UNATTENDED_PATH_ALLOWLIST=./persona,./temp,./logs
UNATTENDED_COMMAND_ALLOWLIST=python,gh
```

Do not set `AUTONOMOUS_MODE=true` just to make cron finish. That switch
bypasses confirmation for every channel.
