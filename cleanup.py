import os
import sys
import time
import logging
import json
import subprocess

from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timezone
from tabulate import tabulate

from utilities import (
    Colors,
    send_discord_message,
    sleep,
    seconds_to_pretty,
    convert_speed,
    formatted_duration,
    timestamp_seconds,
    create_ssh_client,
    run_ssh_command,
)

from helpers import (
    get_storage_data,
    get_qbittorrent_client,
    get_total_torrent_size,
)

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ----------------------------------
# SSH config
# ----------------------------------
SSH_SERVER = os.getenv("SSH_SERVER")
SSH_PORT = int(os.getenv("SSH_PORT", 22))
SSH_USERNAME = os.getenv("SSH_USERNAME")
SSH_KEY = os.getenv("SSH_KEY")

COMMANDS_WHEN_LIMIT_HIT = os.getenv("COMMAND_LIMIT_HIT")
COMMANDS_WHEN_LIMIT_REFRESHED = os.getenv("COMMAND_LIMIT_REFRESHED")

SSH_CONFIGURED = bool(SSH_SERVER and SSH_USERNAME and SSH_KEY)

RUNNING_ON_SERVER = os.getenv("RUNNING_ON_SERVER", "false").lower() in ("true", "1", "yes")

# ----------------------------------
# Cleanup thresholds
# ----------------------------------
MIN_FREE_GB = int(os.getenv("MIN_FREE_GB", 300))
MAX_DELETIONS_PER_RUN = int(os.getenv("MAX_DELETIONS_PER_RUN", 5))
STORAGE_MISMATCH_THRESHOLD_GB = int(os.getenv("STORAGE_MISMATCH_THRESHOLD_GB", 200))
TRAFFIC_LIMIT_THRESHOLD = int(os.getenv("TRAFFIC_LIMIT_THRESHOLD", 98))

# ----------------------------------
# Unregistered torrent cleanup
# ----------------------------------
UNREGISTERED_CHECK_ENABLED = os.getenv("UNREGISTERED_CHECK_ENABLED", "true").lower() == "true"
UNREGISTERED_INACTIVE_HOURS = int(os.getenv("UNREGISTERED_INACTIVE_HOURS", 6))

# ----------------------------------
# State persistence
# ----------------------------------
STATE_FILE = Path(__file__).parent / "state.json"

_DEFAULT_STATE = {
    "bandwidth_commands_ran": False,
    "storage_commands_ran": False,
    "api_down_commands_ran": False,
}

def read_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            # Merge with defaults so new keys are always present
            return {**_DEFAULT_STATE, **data}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read state file, using defaults: {e}")
    return dict(_DEFAULT_STATE)

def write_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logger.error(f"Could not write state file: {e}")

BANDWIDTH_COMMANDS_RAN = False
STORAGE_COMMANDS_RAN = False
API_DOWN_COMMANDS_RAN = False

# ----------------------------------
# Tracker rule time units
# ----------------------------------
UNIT_TO_SECONDS = {
    "hours": 3600,
    "days": 86400,
    "weeks": 604800,
    "months": 2592000,  # 30 days
}


# ----------------------------------
# Discord message helpers
# ----------------------------------

def _stop_description():
    """Returns a past-tense sentence describing what the stop command did."""
    cmd = COMMANDS_WHEN_LIMIT_HIT or ""
    if "autobrr" in cmd.lower():
        return "autobrr has been stopped to prevent new torrents from being added"
    return "the stop command has been executed to prevent new torrents from being added"


def _start_description():
    """Returns a past-tense sentence describing what the recovery command did."""
    cmd = COMMANDS_WHEN_LIMIT_REFRESHED or ""
    if "autobrr" in cmd.lower():
        return "autobrr has been restarted and will resume adding new torrents"
    return "the recovery command has been executed"


def _still_stopped_description():
    """Returns a present-tense phrase describing the ongoing stopped state."""
    cmd = COMMANDS_WHEN_LIMIT_HIT or ""
    if "autobrr" in cmd.lower():
        return "autobrr remains stopped"
    return "the stop command has already run"


# ----------------------------------
# Startup validation
# ----------------------------------

