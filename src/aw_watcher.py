"""
ActivityWatch integration for Focus Guardian.

Provides two access modes:
  1. REST client — talks to aw-server on localhost:5600 (primary, real-time)
  2. Direct DB — reads the peewee SQLite DB (fallback when server is down)

Outputs event dicts compatible with our JSONL buffer format.
"""

import logging
import socket
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from aw_client import ActivityWatchClient
from aw_core.models import Event
from aw_datastore import Datastore
from aw_datastore.storages.peewee import PeeweeStorage

from config import CONFIG

logger = logging.getLogger(__name__)

_AW_CFG = CONFIG.get("activitywatch", {})
_API_URL = _AW_CFG.get("api_url", "http://localhost:5600/api/0")
_POLL_INTERVAL = _AW_CFG.get("poll_interval_seconds", 5)
_HOSTNAME = socket.gethostname()


# ---------------------------------------------------------------------------
# Bucket ID helpers
# ---------------------------------------------------------------------------

def _window_bucket_id():
    return f"aw-watcher-window_{_HOSTNAME}"


def _afk_bucket_id():
    return f"aw-watcher-afk_{_HOSTNAME}"


# ---------------------------------------------------------------------------
# Event → buffer record conversion
# ---------------------------------------------------------------------------

def _event_to_record(event, source_bucket):
    """Convert an aw_core Event to our buffer-compatible dict."""
    ts = event.timestamp
    if isinstance(ts, datetime):
        ts = ts.isoformat()

    duration = event.duration
    if isinstance(duration, timedelta):
        duration = duration.total_seconds()

    return {
        "ts": ts,
        "source": "aw",
        "bucket": source_bucket,
        "duration": duration,
        "data": dict(event.data),
    }


# ---------------------------------------------------------------------------
# REST client (primary path — requires aw-server running)
# ---------------------------------------------------------------------------

