"""
Brutal tests for aw_watcher.py.

Every public method, every edge case, every error path.
Mocks all external dependencies (aw_client, aw_core, aw_datastore).
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Stub out heavy external dependencies before importing aw_watcher
# ---------------------------------------------------------------------------

_fake_aw_client = types.ModuleType("aw_client")
_fake_aw_client.ActivityWatchClient = MagicMock
sys.modules["aw_client"] = _fake_aw_client

_fake_aw_core = types.ModuleType("aw_core")
_fake_aw_core_models = types.ModuleType("aw_core.models")
_fake_aw_core_models.Event = MagicMock
sys.modules["aw_core"] = _fake_aw_core
sys.modules["aw_core.models"] = _fake_aw_core_models

_fake_aw_datastore = types.ModuleType("aw_datastore")
_fake_aw_datastore.Datastore = MagicMock
sys.modules["aw_datastore"] = _fake_aw_datastore

_fake_aw_peewee = types.ModuleType("aw_datastore.storages")
_fake_aw_peewee_storage = types.ModuleType("aw_datastore.storages.peewee")
_fake_aw_peewee_storage.PeeweeStorage = MagicMock
sys.modules["aw_datastore.storages"] = _fake_aw_peewee
sys.modules["aw_datastore.storages.peewee"] = _fake_aw_peewee_storage

# Stub config so it doesn't need config.yaml
_fake_config = types.ModuleType("config")
_fake_config.CONFIG = {
    "activitywatch": {
        "api_url": "http://localhost:5600/api/0",
        "poll_interval_seconds": 5,
    }
}
sys.modules["config"] = _fake_config

# Now we can import
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))

import aw_watcher
from aw_watcher import (
    _event_to_record,
    _parse_ts_for_sort,
    _window_bucket_id,
    _afk_bucket_id,
    AWRestClient,
    AWDirectClient,
    AWClient,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL = object()


def _make_event(ts=_SENTINEL, duration=_SENTINEL, data=_SENTINEL):
    """Create a fake AW event object."""
    ev = MagicMock()
    ev.timestamp = ts if ts is not _SENTINEL else datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    ev.duration = duration if duration is not _SENTINEL else timedelta(seconds=30)
    ev.data = data if data is not _SENTINEL else {"app": "Firefox", "title": "GitHub"}
    return ev


# ===========================================================================
# _event_to_record
# ===========================================================================

class TestEventToRecord:
    """Conversion from aw_core Event to buffer dict."""

    def test_basic_conversion(self):
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ev = _make_event(ts=ts, duration=timedelta(seconds=42), data={"app": "Code"})
        rec = _event_to_record(ev, "aw-watcher-window_myhost")

        assert rec["ts"] == ts.isoformat()
        assert rec["source"] == "aw"
        assert rec["bucket"] == "aw-watcher-window_myhost"
        assert rec["duration"] == 42.0
        assert rec["data"] == {"app": "Code"}

    def test_timestamp_already_string(self):
        """If timestamp is already a string, it should pass through."""
        ev = _make_event(ts="2025-06-01T12:00:00+00:00")
        rec = _event_to_record(ev, "bucket")
        assert rec["ts"] == "2025-06-01T12:00:00+00:00"

    def test_duration_already_float(self):
        """If duration is a raw float (not timedelta), it passes through."""
        ev = _make_event(duration=99.5)
        rec = _event_to_record(ev, "bucket")
        assert rec["duration"] == 99.5

    def test_duration_already_int(self):
        ev = _make_event(duration=10)
        rec = _event_to_record(ev, "bucket")
        assert rec["duration"] == 10

    def test_zero_duration(self):
        ev = _make_event(duration=timedelta(seconds=0))
        rec = _event_to_record(ev, "bucket")
        assert rec["duration"] == 0.0

    def test_negative_duration(self):
        """AW can theoretically have negative durations from clock skew."""
        ev = _make_event(duration=timedelta(seconds=-5))
        rec = _event_to_record(ev, "bucket")
        assert rec["duration"] == -5.0

    def test_very_large_duration(self):
        ev = _make_event(duration=timedelta(hours=24))
        rec = _event_to_record(ev, "bucket")
        assert rec["duration"] == 86400.0

    def test_fractional_seconds(self):
        ev = _make_event(duration=timedelta(milliseconds=1500))
        rec = _event_to_record(ev, "bucket")
        assert rec["duration"] == 1.5

    def test_empty_data(self):
        ev = _make_event(data={})
        rec = _event_to_record(ev, "bucket")
        assert rec["data"] == {}

    def test_data_is_copied_not_aliased(self):
        """dict(event.data) should create a new dict, not alias the original."""
        original_data = {"app": "Safari"}
        ev = _make_event(data=original_data)
        rec = _event_to_record(ev, "bucket")
        rec["data"]["app"] = "MUTATED"
        assert original_data["app"] == "Safari"

    def test_none_data_returns_empty_dict(self):
        """event.data=None should be handled gracefully as empty dict."""
        ev = _make_event(data=None)
        rec = _event_to_record(ev, "bucket")
        assert rec["data"] == {}

    def test_naive_datetime_no_tz(self):
        """Naive datetime (no tz) still converts to isoformat."""
        naive_ts = datetime(2025, 6, 1, 12, 0, 0)
        ev = _make_event(ts=naive_ts)
        rec = _event_to_record(ev, "bucket")
        assert rec["ts"] == "2025-06-01T12:00:00"
        # Note: no timezone info — could cause sorting issues later

    def test_data_with_nested_structures(self):
        ev = _make_event(data={"app": "Chrome", "tabs": [1, 2, 3], "meta": {"nested": True}})
        rec = _event_to_record(ev, "bucket")
        assert rec["data"]["tabs"] == [1, 2, 3]
        assert rec["data"]["meta"]["nested"] is True

    def test_data_with_non_string_keys(self):
        """dict() on data with int keys should still work."""
        ev = _make_event(data={1: "one", 2: "two"})
        rec = _event_to_record(ev, "bucket")
        assert rec["data"] == {1: "one", 2: "two"}


# ===========================================================================
# Bucket ID helpers
# ===========================================================================

class TestBucketIds:
    def test_window_bucket_format(self):
        bucket = _window_bucket_id()
        assert bucket.startswith("aw-watcher-window_")
        assert len(bucket) > len("aw-watcher-window_")

    def test_afk_bucket_format(self):
        bucket = _afk_bucket_id()
        assert bucket.startswith("aw-watcher-afk_")
        assert len(bucket) > len("aw-watcher-afk_")

    def test_buckets_use_same_hostname(self):
        win = _window_bucket_id()
        afk = _afk_bucket_id()
        win_host = win.split("_", 1)[1]
        afk_host = afk.split("_", 1)[1]
        assert win_host == afk_host


# ===========================================================================
# AWRestClient
# ===========================================================================

class TestAWRestClient:

    def test_lazy_init_no_client_until_needed(self):
        c = AWRestClient()
        assert c._client is None

    def test_ensure_client_creates_once(self):
        c = AWRestClient()
        with patch("aw_watcher.ActivityWatchClient") as mock_cls:
            mock_cls.return_value = MagicMock()
            c._ensure_client()
            c._ensure_client()  # second call should not create again
            mock_cls.assert_called_once()

    def test_is_available_true(self):
        c = AWRestClient()
        mock_client = MagicMock()
        mock_client.get_info.return_value = {"testing": False}
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            assert c.is_available() is True
            assert c._connected is True

    def test_is_available_false_on_exception(self):
        c = AWRestClient()
        mock_client = MagicMock()
        mock_client.get_info.side_effect = ConnectionError("refused")
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            assert c.is_available() is False
            assert c._connected is False

    def test_get_buckets_success(self):
        c = AWRestClient()
        mock_client = MagicMock()
        mock_client.get_buckets.return_value = {"b1": {"type": "test"}}
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            result = c.get_buckets()
            assert result == {"b1": {"type": "test"}}

    def test_get_buckets_returns_empty_on_error(self):
        c = AWRestClient()
        mock_client = MagicMock()
        mock_client.get_buckets.side_effect = Exception("timeout")
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            result = c.get_buckets()
            assert result == {}

    def test_get_window_events_converts_all(self):
        c = AWRestClient()
        mock_client = MagicMock()
        events = [_make_event(), _make_event()]
        mock_client.get_events.return_value = events
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            records = c.get_window_events(limit=50)
            assert len(records) == 2
            assert all(r["source"] == "aw" for r in records)

    def test_get_window_events_passes_params(self):
        c = AWRestClient()
        mock_client = MagicMock()
        mock_client.get_events.return_value = []
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 2, tzinfo=timezone.utc)
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            c.get_window_events(limit=5, start=start, end=end)
            mock_client.get_events.assert_called_once_with(
                _window_bucket_id(), limit=5, start=start, end=end,
            )

    def test_get_afk_events_uses_afk_bucket(self):
        c = AWRestClient()
        mock_client = MagicMock()
        mock_client.get_events.return_value = []
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            c.get_afk_events()
            call_args = mock_client.get_events.call_args
            assert "afk" in call_args[0][0]

    def test_get_recent_activity_merges_and_sorts(self):
        c = AWRestClient()
        mock_client = MagicMock()

        t1 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2025, 6, 1, 12, 1, 0, tzinfo=timezone.utc)
        t3 = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)

        window_events = [_make_event(ts=t1), _make_event(ts=t2)]
        afk_events = [_make_event(ts=t3)]

        call_count = [0]

        def side_effect(bucket_id, **kwargs):
            call_count[0] += 1
            if "window" in bucket_id:
                return window_events
            return afk_events

        mock_client.get_events.side_effect = side_effect

        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            records = c.get_recent_activity(minutes=5)
            assert len(records) == 3
            # Should be sorted by timestamp string
            timestamps = [r["ts"] for r in records]
            assert timestamps == sorted(timestamps)

    def test_get_recent_activity_empty_when_no_events(self):
        c = AWRestClient()
        mock_client = MagicMock()
        mock_client.get_events.return_value = []
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            records = c.get_recent_activity(minutes=1)
            assert records == []

    def test_get_recent_activity_uses_negative_one_limit(self):
        """get_recent_activity passes limit=-1 to get all events in range."""
        c = AWRestClient()
        mock_client = MagicMock()
        mock_client.get_events.return_value = []
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            c.get_recent_activity(minutes=10)
            for call in mock_client.get_events.call_args_list:
                assert call[1]["limit"] == -1


# ===========================================================================
# AWDirectClient
# ===========================================================================

class TestAWDirectClient:

    def test_lazy_init(self):
        c = AWDirectClient()
        assert c._ds is None

    def test_is_available_true(self):
        c = AWDirectClient()
        mock_ds = MagicMock()
        mock_ds.buckets.return_value = {"b1": {}}
        with patch("aw_watcher.Datastore", return_value=mock_ds):
            assert c.is_available() is True

    def test_is_available_false_on_db_error(self):
        c = AWDirectClient()
        with patch("aw_watcher.Datastore", side_effect=Exception("no db")):
            assert c.is_available() is False

    def test_is_available_false_when_buckets_fails(self):
        c = AWDirectClient()
        mock_ds = MagicMock()
        mock_ds.buckets.side_effect = Exception("corrupt")
        with patch("aw_watcher.Datastore", return_value=mock_ds):
            assert c.is_available() is False

    def test_get_buckets_returns_keys(self):
        c = AWDirectClient()
        mock_ds = MagicMock()
        mock_ds.buckets.return_value = {"b1": {}, "b2": {}}
        with patch("aw_watcher.Datastore", return_value=mock_ds):
            buckets = c.get_buckets()
            assert set(buckets) == {"b1", "b2"}

    def test_get_events_missing_bucket_returns_empty(self):
        c = AWDirectClient()
        mock_ds = MagicMock()
        mock_ds.__getitem__ = MagicMock(side_effect=KeyError("no such bucket"))
        with patch("aw_watcher.Datastore", return_value=mock_ds):
            # Force datastore init
            c._ds = mock_ds
            records = c._get_events("nonexistent_bucket")
            assert records == []

    def test_get_window_events_delegates(self):
        c = AWDirectClient()
        mock_ds = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.get.return_value = [_make_event()]
        mock_ds.__getitem__ = MagicMock(return_value=mock_bucket)
        c._ds = mock_ds

        records = c.get_window_events(limit=10)
        assert len(records) == 1
        mock_ds.__getitem__.assert_called_with(_window_bucket_id())

    def test_get_afk_events_delegates(self):
        c = AWDirectClient()
        mock_ds = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.get.return_value = []
        mock_ds.__getitem__ = MagicMock(return_value=mock_bucket)
        c._ds = mock_ds

        records = c.get_afk_events()
        mock_ds.__getitem__.assert_called_with(_afk_bucket_id())

    def test_get_recent_activity_merges_and_sorts(self):
        c = AWDirectClient()
        mock_ds = MagicMock()

        t1 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2025, 6, 1, 11, 59, 0, tzinfo=timezone.utc)

        def getitem(bucket_id):
            bucket = MagicMock()
            if "window" in bucket_id:
                bucket.get.return_value = [_make_event(ts=t1)]
            else:
                bucket.get.return_value = [_make_event(ts=t2)]
            return bucket

        mock_ds.__getitem__ = MagicMock(side_effect=getitem)
        c._ds = mock_ds

        records = c.get_recent_activity(minutes=5)
        assert len(records) == 2
        assert records[0]["ts"] < records[1]["ts"]

    def test_ensure_datastore_raises_propagates(self):
        """_ensure_datastore raises if DB can't open — callers must handle."""
        c = AWDirectClient()
        with patch("aw_watcher.Datastore", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                c._ensure_datastore()

    def test_get_window_events_when_datastore_init_fails(self):
        """
        BUG AREA: If _ensure_datastore fails inside get_window_events,
        the exception propagates unhandled (no try/except in public method).
        """
        c = AWDirectClient()
        with patch("aw_watcher.Datastore", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                c.get_window_events()


# ===========================================================================
# AWClient (unified — the main test surface)
# ===========================================================================

class TestAWClient:

    def _make_client(self, rest_available=False, direct_available=False):
        """Create AWClient with mocked sub-clients."""
        c = AWClient()
        c._rest = MagicMock(spec=AWRestClient)
        c._direct = MagicMock(spec=AWDirectClient)
        c._rest.is_available.return_value = rest_available
        c._direct.is_available.return_value = direct_available
        return c

    # --- Mode selection ---

    def test_prefers_rest_when_both_available(self):
        c = self._make_client(rest_available=True, direct_available=True)
        client = c._active_client()
        assert c._mode == "rest"
        assert client is c._rest

    def test_falls_back_to_direct_when_rest_down(self):
        c = self._make_client(rest_available=False, direct_available=True)
        client = c._active_client()
        assert c._mode == "direct"
        assert client is c._direct

    def test_returns_none_when_nothing_available(self):
        c = self._make_client(rest_available=False, direct_available=False)
        client = c._active_client()
        assert client is None
        assert c._mode is None

    # --- Mode caching ---

    def test_mode_cached_after_first_probe(self):
        c = self._make_client(rest_available=True)
        c._active_client()
        assert c._mode == "rest"

        # Second call should NOT re-probe
        c._rest.is_available.reset_mock()
        c._active_client()
        c._rest.is_available.assert_not_called()

    def test_cached_rest_mode_returns_rest_client_without_checking(self):
        """
        POTENTIAL BUG: Once mode is cached as 'rest', _active_client returns
        _rest without verifying it's still alive. If the server dies,
        subsequent calls will fail until something resets _mode.
        """
        c = self._make_client(rest_available=True)
        c._active_client()
        assert c._mode == "rest"

        # Simulate server going down — is_available would now return False
        c._rest.is_available.return_value = False

        # But _active_client still returns rest because mode is cached
        client = c._active_client()
        assert client is c._rest  # stale cache!

    # --- check_connection ---

    def test_check_connection_reprobes(self):
        c = self._make_client(rest_available=True)
        c._active_client()
        assert c._mode == "rest"

        # Now REST goes down, direct comes up
        c._rest.is_available.return_value = False
        c._direct.is_available.return_value = True

        result = c.check_connection()
        assert result is True
        assert c._mode == "direct"

    def test_check_connection_returns_false_when_nothing_works(self):
        c = self._make_client(rest_available=False, direct_available=False)
        result = c.check_connection()
        assert result is False

    def test_check_connection_detects_rest_recovery(self):
        """Server comes back up after being down."""
        c = self._make_client(rest_available=False, direct_available=True)
        c._active_client()
        assert c._mode == "direct"

        c._rest.is_available.return_value = True
        c.check_connection()
        assert c._mode == "rest"

    # --- get_recent_activity ---

    def test_get_recent_activity_delegates_to_active_client(self):
        c = self._make_client(rest_available=True)
        c._rest.get_recent_activity.return_value = [{"ts": "t1"}]

        result = c.get_recent_activity(minutes=5)
        assert result == [{"ts": "t1"}]
        c._rest.get_recent_activity.assert_called_once_with(minutes=5)

    def test_get_recent_activity_returns_empty_when_unavailable(self):
        c = self._make_client(rest_available=False, direct_available=False)
        result = c.get_recent_activity()
        assert result == []

    def test_get_recent_activity_resets_mode_on_exception(self):
        """If the active client throws, mode should be reset for next call."""
        c = self._make_client(rest_available=True)
        c._rest.get_recent_activity.side_effect = Exception("connection lost")

        result = c.get_recent_activity()
        assert result == []
        assert c._mode is None  # reset!

    def test_get_recent_activity_recovers_after_reset(self):
        """After mode reset, next call should re-probe and potentially switch."""
        c = self._make_client(rest_available=True, direct_available=True)
        c._rest.get_recent_activity.side_effect = Exception("boom")

        # First call fails, resets mode
        c.get_recent_activity()
        assert c._mode is None

        # Now REST is down, direct is up
        c._rest.is_available.return_value = False
        c._rest.get_recent_activity.side_effect = None
        c._direct.get_recent_activity.return_value = [{"ts": "t1"}]

        result = c.get_recent_activity()
        assert c._mode == "direct"
        assert result == [{"ts": "t1"}]

    # --- get_window_events ---

    def test_get_window_events_delegates(self):
        c = self._make_client(rest_available=True)
        c._rest.get_window_events.return_value = [{"ts": "t"}]

        result = c.get_window_events(limit=10)
        assert len(result) == 1
        c._rest.get_window_events.assert_called_once_with(limit=10, start=None, end=None)

    def test_get_window_events_empty_when_unavailable(self):
        c = self._make_client()
        assert c.get_window_events() == []

    def test_get_window_events_resets_mode_on_error(self):
        c = self._make_client(rest_available=True)
        c._rest.get_window_events.side_effect = Exception("timeout")

        result = c.get_window_events()
        assert result == []
        assert c._mode is None

    # --- get_afk_events ---

    def test_get_afk_events_delegates(self):
        c = self._make_client(direct_available=True)
        # REST unavailable, falls to direct
        c._direct.get_afk_events.return_value = [{"ts": "t"}]

        result = c.get_afk_events(limit=5)
        assert len(result) == 1

    def test_get_afk_events_resets_mode_on_error(self):
        c = self._make_client(rest_available=True)
        c._rest.get_afk_events.side_effect = RuntimeError("oops")

        result = c.get_afk_events()
        assert result == []
        assert c._mode is None

    # --- Concurrency-like scenarios ---

    def test_rapid_mode_flapping(self):
        """Simulate REST going up/down/up rapidly."""
        c = self._make_client(rest_available=True, direct_available=True)

        # Start on REST
        c._active_client()
        assert c._mode == "rest"

        # REST dies — force reset
        c._mode = None
        c._rest.is_available.return_value = False
        c._active_client()
        assert c._mode == "direct"

        # REST comes back
        c._mode = None
        c._rest.is_available.return_value = True
        c._active_client()
        assert c._mode == "rest"

    def test_default_minutes_parameter(self):
        """get_recent_activity defaults to 10 minutes."""
        c = self._make_client(rest_available=True)
        c._rest.get_recent_activity.return_value = []

        c.get_recent_activity()
        c._rest.get_recent_activity.assert_called_once_with(minutes=10)

    def test_start_end_params_forwarded_to_window_events(self):
        c = self._make_client(rest_available=True)
        c._rest.get_window_events.return_value = []
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 2, tzinfo=timezone.utc)

        c.get_window_events(limit=50, start=start, end=end)
        c._rest.get_window_events.assert_called_once_with(
            limit=50, start=start, end=end,
        )


# ===========================================================================
# Sorting edge cases (ISO string sort)
# ===========================================================================

class TestSortingEdgeCases:
    """
    get_recent_activity sorts by r["ts"] which is an ISO string.
    This works for UTC datetimes but has edge cases.
    """

    def test_parse_ts_z_and_offset_equal(self):
        """Z and +00:00 should parse to the same datetime."""
        t1 = _parse_ts_for_sort("2025-06-01T12:00:00Z")
        t2 = _parse_ts_for_sort("2025-06-01T12:00:00+00:00")
        assert t1 == t2

    def test_sort_with_z_suffix_vs_offset_now_correct(self):
        """Mixed Z and +00:00 formats now sort correctly by actual time."""
        r_earlier = {"ts": "2025-06-01T11:59:59Z", "source": "aw"}
        r_later = {"ts": "2025-06-01T12:00:00+00:00", "source": "aw"}

        combined = [r_later, r_earlier]
        combined.sort(key=lambda r: _parse_ts_for_sort(r["ts"]))
        assert combined[0]["ts"] == "2025-06-01T11:59:59Z"
        assert combined[1]["ts"] == "2025-06-01T12:00:00+00:00"

    def test_parse_ts_naive(self):
        """Naive timestamps still parse (for backwards compat)."""
        t = _parse_ts_for_sort("2025-06-01T12:00:00")
        assert t == datetime(2025, 6, 1, 12, 0, 0)


# ===========================================================================
# Module-level constants
# ===========================================================================

class TestModuleConstants:

    def test_api_url_default(self):
        assert aw_watcher._API_URL == "http://localhost:5600/api/0"

    def test_poll_interval_default(self):
        assert aw_watcher._POLL_INTERVAL == 5

    def test_hostname_is_string(self):
        assert isinstance(aw_watcher._HOSTNAME, str)
        assert len(aw_watcher._HOSTNAME) > 0


# ===========================================================================
# Integration-style: AWRestClient with realistic event flow
# ===========================================================================

class TestRestClientIntegration:
    """More realistic scenarios with event sequences."""

    def test_many_events_all_converted(self):
        c = AWRestClient()
        mock_client = MagicMock()
        events = [_make_event(data={"app": f"App{i}"}) for i in range(100)]
        mock_client.get_events.return_value = events
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            records = c.get_window_events(limit=100)
            assert len(records) == 100
            apps = {r["data"]["app"] for r in records}
            assert len(apps) == 100

    def test_events_with_unicode_data(self):
        c = AWRestClient()
        mock_client = MagicMock()
        events = [_make_event(data={"app": "Safari", "title": "日本語テスト 🎉"})]
        mock_client.get_events.return_value = events
        with patch("aw_watcher.ActivityWatchClient", return_value=mock_client):
            records = c.get_window_events(limit=1)
            assert records[0]["data"]["title"] == "日本語テスト 🎉"

    def test_events_with_special_characters_in_bucket(self):
        """Bucket IDs with special chars in hostname."""
        ev = _make_event()
        rec = _event_to_record(ev, "aw-watcher-window_my-host.local")
        assert rec["bucket"] == "aw-watcher-window_my-host.local"


# ===========================================================================
# AWClient error cascade
# ===========================================================================

class TestAWClientErrorCascade:
    """Test that errors in one mode don't poison the other."""

    def test_rest_error_then_direct_works(self):
        c = AWClient()
        c._rest = MagicMock(spec=AWRestClient)
        c._direct = MagicMock(spec=AWDirectClient)

        c._rest.is_available.return_value = True
        c._rest.get_recent_activity.side_effect = Exception("REST dead")
        c._direct.is_available.return_value = True
        c._direct.get_recent_activity.return_value = [{"ts": "t"}]

        # First call: uses REST (cached), fails, resets mode
        result1 = c.get_recent_activity()
        assert result1 == []
        assert c._mode is None

        # Second call: re-probes, REST fails avail check too now
        c._rest.is_available.return_value = False
        result2 = c.get_recent_activity()
        assert result2 == [{"ts": "t"}]
        assert c._mode == "direct"

    def test_both_modes_fail_returns_empty(self):
        c = AWClient()
        c._rest = MagicMock(spec=AWRestClient)
        c._direct = MagicMock(spec=AWDirectClient)
        c._rest.is_available.return_value = False
        c._direct.is_available.return_value = False

        assert c.get_recent_activity() == []
        assert c.get_window_events() == []
        assert c.get_afk_events() == []

    def test_repeated_failures_dont_accumulate_state(self):
        """Multiple failures should not leave stale state."""
        c = AWClient()
        c._rest = MagicMock(spec=AWRestClient)
        c._direct = MagicMock(spec=AWDirectClient)
        c._rest.is_available.return_value = True
        c._direct.is_available.return_value = False

        # Fail 5 times
        c._rest.get_recent_activity.side_effect = Exception("fail")
        for _ in range(5):
            c.get_recent_activity()

        # Mode should still be None (reset each time)
        assert c._mode is None