def validate_config():
    """
    Checks that hard-required environment variables are set.
    Exits with a clear error message listing everything that is missing.
    """
    required = {
        "QB_HOST":      os.getenv("QB_HOST"),
        "QB_USERNAME":  os.getenv("QB_USERNAME"),
        "QB_PASSWORD":  os.getenv("QB_PASSWORD"),
    }

    missing = [key for key, value in required.items() if not value]
    if missing:
        logger.error("Cannot start: the following required variables are not set in your .env file:")
        for key in missing:
            logger.error(f"  {key}")
        logger.error("See .env.example for descriptions of each variable.")
        sys.exit(1)

    logger.info("Required configuration present.")


def warn_optional_config():
    """
    Logs a clear warning for each optional feature that is not configured,
    describing exactly what will not work as a result.
    """
    if RUNNING_ON_SERVER:
        # In server mode SSH is not used — only warn about missing commands
        if not COMMANDS_WHEN_LIMIT_HIT:
            logger.warning(
                "COMMAND_LIMIT_HIT is not set. No command will run when the traffic or storage "
                "limit is reached (AutoBRR will keep adding new torrents)."
            )
        if not COMMANDS_WHEN_LIMIT_REFRESHED:
            logger.warning(
                "COMMAND_LIMIT_REFRESHED is not set. No command will run when traffic or storage "
                "recovers (AutoBRR will not be automatically restarted)."
            )
    else:
        # SSH credentials
        ssh_vars_missing = [k for k, v in {"SSH_SERVER": SSH_SERVER, "SSH_USERNAME": SSH_USERNAME, "SSH_KEY": SSH_KEY}.items() if not v]
        if ssh_vars_missing:
            logger.warning(
                f"SSH is not configured ({', '.join(ssh_vars_missing)} not set). "
                "The following automated actions are disabled:"
            )
            logger.warning("  - Stopping AutoBRR (the app that adds new torrents to qBittorrent) when the monthly traffic limit is reached")
            logger.warning("  - Stopping AutoBRR when storage drops below the minimum threshold")
            logger.warning("  - Restarting AutoBRR when traffic or storage recovers")
            logger.warning(
                "  *** IMPORTANT: Without SSH configured, AutoBRR will continue adding new torrents even when "
                "your storage is full or your monthly traffic quota is exhausted. This will cause your disk to "
                "fill up completely, prevent any upload credit from being earned, and may cause errors on your "
                "seedbox. It is strongly recommended to configure SSH unless your storage is very large. ***"
            )
            logger.warning("  Set SSH_SERVER, SSH_USERNAME, and SSH_KEY in your .env to enable these.")
        else:
            # SSH is present — check the commands themselves
            if not COMMANDS_WHEN_LIMIT_HIT:
                logger.warning(
                    "COMMAND_LIMIT_HIT is not set. No SSH command will run when the traffic or storage "
                    "limit is reached (AutoBRR will keep adding new torrents)."
                )
            if not COMMANDS_WHEN_LIMIT_REFRESHED:
                logger.warning(
                    "COMMAND_LIMIT_REFRESHED is not set. No SSH command will run when traffic or storage "
                    "recovers (AutoBRR will not be automatically restarted)."
                )

    # Discord — general webhook
    if not os.getenv("DISCORD_WEBHOOK_URL"):
        logger.warning(
            "DISCORD_WEBHOOK_URL is not set. The following notifications will only appear in logs, not Discord:"
        )
        logger.warning("  - Torrent deletions (storage cleanup)")
        logger.warning("  - Traffic and storage limit alerts")
        logger.warning("  - Hourly storage summaries")
        logger.warning("  - qBittorrent connection errors")

    # Discord — inactive deletions webhook
    if not os.getenv("INACTIVE_DISCORD_URL"):
        logger.warning(
            "INACTIVE_DISCORD_URL is not set. Tracker-specific inactive deletion events "
            "will only appear in logs, not Discord."
        )

    # Unregistered torrent cleanup
    if not UNREGISTERED_CHECK_ENABLED:
        logger.warning(
            "UNREGISTERED_CHECK_ENABLED is false. Torrents that trackers have marked as "
            "'unregistered' will not be automatically removed."
        )