class AWRestClient:
    """Thin wrapper over aw_client.ActivityWatchClient for our use case."""

    def __init__(self):
        self._client = None
        self._connected = False

    def _ensure_client(self):
        if self._client is None:
            self._client = ActivityWatchClient(
                client_name="focus-guardian",
                testing=False,
            )

    def is_available(self):
        """Check if aw-server is reachable."""
        self._ensure_client()
        try:
            self._client.get_info()
            self._connected = True
            return True
        except Exception:
            self._connected = False
            return False

    def get_buckets(self):
        """Return dict of bucket_id → bucket metadata."""
        self._ensure_client()
        try:
            return self._client.get_buckets()
        except Exception as e:
            logger.warning("Failed to list buckets via REST: %s", e)
            return {}

    def get_window_events(
        self,
        limit=100,
        start=None,
        end=None,
    ):
        """Fetch recent window events via REST."""
        self._ensure_client()
        bucket_id = _window_bucket_id()
        events = self._client.get_events(bucket_id, limit=limit, start=start, end=end)
        return [_event_to_record(e, bucket_id) for e in events]

    def get_afk_events(
        self,
        limit=100,
        start=None,
        end=None,
    ):
        """Fetch recent AFK status events via REST."""
        self._ensure_client()
        bucket_id = _afk_bucket_id()
        events = self._client.get_events(bucket_id, limit=limit, start=start, end=end)
        return [_event_to_record(e, bucket_id) for e in events]

    def get_recent_activity(self, minutes=10):
        """
        Fetch window + AFK events from the last N minutes.

        Returns a list of buffer-compatible records sorted by timestamp.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=minutes)

        window_records = self.get_window_events(limit=-1, start=start, end=now)
        afk_records = self.get_afk_events(limit=-1, start=start, end=now)

        combined = window_records + afk_records
        combined.sort(key=lambda r: r["ts"])
        return combined


# ---------------------------------------------------------------------------
# Direct DB client (fallback — no server needed)
# ---------------------------------------------------------------------------

class AWDirectClient:
    """Read AW data directly from the peewee SQLite DB."""

    def __init__(self):
        self._ds = None

    def _ensure_datastore(self):
        if self._ds is None:
            try:
                self._ds = Datastore(PeeweeStorage, testing=False)
            except Exception as e:
                logger.error("Failed to open AW database: %s", e)
                raise

    def is_available(self):
        """Check if we can open the AW database."""
        try:
            self._ensure_datastore()
            self._ds.buckets()
            return True
        except Exception:
            return False

    def get_buckets(self):
        """Return list of bucket IDs in the DB."""
        self._ensure_datastore()
        return list(self._ds.buckets().keys())

    def _get_events(self, bucket_id, limit=100, start=None, end=None):
        """Fetch events from a bucket via direct DB read."""
        self._ensure_datastore()
        try:
            bucket = self._ds[bucket_id]
        except KeyError:
            logger.warning("Bucket %s not found in DB", bucket_id)
            return []

        events = bucket.get(limit=limit, starttime=start, endtime=end)
        return [_event_to_record(e, bucket_id) for e in events]

    def get_window_events(self, limit=100, start=None, end=None):
        return self._get_events(_window_bucket_id(), limit, start, end)

    def get_afk_events(self, limit=100, start=None, end=None):
        return self._get_events(_afk_bucket_id(), limit, start, end)

    def get_recent_activity(self, minutes=10):
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=minutes)

        window_records = self.get_window_events(limit=-1, start=start, end=now)
        afk_records = self.get_afk_events(limit=-1, start=start, end=now)

        combined = window_records + afk_records
        combined.sort(key=lambda r: r["ts"])
        return combined


# ---------------------------------------------------------------------------
# Unified client (REST primary, DB fallback)
# ---------------------------------------------------------------------------

class AWClient:
    """
    Unified ActivityWatch client.

    Tries REST first. Falls back to direct DB reads if the server
    is unreachable. Caches the active mode until the next availability
    check.
    """

    def __init__(self):
        self._rest = AWRestClient()
        self._direct = AWDirectClient()
        self._mode = None  # "rest", "direct", or None

    def _active_client(self):
        """Return the currently active client, probing if needed."""
        if self._mode == "rest":
            return self._rest
        if self._mode == "direct":
            return self._direct

        # Probe REST first
        if self._rest.is_available():
            self._mode = "rest"
            logger.info("AW: using REST client (server is up)")
            return self._rest

        # Fall back to direct DB
        if self._direct.is_available():
            self._mode = "direct"
            logger.info("AW: using direct DB client (server unreachable)")
            return self._direct

        logger.warning("AW: neither REST nor direct DB available")
        return None

    def check_connection(self):
        """
        Re-probe availability. Call periodically to detect server
        coming back up (or going down).
        """
        old_mode = self._mode
        self._mode = None
        client = self._active_client()

        if self._mode != old_mode:
            logger.info("AW: mode changed from %s to %s", old_mode, self._mode)

        return client is not None

    def get_recent_activity(self, minutes=10):
        """
        Fetch recent window + AFK events.

        Returns list of buffer-compatible records, or empty list
        if AW is completely unavailable.
        """
        client = self._active_client()
        if client is None:
            return []

        try:
            return client.get_recent_activity(minutes=minutes)
        except Exception as e:
            logger.warning("AW fetch failed (%s), resetting mode: %s", self._mode, e)
            self._mode = None
            return []

    def get_window_events(self, limit=100, start=None, end=None):
        client = self._active_client()
        if client is None:
            return []
        try:
            return client.get_window_events(limit=limit, start=start, end=end)
        except Exception as e:
            logger.warning("AW window fetch failed: %s", e)
            self._mode = None
            return []

    def get_afk_events(self, limit=100, start=None, end=None):
        client = self._active_client()
        if client is None:
            return []
        try:
            return client.get_afk_events(limit=limit, start=start, end=end)
        except Exception as e:
            logger.warning("AW AFK fetch failed: %s", e)
            self._mode = None
            return []


# ---------------------------------------------------------------------------
# CLI — quick test / debug
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Focus Guardian AW client")
    parser.add_argument(
        "--minutes", type=int, default=10,
        help="How many minutes of recent activity to fetch (default: 10)",
    )
    parser.add_argument(
        "--mode", choices=["auto", "rest", "direct"], default="auto",
        help="Force a specific access mode (default: auto)",
    )
    parser.add_argument(
        "--buckets", action="store_true",
        help="List available buckets and exit",
    )
    args = parser.parse_args()

    aw = AWClient()

    if args.mode == "rest":
        aw._mode = "rest"
    elif args.mode == "direct":
        aw._mode = "direct"

    if args.buckets:
        aw.check_connection()
        client = aw._active_client()
        if client is None:
            print("AW is not available")
        else:
            buckets = client.get_buckets()
            if isinstance(buckets, dict):
                for bid, meta in buckets.items():
                    print(f"  {bid}: type={meta.get('type', '?')}")
            else:
                for bid in buckets:
                    print(f"  {bid}")
    else:
        records = aw.get_recent_activity(minutes=args.minutes)
        print(f"Fetched {len(records)} events (last {args.minutes} min):")
        for r in records:
            print(json.dumps(r, ensure_ascii=False))
