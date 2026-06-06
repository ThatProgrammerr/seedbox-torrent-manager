from enum import Enum
import os
import logging
import subprocess
import requests
import qbittorrentapi

from pathlib import Path
from dotenv import load_dotenv

from utilities import Colors, send_discord_message, sleep

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

API_URL = os.getenv("API_URL", "")
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "")
RUNNING_ON_SERVER = os.getenv("RUNNING_ON_SERVER", "false").lower() in ("true", "1", "yes")

QB_HOST = os.getenv("QB_HOST", "localhost")
QB_PORT = int(os.getenv("QB_PORT", 8080))
QB_USERNAME = os.getenv("QB_USERNAME", "admin")
QB_PASSWORD = os.getenv("QB_PASSWORD", "adminadmin")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_STORAGE_FETCH_ERROR = None
_COMMAND_TIMEOUT_SECONDS = 10


def get_storage_fetch_error():
    """Returns diagnostic detail from the most recent failed storage data fetch."""
    return _STORAGE_FETCH_ERROR or "Unknown error — no diagnostic information available."


def _set_storage_fetch_error(error):
    global _STORAGE_FETCH_ERROR
    _STORAGE_FETCH_ERROR = error


def _clear_storage_fetch_error():
    global _STORAGE_FETCH_ERROR
    _STORAGE_FETCH_ERROR = None


def _format_subprocess_output(result):
    parts = []
    if result.returncode != 0:
        parts.append(f"exit code: {result.returncode}")
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if not parts:
        parts.append("(no output)")
    return "\n".join(parts)


def _format_command_error(command, result):
    return f"$ {' '.join(command)}\n{_format_subprocess_output(result)}"


def _format_timeout_error(command, timeout_seconds):
    timeout_ms = int(timeout_seconds * 1000)
    return f"$ {' '.join(command)}\nExperienced a timeout of {timeout_ms}ms"


def _parse_quota_size(value_str):
    """Convert a quota -s size string (e.g. '512G', '1024M*') to bytes."""
    value_str = value_str.rstrip('*').strip()
    multipliers = {'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4}
    for suffix, mult in multipliers.items():
        if value_str.upper().endswith(suffix):
            return float(value_str[:-1]) * mult
    return float(value_str)


def get_storage_data_local():
    """
    Fetches storage and traffic statistics using local system commands.
    Used when RUNNING_ON_SERVER=true. Returns the same structure as get_storage_data().
    """
    quota_command = ["quota", "-s"]
    traffic_command = ["app-traffic", "info"]

    try:
        try:
            quota_result = subprocess.run(
                quota_command, capture_output=True, text=True, timeout=_COMMAND_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            error = _format_timeout_error(quota_command, _COMMAND_TIMEOUT_SECONDS)
            logger.error(f"Timed out running 'quota -s': {error}")
            _set_storage_fetch_error(error)
            return None

        if quota_result.returncode != 0:
            error = _format_command_error(quota_command, quota_result)
            logger.error(f"'quota -s' failed: {error}")
            _set_storage_fetch_error(error)
            return None

        used_bytes = None
        total_bytes = None
        for line in quota_result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0].startswith('/'):
                used_bytes = _parse_quota_size(parts[1])
                total_bytes = _parse_quota_size(parts[3])  # hard limit
                break

        if used_bytes is None or total_bytes is None:
            error = (
                f"$ {' '.join(quota_command)}\n"
                "Could not parse output.\n"
                f"{_format_subprocess_output(quota_result)}"
            )
            logger.error("Could not parse 'quota -s' output.")
            _set_storage_fetch_error(error)
            return None

        free_bytes = max(0.0, total_bytes - used_bytes)
        free_gb = free_bytes / (1024 ** 3)
        used_mb = used_bytes / (1024 ** 2)

        try:
            traffic_result = subprocess.run(
                traffic_command, capture_output=True, text=True, timeout=_COMMAND_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            error = _format_timeout_error(traffic_command, _COMMAND_TIMEOUT_SECONDS)
            logger.error(f"Timed out running 'app-traffic info': {error}")
            _set_storage_fetch_error(error)
            return None

        if traffic_result.returncode != 0:
            error = _format_command_error(traffic_command, traffic_result)
            logger.error(f"'app-traffic info' failed: {error}")
            _set_storage_fetch_error(error)
            return None

        traffic_available_pct = None
        for line in traffic_result.stdout.splitlines():
            if "traffic available:" in line.lower():
                parts = line.split(":", 1)
                if len(parts) == 2:
                    traffic_available_pct = float(parts[1].strip().rstrip('%'))
                    break

        if traffic_available_pct is None:
            error = (
                f"$ {' '.join(traffic_command)}\n"
                "Could not parse output.\n"
                f"{_format_subprocess_output(traffic_result)}"
            )
            logger.error("Could not parse 'app-traffic info' output.")
            _set_storage_fetch_error(error)
            return None

        traffic_used_pct = round(100 - traffic_available_pct, 2)

        _clear_storage_fetch_error()
        return {
            "service_stats_info": {
                "free_storage_gb": free_gb,
                "used_storage_value": used_mb,
                "traffic_used_percentage": traffic_used_pct,
                "traffic_available_percentage": traffic_available_pct,
            }
        }
    except Exception as e:
        error = f"Unexpected error fetching local storage data: {e}"
        logger.exception(error)
        _set_storage_fetch_error(error)
        return None


def get_storage_data():
    """
    Fetches storage and traffic statistics.
    Uses local system commands if RUNNING_ON_SERVER=true, otherwise calls the Ultra API.
    Returns None if data is unavailable or the request fails.
    """
    if RUNNING_ON_SERVER:
        return get_storage_data_local()

    if not API_URL or not BEARER_TOKEN:
        logger.debug("API_URL or BEARER_TOKEN not set. Skipping storage data fetch.")
        return None

    headers = {"Authorization": f"Bearer {BEARER_TOKEN}"}
    try:
        response = requests.get(API_URL, headers=headers)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 429:
            logger.warning("Hit API rate limit. Sleeping for 2 minutes...")
            sleep(120)
        else:
            logger.error(f"Failed to fetch storage data. Status: {response.status_code}")
            send_discord_message(
                title="Storage API Error",
                description=f"Failed to fetch storage data (HTTP {response.status_code}).",
                color=Colors.RED
            )
    except Exception as e:
        logger.exception(f"Error fetching storage data: {e}")
        send_discord_message(
            title="Storage API Error",
            description="Failed to reach the storage API. Will retry later.",
            color=Colors.RED
        )
    return None


def get_qbittorrent_client():
    """Connects to qBittorrent. Returns None if connection fails."""
    try:
        qb = qbittorrentapi.Client(host=QB_HOST, port=QB_PORT, username=QB_USERNAME, password=QB_PASSWORD)
        qb.auth_log_in()
        return qb
    except Exception as e:
        logger.error(f"Failed to connect to qBittorrent: {e}")
        send_discord_message(
            title="qBittorrent Connection Error",
            description="Could not connect to qBittorrent.",
            color=Colors.RED
        )
        return None


def get_total_torrent_size(qb_client):
    """Returns the total size in GB of all torrents currently in qBittorrent."""
    try:
        torrents = qb_client.torrents_info()
        total_gb = sum(t.size / (1024 ** 3) for t in torrents)
        logger.info(f"Total torrent size: {total_gb:.2f} GB")
        return total_gb
    except Exception as e:
        logger.exception(f"Error retrieving total torrent size: {e}")
        send_discord_message(
            title="qBittorrent Error",
            description="Failed to retrieve total torrent size.",
            color=Colors.RED
        )
        return None