def validate_server_mode():
    """
    Validates that the local system commands required by RUNNING_ON_SERVER mode are available.
    Exits with a clear error if either command fails.
    """
    errors = []

    result = subprocess.run(["quota", "-s"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        errors.append("'quota -s' failed — is quota installed and configured for this user?")

    result = subprocess.run(["app-traffic", "info"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        errors.append("'app-traffic info' failed — is this command available on your system?")

    if errors:
        logger.error("RUNNING_ON_SERVER=true is set but required system commands are not working:")
        for err in errors:
            logger.error(f"  {err}")
        sys.exit(1)

    logger.info("Server mode validated: quota and app-traffic commands are available.")


def load_tracker_rules():
    """
    Loads and validates tracker_rules.json.
    Exits with a clear error if the file is missing, malformed, or contains invalid rules.
    Returns a list of validated rule dicts, each with a pre-computed min_seed_seconds.
    """
    rules_path = Path(__file__).parent / "tracker_rules.json"

    if not rules_path.exists():
        logger.error(
            f"tracker_rules.json not found at {rules_path}.\n"
            "Copy the default file and edit it to match your trackers:\n"
            "  cp tracker_rules.json.example tracker_rules.json"
        )
        sys.exit(1)

    try:
        with open(rules_path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"tracker_rules.json is not valid JSON: {e}")
        sys.exit(1)

    if not isinstance(raw, list):
        logger.error("tracker_rules.json must be a JSON array (a list of rule objects).")
        sys.exit(1)

    if len(raw) == 0:
        logger.warning("tracker_rules.json is empty. No tracker-specific cleanup will run.")
        return []

    errors = []
    validated = []

    for i, rule in enumerate(raw, start=1):
        rule_errors = []

        displayed_name = rule.get("displayedName")
        category = rule.get("category")
        min_seed_time = rule.get("min_seed_time")
        min_seed_unit = rule.get("min_seed_unit")

        if not isinstance(displayed_name, str) or not displayed_name.strip():
            rule_errors.append("'displayedName' must be a non-empty string")

        if not isinstance(category, str) or not category.strip():
            rule_errors.append("'category' must be a non-empty string")
        elif category.lower() == "keep":
            rule_errors.append("'category' cannot be 'keep' — that category is reserved to protect torrents from deletion")

        if not isinstance(min_seed_time, (int, float)) or min_seed_time <= 0:
            rule_errors.append("'min_seed_time' must be a positive number")

        if min_seed_unit not in UNIT_TO_SECONDS:
            rule_errors.append(f"'min_seed_unit' must be one of: {', '.join(UNIT_TO_SECONDS.keys())}")

        if rule_errors:
            errors.append(f"Rule {i} ({category!r}): {'; '.join(rule_errors)}")
        else:
            validated.append({
                "displayedName":    displayed_name,
                "category":         category,
                "min_seed_time":    min_seed_time,
                "min_seed_unit":    min_seed_unit,
                "min_seed_seconds": int(min_seed_time * UNIT_TO_SECONDS[min_seed_unit]),
            })

    if errors:
        logger.error("tracker_rules.json contains invalid rules:")
        for err in errors:
            logger.error(f"  {err}")
        sys.exit(1)

    return validated


def display_tracker_rules(rules):
    """Logs the loaded tracker rules as a table so users can verify them on startup."""
    if not rules:
        logger.info("No tracker cleanup rules loaded.")
        return

    table = [["Name", "qBit Category", "Min Seed Time", "Min Seed (seconds)"]]
    for r in rules:
        table.append([
            r["displayedName"],
            r["category"],
            f"{r['min_seed_time']} {r['min_seed_unit']}",
            f"{r['min_seed_seconds']:,}",
        ])

    formatted = tabulate(table, headers="firstrow", tablefmt="simple")
    logger.info(f"Tracker cleanup rules ({len(rules)} loaded):\n{formatted}")


# ----------------------------------
# SSH helper
# ----------------------------------

def _get_ssh_client():
    if not SSH_CONFIGURED:
        return None
    return create_ssh_client(SSH_SERVER, SSH_PORT, SSH_USERNAME, SSH_KEY)


def run_command_local(command):
    """
    Run a shell command locally. Exits the program if the command fails.
    Used in RUNNING_ON_SERVER mode where command failure is unrecoverable.
    """
    if not command:
        return
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"Local command failed (exit {result.returncode}): {command!r}")
            if result.stdout.strip():
                logger.error(f"  stdout: {result.stdout.strip()}")
            if result.stderr.strip():
                logger.error(f"  stderr: {result.stderr.strip()}")
            sys.exit(1)
        if result.stdout.strip():
            logger.info(result.stdout.strip())
    except Exception as e:
        logger.error(f"Error running local command {command!r}: {e}")
        sys.exit(1)


def run_limit_command(command):
    """
    Run a limit/recovery command (e.g. stop or start autobrr).
    Runs locally if RUNNING_ON_SERVER=true, otherwise executes over SSH.
    """
    if not command:
        return
    if RUNNING_ON_SERVER:
        run_command_local(command)
    else:
        ssh_client = _get_ssh_client()
        if ssh_client:
            run_ssh_command(ssh_client, command)
            ssh_client.close()


# ----------------------------------
# Traffic and storage checks
# ----------------------------------

def manage_traffic_based_ssh_commands(status_update=False):
    global BANDWIDTH_COMMANDS_RAN

    logger.info("Checking traffic usage...")
    storage_data = get_storage_data()
    if not storage_data:
        return

    service_stats = storage_data.get("service_stats_info", {})
    traffic_used_percentage = service_stats.get("traffic_used_percentage")
    traffic_available_percentage = service_stats.get("traffic_available_percentage")

    if traffic_available_percentage is None:
        logger.info("No traffic data in API response. Skipping traffic check.")
        return
    if not traffic_used_percentage and traffic_available_percentage == 100:
        traffic_used_percentage = 0

    if traffic_used_percentage >= TRAFFIC_LIMIT_THRESHOLD and not BANDWIDTH_COMMANDS_RAN:
        logger.info(f"Traffic at {traffic_used_percentage}% — limit reached. Running stop command.")
        run_limit_command(COMMANDS_WHEN_LIMIT_HIT)
        BANDWIDTH_COMMANDS_RAN = True
        write_state({**read_state(), "bandwidth_commands_ran": True})
        send_discord_message(
            title="Traffic Limit Hit",
            description=(
                f"Monthly traffic usage is at {traffic_used_percentage}% (threshold: {TRAFFIC_LIMIT_THRESHOLD}%). "
                f"{_stop_description().capitalize()} until traffic resets."
            ),
            color=Colors.RED
        )

    elif traffic_used_percentage < 10 and BANDWIDTH_COMMANDS_RAN:
        logger.info("Traffic reset. Running recovery command.")
        run_limit_command(COMMANDS_WHEN_LIMIT_REFRESHED)
        BANDWIDTH_COMMANDS_RAN = False
        write_state({**read_state(), "bandwidth_commands_ran": False})
        send_discord_message(
            title="Traffic Limit Refreshed",
            description=f"Monthly traffic has reset. {_start_description().capitalize()}.",
            color=Colors.GREEN
        )

    elif traffic_used_percentage < TRAFFIC_LIMIT_THRESHOLD:
        logger.info(f"Traffic at {traffic_used_percentage}% — within limits.")
        if status_update:
            send_discord_message(
                title="Traffic Update",
                description=f"Traffic at {traffic_used_percentage}% (limit: {TRAFFIC_LIMIT_THRESHOLD}%). No action needed.",
                color=Colors.GREENYELLOW
            )

    else:
        logger.info(f"Traffic at {traffic_used_percentage}% — commands already run, waiting for reset.")
        if status_update:
            send_discord_message(
                title="Traffic Update",
                description=(
                    f"Traffic still at {traffic_used_percentage}% (limit: {TRAFFIC_LIMIT_THRESHOLD}%). "
                    f"{_still_stopped_description().capitalize()} — waiting for monthly traffic reset."
                ),
                color=Colors.FUCHSIA
            )


def check_storage_mismatch():
    storage_data = get_storage_data()
    if not storage_data:
        return

    used_storage_gb = storage_data["service_stats_info"]["used_storage_value"] / 1024
    qb_client = get_qbittorrent_client()
    if not qb_client:
        return

    total_torrent_size_gb = get_total_torrent_size(qb_client)
    if total_torrent_size_gb is None:
        return

    mismatch_gb = used_storage_gb - total_torrent_size_gb
    if mismatch_gb > STORAGE_MISMATCH_THRESHOLD_GB:
        logger.warning(
            f"Storage mismatch: API reports {used_storage_gb:.2f} GB used, "
            f"qBittorrent totals {total_torrent_size_gb:.2f} GB ({mismatch_gb:.2f} GB gap)."
        )
        send_discord_message(
            title="Storage Mismatch Warning",
            description=(
                f"The storage API reports {used_storage_gb:.2f} GB used, but qBittorrent only accounts for "
                f"{total_torrent_size_gb:.2f} GB — a gap of {mismatch_gb:.2f} GB.\n\n"
                "This usually means non-torrent files (logs, media, partial downloads, etc.) are taking up significant space. "
                "Manual cleanup of files outside qBittorrent may be needed."
            ),
            color=Colors.YELLOW
        )


def send_hourly_storage_update():
    storage_data = get_storage_data()
    if storage_data:
        free_gb = storage_data["service_stats_info"]["free_storage_gb"]
        send_discord_message(
            title="Hourly Storage Update",
            description=f"Free storage: {free_gb:.2f} GB (minimum threshold: {MIN_FREE_GB} GB)",
            color=Colors.LIME
        )


# ----------------------------------
# Torrent cleanup
# ----------------------------------

def delete_torrent(qb_client, torrent, real_delete=False):
    if not qb_client:
        logger.error("No qBittorrent client available for deletion.")
        return
    if real_delete:
        qb_client.torrents_delete(torrent_hashes=[torrent.hash], delete_files=True)


def cleanup_torrents(qb_client, free_storage_gb):
    global STORAGE_COMMANDS_RAN, BANDWIDTH_COMMANDS_RAN

    logger.info(f"Running storage-based cleanup. Free: {free_storage_gb:.2f} GB, minimum: {MIN_FREE_GB} GB.")

    if free_storage_gb < MIN_FREE_GB and not STORAGE_COMMANDS_RAN and not BANDWIDTH_COMMANDS_RAN:
        logger.error("Storage below threshold. Running stop command.")
        send_discord_message(
            title="Critical Storage Alert",
            description=(
                f"Free storage has dropped below the {MIN_FREE_GB} GB minimum threshold. "
                f"{_stop_description().capitalize()} while cleanup runs."
            ),
            color=Colors.RED
        )
        run_limit_command(COMMANDS_WHEN_LIMIT_HIT)
        STORAGE_COMMANDS_RAN = True
        write_state({**read_state(), "storage_commands_ran": True})

    candidates = [
        t for t in qb_client.torrents_info()
        if t.ratio >= 1.0
        and t.state != "uploading"
        and timestamp_seconds(t.last_activity) > 7200
        and t.popularity < 80
        and (t.category or "").lower() != "keep"
    ]
    candidates = sorted(
        candidates,
        key=lambda t: (-t.popularity, (timestamp_seconds(t.last_activity), timestamp_seconds(t.added_on))),
        reverse=True,
    )

    freed_space = 0
    deletions = 0

    for torrent in candidates:
        if free_storage_gb - freed_space >= MIN_FREE_GB:
            logger.info("Minimum free space achieved. Stopping cleanup.")
            return

        if deletions >= MAX_DELETIONS_PER_RUN:
            logger.warning(f"Reached max deletions per run ({MAX_DELETIONS_PER_RUN}).")
            send_discord_message(
                title="Cleanup Limit Reached",
                description=(
                    f"Reached the {MAX_DELETIONS_PER_RUN} deletion limit for this cycle — this is a safety cap to avoid "
                    "removing too many torrents at once. Storage is still below the minimum, but cleanup will "
                    "re-evaluate and continue on the next cycle (every 5 minutes)."
                ),
                color=Colors.YELLOW
            )
            return

        delete_torrent(qb_client, torrent, real_delete=True)
        space_gb = torrent.size / (1024 ** 3)
        freed_space += space_gb
        free_storage_gb += space_gb
        deletions += 1

        logger.info(f"Deleted: {torrent.name} | Freed: {space_gb:.2f} GB")
        send_discord_message(
            title="Deleted Torrent (Storage Cleanup)",
            description=f"Torrent: {torrent.name}",
            color=Colors.RED,
            fields=[
                {"name": "Size",          "value": f"{space_gb:.2f} GB",                       "inline": True},
                {"name": "Ratio",         "value": f"{torrent.ratio:.2f}",                      "inline": True},
                {"name": "Popularity",    "value": f"{torrent.popularity:.2f}",                 "inline": True},
                {"name": "Upload Speed",  "value": convert_speed(torrent.upspeed),              "inline": True},
                {"name": "Seeding Time",  "value": seconds_to_pretty(torrent.seeding_time),     "inline": True},
                {"name": "Added On",      "value": formatted_duration(torrent.added_on),        "inline": True},
                {"name": "Last Activity", "value": formatted_duration(torrent.last_activity),   "inline": True},
                {"name": "Category",      "value": torrent.category or "None",                  "inline": True},
            ]
        )


def cleanup_inactive_tracker(qb_client, rule):
    """
    Deletes torrents whose category exactly matches rule['category'] and that have
    seeded past the minimum seed time and been inactive for more than 24 hours.
    """
    category = rule["category"]
    min_seed_seconds = rule["min_seed_seconds"]

    candidates = [
        t for t in qb_client.torrents_info()
        if (t.category or "") == category
        and t.state.lower() not in ["uploading", "downloading", "stalledUP", "checkingUP"]
        and t.seeding_time > min_seed_seconds
        and timestamp_seconds(t.last_activity) > 86400
    ]

    for torrent in candidates:
        delete_torrent(qb_client, torrent, real_delete=True)
        logger.info(
            f"[{rule['displayedName']}] Deleted: {torrent.name} "
            f"({rule['min_seed_time']} {rule['min_seed_unit']} seeding, inactive 24h+)."
        )
        send_discord_message(
            title=f"Deleted Torrent ({rule['displayedName']})",
            description=f"Torrent: {torrent.name}",
            color=Colors.FUCHSIA,
            url="inactive_deletion",
            fields=[
                {"name": "Size",          "value": f"{torrent.size / (1024 ** 3):.2f} GB",     "inline": True},
                {"name": "Ratio",         "value": f"{torrent.ratio:.2f}",                      "inline": True},
                {"name": "Popularity",    "value": f"{torrent.popularity:.2f}",                 "inline": True},
                {"name": "Upload Speed",  "value": convert_speed(torrent.upspeed),              "inline": True},
                {"name": "Seeding Time",  "value": seconds_to_pretty(torrent.seeding_time),     "inline": True},
                {"name": "Added On",      "value": formatted_duration(torrent.added_on),        "inline": True},
                {"name": "Last Activity", "value": formatted_duration(torrent.last_activity),   "inline": True},
                {"name": "Category",      "value": category,                                    "inline": True},
            ]
        )


def cleanup_unregistered_torrents(qb_client):
    if not UNREGISTERED_CHECK_ENABLED:
        return

    inactive_threshold = UNREGISTERED_INACTIVE_HOURS * 3600
    logger.info(f"Checking for unregistered torrents (inactive > {UNREGISTERED_INACTIVE_HOURS}h)...")

    try:
        torrents = qb_client.torrents_info()
    except Exception as e:
        logger.error(f"Failed to fetch torrent list: {e}")
        return

    deleted = 0

    for torrent in torrents:
        try:
            if (torrent.category or "").lower() == "keep":
                continue

            if torrent.state.lower() in ["downloading", "uploading", "checkingDL", "checkingUP"]:
                continue

            if timestamp_seconds(torrent.last_activity) < inactive_threshold:
                continue

            try:
                trackers = qb_client.torrents_trackers(hash=torrent.hash)
            except TimeoutError:
                logger.warning(f"Timeout fetching trackers for: {torrent.name}")
                continue
            except Exception as e:
                logger.error(f"Error fetching trackers for {torrent.name}: {e}")
                continue

            unregistered = any(
                tracker.get('msg', '').lower() == "unregistered torrent"
                for tracker in trackers
                if not tracker.get('url', '').startswith('** [')
            )

            if unregistered:
                delete_torrent(qb_client, torrent, real_delete=True)
                deleted += 1
                logger.info(f"Deleted unregistered torrent: {torrent.name}")
                send_discord_message(
                    title="Deleted Torrent (Unregistered)",
                    description=f"Torrent: {torrent.name}",
                    color=Colors.TEAL,
                    fields=[
                        {"name": "Size",           "value": f"{torrent.size / (1024 ** 3):.2f} GB",   "inline": True},
                        {"name": "Ratio",          "value": f"{torrent.ratio:.2f}",                    "inline": True},
                        {"name": "Last Activity",  "value": formatted_duration(torrent.last_activity), "inline": True},
                        {"name": "Category",       "value": torrent.category or "None",                "inline": True},
                        {"name": "Reason",         "value": f"Unregistered + {UNREGISTERED_INACTIVE_HOURS}h inactive", "inline": True},
                    ]
                )

        except Exception as e:
            logger.error(f"Unexpected error processing {torrent.name}: {e}")

    logger.info(f"Unregistered cleanup complete. Deleted {deleted} torrent(s).")


def send_next_torrents_to_delete_webhook(qb_client):
    candidates = [
        t for t in qb_client.torrents_info()
        if t.ratio >= 1.0
        and t.state != "uploading"
        and t.popularity < 100
        and (t.category or "").lower() != "keep"
    ]
    candidates.sort(
        key=lambda x: (-x.popularity, (timestamp_seconds(x.last_activity), timestamp_seconds(x.added_on))),
        reverse=True,
    )

    table = [["Name", "Size", "Ratio", "Fame", "Speed", "Seeding Time", "Added", "Activity", "Category"]]
    for t in candidates:
        table.append([
            t.name[:20],
            f"{t.size / (1024 ** 3):.2f} GB",
            f"{t.ratio:.2f}",
            f"{t.popularity:.2f}",
            convert_speed(t.upspeed),
            seconds_to_pretty(t.seeding_time),
            formatted_duration(t.added_on),
            formatted_duration(t.last_activity),
            t.category or "None",
        ])

    send_discord_message(
        description=(
            "**Next Scheduled Torrents**\n"
            "Torrents most likely to be deleted first during the next storage cleanup, ordered by deletion priority "
            "(lowest popularity + oldest activity first). All have ratio ≥ 1.0, are not actively uploading, "
            "and are not in the 'keep' category. Nothing is deleted until free storage drops below the minimum threshold.\n"
            "```\n\n" + tabulate(table, headers="firstrow", tablefmt="markdown") + "\n ```"
        ),
        color=Colors.GRAY,
        send_raw=True
    )


def save_torrent_files(qb_client):
    backup_dir = Path(__file__).parent / "torrent_file_backup"
    index_path = Path(__file__).parent / "torrent_file_backup.json"

    backup_dir.mkdir(exist_ok=True)

    torrent_index = {}
    if index_path.exists():
        with open(index_path) as f:
            try:
                torrent_index = json.load(f)
            except json.JSONDecodeError:
                corrupted_path = index_path.with_suffix(".corrupted")
                index_path.rename(corrupted_path)
                logger.warning(f"Corrupted torrent index renamed to {corrupted_path}, starting fresh.")

    existing_hashes = {p.stem for p in backup_dir.glob("*.torrent")}

    for torrent in qb_client.torrents_info():
        torrent_hash = torrent['hash']
        torrent_index[torrent_hash] = torrent["name"]

        if torrent_hash not in existing_hashes:
            try:
                torrent_data = qb_client.torrents_export(hash=torrent_hash)
                (backup_dir / f"{torrent_hash}.torrent").write_bytes(torrent_data)
                logger.info(f"Backed up: {torrent['name']}")
            except Exception as e:
                logger.error(f"Error backing up {torrent['name']}: {e}")

    with open(index_path, 'w') as f:
        json.dump(torrent_index, f, indent=4)


# ----------------------------------
# Main loop
# ----------------------------------

def main():
    global BANDWIDTH_COMMANDS_RAN, STORAGE_COMMANDS_RAN, API_DOWN_COMMANDS_RAN

    validate_config()
    warn_optional_config()
    tracker_rules = load_tracker_rules()
    display_tracker_rules(tracker_rules)

    if RUNNING_ON_SERVER:
        validate_server_mode()

    logger.info("Starting torrent cleanup script...")
    state = read_state()
    BANDWIDTH_COMMANDS_RAN = state["bandwidth_commands_ran"]
    STORAGE_COMMANDS_RAN = state["storage_commands_ran"]
    API_DOWN_COMMANDS_RAN = state["api_down_commands_ran"]

    send_hourly_storage_update()
    check_storage_mismatch()

    last_update_time = time.time()

    while True:
        logger.info("Running periodic checks...")

        qb_client = get_qbittorrent_client()

        # Storage-based cleanup
        storage_data = get_storage_data()
        if storage_data:
            if API_DOWN_COMMANDS_RAN:
                logger.info("Ultra API recovered. Running recovery command.")
                run_limit_command(COMMANDS_WHEN_LIMIT_REFRESHED)
                API_DOWN_COMMANDS_RAN = False
                write_state({**read_state(), "api_down_commands_ran": False})
                if RUNNING_ON_SERVER:
                    send_discord_message(
                        title="Storage Commands Recovered",
                        description=f"Local storage commands are returning data again. {_start_description().capitalize()} and storage/traffic monitoring has resumed.",
                        color=Colors.LIME
                    )
                else:
                    send_discord_message(
                        title="Ultra API Recovered",
                        description=f"The Ultra API is back online. {_start_description().capitalize()} and storage/traffic monitoring has resumed.",
                        color=Colors.LIME
                    )

            free_storage_gb = storage_data["service_stats_info"]["free_storage_gb"]

            if free_storage_gb > MIN_FREE_GB and STORAGE_COMMANDS_RAN and not BANDWIDTH_COMMANDS_RAN:
                logger.info("Storage recovered. Running recovery command.")
                run_limit_command(COMMANDS_WHEN_LIMIT_REFRESHED)
                STORAGE_COMMANDS_RAN = False
                write_state({**read_state(), "storage_commands_ran": False})
                send_discord_message(
                    title="Storage Recovered",
                    description=f"Free storage is back above {MIN_FREE_GB} GB. {_start_description().capitalize()}.",
                    color=Colors.LIME
                )

            if qb_client:
                cleanup_torrents(qb_client, free_storage_gb)
            else:
                send_discord_message(
                    title="qBittorrent Unreachable",
                    description="Could not connect to qBittorrent. Storage-based cleanup and torrent management will be skipped this cycle.",
                    color=Colors.RED
                )
        else:
            logger.warning("Storage data unavailable. Skipping storage-based cleanup this cycle.")
            if not API_DOWN_COMMANDS_RAN:
                logger.warning("Storage data unavailable. Running stop command to prevent quota overrun.")
                run_limit_command(COMMANDS_WHEN_LIMIT_HIT)
                API_DOWN_COMMANDS_RAN = True
                write_state({**read_state(), "api_down_commands_ran": True})
                if RUNNING_ON_SERVER:
                    send_discord_message(
                        title="Storage Commands Unavailable",
                        description=(
                            "The local storage commands (quota / app-traffic) failed to return data. "
                            f"{_stop_description().capitalize()} as a precaution to prevent quota overrun. "
                            "Will retry each cycle and run the recovery command automatically when the commands succeed again."
                        ),
                        color=Colors.RED
                    )
                else:
                    send_discord_message(
                        title="Ultra API Unreachable",
                        description=(
                            "The Ultra API (used to check storage and traffic usage) is not responding. "
                            f"{_stop_description().capitalize()} as a precaution to prevent quota overrun. "
                            "Will retry each cycle and run the recovery command automatically when the API comes back."
                        ),
                        color=Colors.RED
                    )

        if qb_client:
            cleanup_unregistered_torrents(qb_client)

            for rule in tracker_rules:
                cleanup_inactive_tracker(qb_client, rule)

            save_torrent_files(qb_client)
        else:
            logger.warning("qBittorrent unreachable. Skipping torrent cleanup this cycle.")

        check_storage_mismatch()
        manage_traffic_based_ssh_commands()

        if time.time() - last_update_time >= 3600:
            send_hourly_storage_update()
            manage_traffic_based_ssh_commands(status_update=True)
            if qb_client:
                send_next_torrents_to_delete_webhook(qb_client)
            last_update_time = time.time()

        sleep(300)


if __name__ == "__main__":
    main()
