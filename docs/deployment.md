# Linux VPS deployment

Use a dedicated working directory for the property. These examples use `~/host-helper`; substitute your own Linux account and hostname. Run commands from that directory because `.env`, OAuth files, and `open_manager.db` are resolved there.

## Install

Install Python 3.11 or newer, its `venv` support, and Git using your distribution's package manager. Then:

```bash
git clone https://github.com/Satoshilab21/host-helper.git ~/host-helper
cd ~/host-helper
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
cp .env.example .env
```

Set your private feed URL and property timezone in `.env`. Place the Desktop OAuth client JSON at `credentials.json` as described in the [README](../README.md). Align the server's local timezone with the property timezone; inspect it with `timedatectl` and change it with `sudo timedatectl set-timezone YOUR_IANA_TIMEZONE` if needed.

## Authorize a headless server

From your laptop, open an SSH session with port forwarding:

```bash
ssh -L 8080:localhost:8080 YOUR_USER@YOUR_VPS_HOST
```

In that session, on the VPS:

```bash
cd ~/host-helper
./.venv/bin/python -m host_helper authorize
```

Open each printed authorization URL in your laptop's browser and authorize the intended Google account. The local callback reaches the server through the tunnel. The helper runs the three authorization flows separately and writes `token.json`, `token_gmail.json`, and `token_tasks.json`.

To replace only one token:

```bash
./.venv/bin/python -m host_helper authorize token_tasks.json
```

If port 8080 is occupied, forward a different port and set `AUTH_PORT` to the same value when invoking authorization. Tokens must be renewed when Google revokes access or a refresh token expires. Google documents additional [refresh-token expiration conditions](https://developers.google.com/identity/protocols/oauth2#expiration), including restrictions for apps in testing mode.

Protect the local deployment files:

```bash
chmod 600 .env credentials.json token.json token_gmail.json token_tasks.json
```

## Test a run

```bash
cd ~/host-helper
./.venv/bin/python -m host_helper preview calendar
./.venv/bin/python -m host_helper sync
```

The calendar preview reads your feed and Gmail and updates local booking state. The full sync writes actual Google events and tasks. Inspect the result before scheduling it. Keep command output private because it can include guest details and API error information.

## Schedule with cron

Run `crontab -e` for the same Linux account that owns the working directory. Add a daily entry using an absolute path:

```cron
0 5 * * * cd /home/YOUR_USER/host-helper && umask 077 && /home/YOUR_USER/host-helper/.venv/bin/python -m host_helper sync >> /home/YOUR_USER/host-helper/run.log 2>&1
```

This runs at 05:00 in the cron service's configured timezone and records both output streams. Check `crontab -l` and your server timezone. Use only one scheduled job per state directory and remove the previous Open Manager cron entry when migrating.

Optional `HEALTHCHECK_URL` monitoring signals `/start`, `/fail`, and success. Configure the monitoring service for the same schedule with enough grace time for normal sync duration. Missing heartbeats also detect a disabled cron job or an unavailable server.

## Logs and updates

```bash
cd ~/host-helper
tail -n 150 run.log
tail -f run.log
```

A Google quota error may leave optional duplicate cleanup pending; later runs resume it. Other failed operations return a failing exit status. When both sync stages succeed, Host Helper attempts to clear previous Calendar failure alerts. Look in the log if cleanup itself cannot reach Google.

Rotate `run.log` using your server's log rotation tooling and restrict access to rotated copies. Keep encrypted/private backups of `open_manager.db`, `.env`, and OAuth files outside Git. Stop scheduled and manual runs before taking a database backup so no active SQLite journal is omitted.

For an update, wait for the current run to finish, then:

```bash
cd ~/host-helper
git pull --ff-only
./.venv/bin/python -m pip install -e .
./.venv/bin/python -m host_helper sync
```

Keep private deployment files in place. If authorization fails, repeat the SSH-tunnel procedure for the affected token instead of trying to authorize from cron.

## Migrate from Open Manager

1. Disable the old cron entry and wait for any active run to finish.
2. Back up the old deployment privately.
3. Install Host Helper into its new working directory.
4. Copy `.env`, `credentials.json`, `token.json`, `token_gmail.json`, `token_tasks.json`, and `open_manager.db` from the old working directory. Keep their original filenames and restrict file permissions.
5. Run a manual sync from the new directory and inspect Calendar/Tasks.
6. Install the new cron entry and keep the old deployment as a private backup until verified.

The default Tasks list becomes **Host Helper** by renaming and reusing the old **Open Manager** list. Existing task markers and Calendar identity are preserved. Set `TASK_LIST_TITLE=Open Manager` in `.env` if you prefer the original list name. Existing `python run.py` and `python authorize.py` commands remain supported through compatibility wrappers.
