# seedbox-torrent-manager

An automated qBittorrent cleanup daemon for [UltraSeedbox](https://ultra.cc/) that keeps your storage healthy by removing low-value torrents, clearing unregistered torrents, and enforcing tracker-specific seeding rules.

See [CHANGELOG.md](CHANGELOG.md) for recent changes.

Integrates with Discord for notifications and supports two deployment modes for storage and traffic monitoring:

- **Remote mode** (default) — runs on any machine and connects to your seedbox via the Ultra API and SSH.
- **Server mode** (`RUNNING_ON_SERVER=true`) — runs directly on the seedbox itself, reading quota and traffic data locally and executing limit commands locally. No API or SSH required.

---

## What it does

- **Storage-based cleanup** — when free space drops below a threshold, deletes the oldest/least popular seeded torrents (ratio ≥ 1.0, inactive for 2+ hours) until space is recovered.
- **Unregistered torrent cleanup** — detects torrents the tracker has marked as "unregistered" and removes them after a configurable inactivity period.
- **Tracker-specific rules** — deletes torrents from specific trackers after their minimum seeding period has passed and they've been inactive for 24+ hours. Ships with rules for TorrentLeech and IPTorrents; fully configurable.
- **Traffic limit handling** — monitors your monthly traffic usage and runs a configurable command when you approach the limit (e.g. stopping AutoBRR).
- **API outage protection** — in remote mode, if the Ultra API becomes unreachable, the stop command is run automatically to prevent quota overrun. AutoBRR is restarted once the API recovers. In server mode, failed `quota` / `app-traffic` checks are retried without stopping the managed app, with Discord alerts at most once per hour.
- **Storage mismatch alerts** — warns when your reported storage and qBittorrent's total diverge significantly (a sign of orphaned files).
- **Orphaned folder cleanup** — on a configurable schedule, compares qBittorrent content folders to the seed directory and moves orphaned folders into a safe delete directory (restorable before manual emptying).
- **Hourly Discord summary** — posts free storage, traffic status, and a table of the next torrents in line for deletion.
- **Torrent file backup** — exports `.torrent` files from qBittorrent into a local backup folder. Handles a corrupted index gracefully by renaming it to `.corrupted` and starting fresh.

The script runs in an infinite loop and rechecks every 5 minutes.

---

## Requirements

- Python 3.9+
- A running qBittorrent instance accessible over HTTP/HTTPS
- One of:
  - **Remote mode**: Ultra API installed on your seedbox + SSH access
  - **Server mode**: Script running directly on the seedbox (no API or SSH needed)
- (Optional) A Discord webhook URL for notifications

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/ThatProgrammerr/seedbox-torrent-manager.git
cd seedbox-torrent-manager
pip install -r requirements.txt
```

### 2. Configure your environment

```bash
cp .env.example .env
```

Edit `.env` with your values. At minimum you need the qBittorrent credentials. Everything else is optional and those features are automatically disabled if left empty.

### 3. Run

```bash
python3 cleanup.py
```

On Ultra.cc, prefer the [user systemd service](#running-as-a-systemd-service-ultracc) so the script survives SSH disconnects and node reboots without `screen`.

---

## Deployment modes

### Remote mode (default)

The script runs on any machine outside the seedbox. Storage and traffic data is fetched from the Ultra API, and limit commands are executed over SSH.

Requires: `API_URL`, `BEARER_TOKEN`, `SSH_SERVER`, `SSH_USERNAME`, `SSH_KEY`.

### Server mode (`RUNNING_ON_SERVER=true`)

The script runs directly on the seedbox. Storage data is read from `quota -s` and traffic data from `app-traffic info`. Limit commands (`COMMAND_LIMIT_HIT` / `COMMAND_LIMIT_REFRESHED`) are executed as local shell commands.

**SSH and the Ultra API are not required.** If a limit command fails, the script exits immediately with an error — there is no silent fallback.

On startup, the script checks that `quota` and `app-traffic` are accessible. If they are temporarily unavailable (common right after a node reboot), the script **keeps running**, retries each cycle, leaves AutoBRR alone, and sends Discord alerts at most once per hour until the commands work again.

---

## Configuration reference

All configuration is via `.env`. See `.env.example` for the full list with descriptions.

| Variable | Required | Description |
|---|---|---|
| `QB_HOST` | Yes | qBittorrent URL (e.g. `https://user.server.usbx.me/qbittorrent`) |
| `QB_PORT` | Yes | qBittorrent port (typically `443` for HTTPS) |
| `QB_USERNAME` | Yes | qBittorrent username |
| `QB_PASSWORD` | Yes | qBittorrent password |
| `RUNNING_ON_SERVER` | No | Set to `true` to run in server mode — reads quota/traffic locally and executes commands locally. Disables the Ultra API and SSH requirements. (default: `false`) |
| `API_URL` | No | Storage/traffic API endpoint. Not needed if `RUNNING_ON_SERVER=true`. |
| `BEARER_TOKEN` | No | Bearer token for the storage API. Not needed if `RUNNING_ON_SERVER=true`. |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook for general notifications. |
| `INACTIVE_DISCORD_URL` | No | Discord webhook specifically for tracker-inactive deletions. |
| `ORPHAN_DISCORD_URL` | No | Optional dedicated Discord webhook for orphan-folder check notifications. Falls back to `DISCORD_WEBHOOK_URL` when unset. |
| `SSH_SERVER` | No* | SSH hostname. Not needed if `RUNNING_ON_SERVER=true`. If omitted in remote mode, all SSH-based automation is disabled (see below). |
| `SSH_PORT` | No | SSH port (default: `22`) |
| `SSH_USERNAME` | No* | SSH username (required if `SSH_SERVER` is set) |
| `SSH_KEY` | No* | Path to your SSH private key file (required if `SSH_SERVER` is set) |
| `SSH_STRICT_HOST_KEYS` | No | Set to `true` to reject unknown SSH host keys instead of auto-accepting them (default: `false`). See [SSH host key verification](#ssh-host-key-verification). |
| `COMMAND_LIMIT_HIT` | No | Command to run when traffic/storage limit is hit. Executed over SSH in remote mode, locally in server mode. If unset, no command runs. |
| `COMMAND_LIMIT_REFRESHED` | No | Command to run when traffic/storage recovers. Executed over SSH in remote mode, locally in server mode. If unset, no command runs. |
| `COMMAND_LIMIT_STATUS` | No | Command that reports whether the managed app (e.g. AutoBRR) is running. While a limit is active, this is checked each cycle and `COMMAND_LIMIT_HIT` is re-run if the app is up again (e.g. after a node reboot). Exit code `0` means running unless a pattern is set. |
| `COMMAND_LIMIT_STATUS_RUNNING_PATTERN` | No | Optional case-insensitive regex matched against status command output. When set, the app is considered running only if the pattern matches. |
| `MIN_FREE_GB` | No | Minimum free GB before cleanup triggers (default: `300`) |
| `MAX_DELETIONS_PER_RUN` | No | Max torrents deleted per cleanup pass (default: `5`) |
| `STORAGE_MISMATCH_THRESHOLD_GB` | No | Gap in GB before a mismatch alert fires (default: `200`) |
| `TRAFFIC_LIMIT_THRESHOLD` | No | Traffic usage % before limit command runs (default: `98`) |
| `ORPHAN_CHECK_ENABLED` | No | Enable scheduled orphan-folder checks (default: `false`) |
| `ORPHAN_REMOTE_PATH` | No | Seed root path used to detect orphaned folders (required if orphan checks enabled) |
| `ORPHAN_DELETE_PATH` | No | Destination path where orphaned folders are moved. Default: `ORPHAN_REMOTE_PATH/../delete` |
| `ORPHAN_MOVE_ENABLED` | No | If `false`, orphan checks run in dry-run mode and only report findings (default: `false`) |
| `ORPHAN_CHECK_INTERVAL_HOURS` | No | Hours between orphan-folder checks (default: `6`) |
| `ORPHAN_IGNORE_FOLDERS` | No | Comma-separated folder names to exclude from orphan moves |
| `UNREGISTERED_CHECK_ENABLED` | No | Enable unregistered torrent cleanup (default: `true`) |
| `UNREGISTERED_INACTIVE_HOURS` | No | Hours inactive before an unregistered torrent is deleted (default: `6`) |
| `TIMEZONE` | No | IANA timezone for activity timestamps (default: `UTC`) |

### What happens when optional config is missing

The script will log a warning at startup for each disabled feature, so you know exactly what is not running. Here is a summary:

| If you omit... | What stops working |
|---|---|
| `SSH_SERVER` / `SSH_USERNAME` / `SSH_KEY` (remote mode only) | **AutoBRR will not be stopped when limits are hit.** AutoBRR is the app that automatically adds new torrents to qBittorrent — without SSH, it will keep adding torrents even when your storage is full or your monthly traffic quota is exhausted. This will cause your disk to fill completely, prevent upload credit from being earned, and may cause errors on your seedbox. Not required if `RUNNING_ON_SERVER=true`. |
| `COMMAND_LIMIT_HIT` | No command runs when a limit is hit. |
| `COMMAND_LIMIT_REFRESHED` | No command runs on recovery. |
| `COMMAND_LIMIT_STATUS` | No live check that the managed app stayed stopped. After a node reboot AutoBRR may start again while `state.json` still says the stop command already ran. |
| `DISCORD_WEBHOOK_URL` | No Discord notifications for deletions, storage alerts, errors, or hourly summaries. Everything still appears in the log. |
| `INACTIVE_DISCORD_URL` | No Discord notifications specifically for tracker-inactive deletions. |
| `UNREGISTERED_CHECK_ENABLED=false` | Torrents marked as unregistered by their tracker are left alone. |
| `ORPHAN_CHECK_ENABLED=true` without `ORPHAN_REMOTE_PATH` | Orphan-folder checks are skipped because no seed path is configured. |

### Orphaned folder cleanup

When enabled, the script periodically:

1. Reads active torrent content folders from qBittorrent.
2. Lists folders in `ORPHAN_REMOTE_PATH`.
3. Finds folders present on disk but no longer linked to any active torrent.
4. Moves those folders into `ORPHAN_DELETE_PATH` (or `ORPHAN_REMOTE_PATH/../delete` by default).

This intentionally does **not** permanently delete files. Users can review and restore from the delete folder before manually emptying it.

In `RUNNING_ON_SERVER=true` mode this operation runs locally. In remote mode it runs over SSH.

Storage mismatch alerts continue to run independently. If both a mismatch alert and an orphan-folder finding appear, that warrants investigation — the gap may be orphaned seed folders, or something else entirely (logs, files outside the seed path, etc.).

### Tracker cleanup rules (`tracker_rules.json`)

Tracker-specific seeding rules are configured in `tracker_rules.json`, not in `.env`. This lets you define as many trackers as you need, each with their own minimum seeding time.

The file ships with defaults for TorrentLeech and IPTorrents:

```json
[
  {
    "displayedName": "TorrentLeech",
    "category": "tl",
    "min_seed_time": 11,
    "min_seed_unit": "days"
  },
  {
    "displayedName": "IPTorrents",
    "category": "ipt",
    "min_seed_time": 15,
    "min_seed_unit": "days"
  }
]
```

Each rule requires four fields:

| Field | Description |
|---|---|
| `displayedName` | A human-readable name shown in logs and Discord notifications (e.g. `"TorrentLeech"`) |
| `category` | The exact qBittorrent category string to match — keep this as short as you like (e.g. `"tl"`) |
| `min_seed_time` | A positive number representing the minimum seeding duration |
| `min_seed_unit` | One of: `hours`, `days`, `weeks`, `months` |

A torrent is eligible for deletion when its category **exactly matches** the rule's `category`, it has been seeding longer than `min_seed_time`, and it has been inactive for more than 24 hours.

The `keep` category is reserved — any torrent assigned to `keep` in qBittorrent is excluded from all automated cleanup. Adding a rule targeting `keep` will be rejected at startup.

On startup the script prints a table of all loaded rules so you can confirm they look correct before the first cleanup cycle runs.

---

## SSH host key verification

> Not relevant when using `RUNNING_ON_SERVER=true`.

By default (`SSH_STRICT_HOST_KEYS=false`) the script accepts any SSH host key automatically. This is convenient for a seedbox where the host is trusted and the key is unlikely to change, but it does mean a man-in-the-middle could go undetected.

If you prefer stricter behaviour, set `SSH_STRICT_HOST_KEYS=true`. The script will then use paramiko's `RejectPolicy`, which refuses connections to hosts not already in your `~/.ssh/known_hosts` file. To pre-populate it, SSH into your seedbox once manually:

```bash
ssh your_username@your.seedbox.host
```

After accepting the host key interactively, the script will connect without issues.

---

## Running as a systemd service (Ultra.cc)

On [Ultra.cc](https://ultra.cc/) you do **not** have root/`sudo`, and you should not use `screen` or `tmux` to keep the script alive. Use a **user** systemd unit instead — the same pattern Ultra documents for custom apps.

A ready-made unit ships in [`deploy/seedbox-torrent-manager.service`](deploy/seedbox-torrent-manager.service).

### 1. Install dependencies in a virtualenv (recommended)

From the repo directory on your slot:

```bash
cd ~/seedbox-torrent-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp -n .env.example .env
# edit .env — for Ultra slots set RUNNING_ON_SERVER=true
```

### 2. Install the user unit

```bash
mkdir -p ~/.config/systemd/user
cp ~/seedbox-torrent-manager/deploy/seedbox-torrent-manager.service \
  ~/.config/systemd/user/seedbox-torrent-manager.service
```

Edit the unit if your clone path or Python path differs:

```bash
nano ~/.config/systemd/user/seedbox-torrent-manager.service
```

The shipped file assumes:

- Repo at `~/seedbox-torrent-manager`
- Interpreter at `~/seedbox-torrent-manager/.venv/bin/python`

`%h` expands to your home directory. Do **not** set `User=` / `Group=` in a user unit.

### 3. Enable and start

Always use `systemctl --user` on Ultra.cc:

```bash
systemctl --user daemon-reload
systemctl --user enable --now seedbox-torrent-manager.service
systemctl --user status seedbox-torrent-manager.service
```

Useful commands:

```bash
systemctl --user restart seedbox-torrent-manager.service
systemctl --user stop seedbox-torrent-manager.service
journalctl --user -u seedbox-torrent-manager.service -f
```

Ultra.cc enables linger for your account, so the service keeps running after you disconnect SSH and comes back after a node reboot. Confirm with:

```bash
loginctl show-user "$(whoami)" | grep -i linger
```

If `Linger=no`, open a support ticket with Ultra.

### Non-Ultra hosts (system-wide unit)

On a VPS where you have root, you can instead install a system unit under `/etc/systemd/system/` (with `User=` / `WorkingDirectory=` / `ExecStart=` filled in) and use `sudo systemctl enable --now seedbox-torrent-manager`. Prefer the user-unit approach above on Ultra.cc.

---

## State files

The script persists a single `state.json` file in its working directory:

```json
{
  "bandwidth_commands_ran": false,
  "storage_commands_ran": false,
  "api_down_commands_ran": false
}
```

- `bandwidth_commands_ran` — whether the stop command was executed in response to the traffic limit being hit
- `storage_commands_ran` — whether the stop command was executed in response to storage dropping below the minimum
- `api_down_commands_ran` — remote mode: whether the stop command was executed because the Ultra API was unreachable. Server mode: whether local `quota` / `app-traffic` commands are currently failing (the managed app is **not** stopped for this)

This is used to avoid re-running commands on every loop and to know when to send a "recovered" command. The file is created automatically and excluded from git.

If `COMMAND_LIMIT_STATUS` is set, the script also verifies the managed app is still stopped whenever any of these flags is true. That covers the case where a seedbox/node reboot starts AutoBRR again while `state.json` still records that the stop command already ran — the stop command is re-executed automatically.

---

## UltraSeedbox setup

### Server mode (recommended if running on the seedbox)

If you are running this script directly on your UltraSeedbox, set `RUNNING_ON_SERVER=true` in your `.env`. The script will use `quota -s` and `app-traffic info` — both available by default on UltraSeedbox — to read storage and traffic data locally. No API installation or SSH configuration is needed.

### Remote mode — installing the Ultra API

If you are running the script from a separate machine, you need the Ultra API installed on your seedbox. SSH in and run:

```bash
bash <(curl -s https://scripts.ultra.cc/util-v2/Ultra-API/main.sh)
```

This installs a local API server and a stats script at `$HOME/scripts/Ultra-API/stats_request.py`.

**Important — remove the rate limit.** The default install rate-limits the API to 2 requests per minute, which is too restrictive for this script (it polls every 5 minutes but also calls the API for other checks). Edit the stats script and remove the rate limiter:

```bash
nano ~/scripts/Ultra-API/stats_request.py
```

Find and delete every line that contains:

```python
@limiter.limit("2 per minute", override_defaults=True)
```

There may be multiple occurrences — remove all of them, then save.

Once the API is running, your `API_URL` will be:

```
https://USERNAME.SERVER.usbx.me/ultra-api/total-stats
```

You can find your `BEARER_TOKEN` in the API configuration that was set up during the install step. Refer to the [Ultra API documentation](https://docs.ultra.cc/unofficial-ssh-utilities/storagetraffic-api-endpoint) for details.

### SSH commands

The default `COMMAND_LIMIT_HIT` and `COMMAND_LIMIT_REFRESHED` values use UltraSeedbox's `app-*` app-management commands (e.g. `app-autobrr stop`). These are available on UltraSeedbox regardless of whether you use remote or server mode. Adjust them to match whichever app you want to pause when your traffic or storage limit is hit.

`COMMAND_LIMIT_STATUS` should match the same app. On UltraSeedbox a reliable check is `systemctl --user is-active --quiet autobrr` (exit `0` = running). Alternatives include `pgrep -x autobrr`, or a status command paired with `COMMAND_LIMIT_STATUS_RUNNING_PATTERN` when the tool always exits `0` and prints text like `running` / `stopped`.
