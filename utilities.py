import os
import time
import logging
import requests
import paramiko
import pytz
from enum import IntEnum
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
INACTIVE_DISCORD_URL = os.getenv("INACTIVE_DISCORD_URL")
ORPHAN_DISCORD_URL = os.getenv("ORPHAN_DISCORD_URL")
TIMEZONE = os.getenv("TIMEZONE", "UTC")
SSH_STRICT_HOST_KEYS = os.getenv("SSH_STRICT_HOST_KEYS", "false").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class Colors(IntEnum):
    GREEN = 65280
    RED = 16711680
    BLUE = 255
    BLACK = 0
    WHITE = 16777215
    YELLOW = 16776960
    MAGENTA = 16711935
    CYAN = 65535
    GRAY = 8421504
    SILVER = 12632256
    MAROON = 8388608
    OLIVE = 8421376
    GREENYELLOW = 10145074
    TEAL = 32896
    NAVY = 128
    FUCHSIA = 16711935
    AQUA = 65535
    LIME = 65280


def sleep(delay):
    logger.info(f"Sleeping for {delay} seconds")
    time.sleep(delay)



def days_to_milliseconds(days):
    return int(days * 24 * 60 * 60 * 1000)


def days_to_seconds(days):
    return int(days * 24 * 60 * 60)


def milliseconds_to_pretty(milliseconds):
    total_seconds = int(milliseconds / 1000)
    days = total_seconds // 86400
    remaining_seconds = total_seconds % 86400
    hours = remaining_seconds // 3600
    remaining_seconds %= 3600
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0 or days == 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0 or (days == 0 and hours == 0):
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return ', '.join(parts)


def seconds_to_pretty(seconds):
    days = seconds // 86400
    remaining_seconds = seconds % 86400
    hours = remaining_seconds // 3600
    remaining_seconds %= 3600
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0 or days == 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0 or (days == 0 and hours == 0):
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return ', '.join(parts)


def convert_hours_pretty(total_hours):
    total_seconds = int(total_hours * 3600)
    days = total_seconds // 86400
    remaining_seconds = total_seconds % 86400
    hours = remaining_seconds // 3600
    remaining_seconds %= 3600
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0 or days == 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return ', '.join(parts)


def formatted_duration(last_activity_epoch):
    tz = pytz.timezone(TIMEZONE)
    current_tz_dt = datetime.now(timezone.utc).astimezone(tz)
    last_activity_dt = datetime.fromtimestamp(last_activity_epoch, tz)
    duration_seconds = int((current_tz_dt - last_activity_dt).total_seconds())

    days = duration_seconds // (3600 * 24)
    remaining_seconds = duration_seconds % (3600 * 24)
    hours = remaining_seconds // 3600
    remaining_seconds %= 3600
    minutes = remaining_seconds // 60

    parts = []
    if days or (days == 0 and hours > 0):
        parts.append(f"{days} days")
    if hours or (hours == 0 and minutes > 0):
        parts.append(f"{hours} hours")
    if minutes:
        parts.append(f"{minutes} minutes")
    return ", ".join(parts) if parts else "0 minutes"


def timestamp_seconds(last_activity_epoch):
    tz = pytz.timezone(TIMEZONE)
    current_tz_dt = datetime.now(timezone.utc).astimezone(tz)
    last_activity_dt = datetime.fromtimestamp(last_activity_epoch, tz)
    return int((current_tz_dt - last_activity_dt).total_seconds())


def create_ssh_client(server, port, username, key_path, retries=3, delay=5):
    client = paramiko.SSHClient()
    if SSH_STRICT_HOST_KEYS:
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    for attempt in range(1, retries + 1):
        try:
            client.connect(server, port=port, username=username, key_filename=key_path, timeout=10)
            logger.info("SSH connection established.")
            return client
        except (paramiko.ssh_exception.NoValidConnectionsError, TimeoutError) as e:
            logger.warning(f"Attempt {attempt}: SSH connection failed - {e}")
            if attempt < retries:
                logger.info(f"Retrying in {delay} seconds...")
                sleep(delay)
            else:
                logger.error("Max SSH retries reached. Skipping SSH connection.")
                return None
        except paramiko.AuthenticationException:
            logger.error("SSH authentication failed. Check SSH_USERNAME and SSH_KEY.")
            return None
        except paramiko.SSHException as e:
            logger.error(f"SSH error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected SSH error: {e}")
            return None


def run_ssh_command(ssh_client, command):
    if ssh_client is None:
        return "SSH client is not connected."
    try:
        stdin, stdout, stderr = ssh_client.exec_command(command)
        output = stdout.read().decode()
        error = stderr.read().decode()
        if error:
            logger.warning(f"SSH command error output: {error}")
        return output or error
    except Exception as e:
        logger.error(f"Failed to execute SSH command: {e}")
        return f"Error executing command: {e}"


def send_discord_message(
    send_raw=False,
    title=None,
    description=None,
    color=Colors.BLUE,
    fields=None,
    footer=None,
    include_timestamp=True,
    embed=True,
    url="default"
):
    if url == "default":
        webhook_url = DISCORD_WEBHOOK_URL
        if not webhook_url:
            logger.debug("DISCORD_WEBHOOK_URL not set, skipping notification.")
            return
    elif url == "inactive_deletion":
        webhook_url = INACTIVE_DISCORD_URL
        if not webhook_url:
            logger.debug("INACTIVE_DISCORD_URL not set, skipping notification.")
            return
    elif url == "orphan":
        webhook_url = ORPHAN_DISCORD_URL or DISCORD_WEBHOOK_URL
        if not webhook_url:
            logger.debug("ORPHAN_DISCORD_URL and DISCORD_WEBHOOK_URL not set, skipping notification.")
            return
    else:
        logger.warning(f"Unknown Discord URL key: {url}")
        return

    embed_payload = {
        "title": title,
        "description": description,
        "color": color
    }

    if fields:
        embed_payload["fields"] = fields
    if footer:
        embed_payload["footer"] = {"text": footer}
    if include_timestamp:
        embed_payload["timestamp"] = datetime.now(timezone.utc).isoformat()

    payload = {"content": description} if send_raw else {"embeds": [embed_payload]}

    try:
        response = requests.post(webhook_url, json=payload)
        if response.status_code == 204:
            logger.info(f"Discord notification sent: {title}")
        else:
            logger.error(f"Discord webhook failed. Status: {response.status_code}")
    except Exception as e:
        logger.exception(f"Error sending Discord notification: {e}")


def convert_speed(bps):
    kb = 1024
    mb = kb * 1024
    gb = mb * 1024
    tb = gb * 1024

    if bps < kb:
        return f"{bps:.2f} B/s"
    elif bps < mb:
        return f"{bps / kb:.2f} KB/s"
    elif bps < gb:
        return f"{bps / mb:.2f} MB/s"
    elif bps < tb:
        return f"{bps / gb:.2f} GB/s"
    else:
        return f"{bps / tb:.2f} TB/s"
