# seedbox-torrent-manager

An automated qBittorrent cleanup daemon for [UltraSeedbox](https://ultra.cc/) that keeps your storage healthy by removing low-value torrents, clearing unregistered torrents, and enforcing tracker-specific seeding rules.

Integrates with the Ultra API for storage and traffic monitoring, Discord for notifications, and SSH for running remote commands (e.g. stopping AutoBRR when you approach your monthly traffic limit).

---

## What it does

- **Storage-based cleanup** — when free space drops below a threshold, deletes the oldest/least popular seeded torrents (ratio ≥ 1.0, inactive for 2+ hours) until space is recovered.
- **Unregistered torrent cleanup** — detects torrents the tracker has marked as "unregistered" and removes them after a configurable inactivity period.
- **Tracker-specific rules** — deletes torrents from specific trackers after their minimum seeding period has passed and they've been inactive for 24+ hours. Ships with rules for TorrentLeech and IPTorrents; fully configurable.
- **Traffic limit handling** — monitors your monthly traffic usage via the seedbox API and runs a configurable SSH command when you approach the limit (e.g. stopping AutoBRR).
- **Storage mismatch alerts** — warns when your API-reported storage and qBittorrent's total diverge significantly (a sign of orphaned files).
- **Hourly Discord summary** — posts free storage, traffic status, and a table of the next torrents in line for deletion.
- **Torrent file backup** — exports `.torrent` files from qBittorrent into a local backup folder.

The script runs in an infinite loop and rechecks every 5 minutes.

---

## Requirements

- Python 3.9+
- A running qBittorrent instance accessible over HTTP/HTTPS
- An UltraSeedbox account with the Ultra API installed (required — used for storage and traffic monitoring)
- SSH access to your seedbox (strongly recommended — without it, AutoBRR cannot be stopped when limits are hit, risking a full disk)
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

---

## Configuration reference

All configuration is via `.env`. See `.env.example` for the full list with descriptions.

| Variable | Required | Description |
|---|---|---|
| `QB_HOST` | Yes | qBittorrent URL (e.g. `https://user.server.usbx.me/qbittorrent`) |
| `QB_PORT` | Yes | qBittorrent port (typically `443` for HTTPS) |
| `QB_USERNAME` | Yes | qBittorrent username |
| `QB_PASSWORD` | Yes | qBittorrent password |
| `API_URL` | No | Storage/traffic API endpoint. Leave empty to disable storage features. |
| `BEARER_TOKEN` | No | Bearer token for the storage API. |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook for general notifications. |
| `INACTIVE_DISCORD_URL` | No | Discord webhook specifically for tracker-inactive deletions. |
| `SSH_SERVER` | No* | SSH hostname. If omitted, all SSH-based automation is disabled (see below). |
| `SSH_PORT` | No | SSH port (default: `22`) |
| `SSH_USERNAME` | No* | SSH username (required if SSH_SERVER is set) |
| `SSH_KEY` | No* | Path to your SSH private key file (required if SSH_SERVER is set) |
| `SSH_STRICT_HOST_KEYS` | No | Set to `true` to reject unknown SSH host keys instead of auto-accepting them (default: `false`). See [SSH host key verification](#ssh-host-key-verification). |
| `COMMAND_LIMIT_HIT` | No | SSH command to run when traffic/storage limit is hit. If unset, no command runs. |
| `COMMAND_LIMIT_REFRESHED` | No | SSH command to run when traffic/storage recovers. If unset, no command runs. |
| `MIN_FREE_GB` | No | Minimum free GB before cleanup triggers (default: `300`) |
| `MAX_DELETIONS_PER_RUN` | No | Max torrents deleted per cleanup pass (default: `5`) |
| `STORAGE_MISMATCH_THRESHOLD_GB` | No | Gap in GB before a mismatch alert fires (default: `200`) |
| `TRAFFIC_LIMIT_THRESHOLD` | No | Traffic usage % before limit command runs (default: `98`) |
| `UNREGISTERED_CHECK_ENABLED` | No | Enable unregistered torrent cleanup (default: `true`) |
| `UNREGISTERED_INACTIVE_HOURS` | No | Hours inactive before an unregistered torrent is deleted (default: `6`) |
| `TIMEZONE` | No | IANA timezone for activity timestamps (default: `UTC`) |

### What happens when optional config is missing

The script will log a warning at startup for each disabled feature, so you know exactly what is not running. Here is a summary:

| If you omit... | What stops working |
|---|---|
| `SSH_SERVER` / `SSH_USERNAME` / `SSH_KEY` | **AutoBRR will not be stopped when limits are hit.** AutoBRR is the app that automatically adds new torrents to qBittorrent — without SSH, it will keep adding torrents even when your storage is full or your monthly traffic quota is exhausted. This will cause your disk to fill completely, prevent upload credit from being earned, and may cause errors on your seedbox. Strongly recommended unless your storage is very large. |
| `COMMAND_LIMIT_HIT` | SSH connects fine but no command runs when a limit is hit. |
| `COMMAND_LIMIT_REFRESHED` | SSH connects fine but no command runs on recovery. |
| `DISCORD_WEBHOOK_URL` | No Discord notifications for deletions, storage alerts, errors, or hourly summaries. Everything still appears in the log. |
| `INACTIVE_DISCORD_URL` | No Discord notifications specifically for tracker-inactive deletions. |
| `UNREGISTERED_CHECK_ENABLED=false` | Torrents marked as unregistered by their tracker are left alone. |

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

By default (`SSH_STRICT_HOST_KEYS=false`) the script accepts any SSH host key automatically. This is convenient for a seedbox where the host is trusted and the key is unlikely to change, but it does mean a man-in-the-middle could go undetected.

If you prefer stricter behaviour, set `SSH_STRICT_HOST_KEYS=true`. The script will then use paramiko's `RejectPolicy`, which refuses connections to hosts not already in your `~/.ssh/known_hosts` file. To pre-populate it, SSH into your seedbox once manually:

```bash
ssh your_username@your.seedbox.host
```

After accepting the host key interactively, the script will connect without issues.

---

## Running as a systemd service

Create `/etc/systemd/system/seedbox-torrent-manager.service`:

```ini
[Unit]
Description=Seedbox Torrent Manager
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/seedbox-torrent-manager
ExecStart=/usr/bin/python3 /path/to/seedbox-torrent-manager/cleanup.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable seedbox-torrent-manager
sudo systemctl start seedbox-torrent-manager
```

---

## State files

The script persists a single `state.json` file in its working directory:

```json
{
  "bandwidth_commands_ran": false,
  "storage_commands_ran": false
}
```

- `bandwidth_commands_ran` — whether the SSH command was executed in response to the traffic limit being hit
- `storage_commands_ran` — whether the SSH command was executed in response to storage dropping below the minimum

This is used to avoid re-running commands on every loop and to know when to send a "recovered" command. The file is created automatically and excluded from git.

---

## UltraSeedbox setup

This tool is designed for use with [UltraSeedbox](https://ultra.cc/) seedboxes. The storage and traffic features depend on the Ultra API, which is a self-hosted utility you install on your seedbox.

### Installing the Ultra API

SSH into your seedbox and run:

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

The default `COMMAND_LIMIT_HIT` and `COMMAND_LIMIT_REFRESHED` values use UltraSeedbox's `app-*` app-management commands (e.g. `app-autobrr stop`). These are only available on UltraSeedbox. Adjust them to match whichever app you want to pause when your traffic or storage limit is hit.
