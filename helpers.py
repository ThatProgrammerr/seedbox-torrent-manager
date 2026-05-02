from enum import Enum
import os
import logging
import requests
import qbittorrentapi

from pathlib import Path
from dotenv import load_dotenv

from utilities import Colors, send_discord_message, sleep

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

API_URL = os.getenv("API_URL", "")
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "")

QB_HOST = os.getenv("QB_HOST", "localhost")
QB_PORT = int(os.getenv("QB_PORT", 8080))
QB_USERNAME = os.getenv("QB_USERNAME", "admin")
QB_PASSWORD = os.getenv("QB_PASSWORD", "adminadmin")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def get_storage_data():
    """
    Fetches storage and traffic statistics from the seedbox provider API.
    Returns None if API_URL is not configured or the request fails.
    """
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
