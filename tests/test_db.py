"""
Tests for the db module — Focus Guardian's SQLite storage layer.

Covers: schema creation, CRUD for captures/sessions/summaries,
FTS5 full-text search, time-range queries, edge cases, and data integrity.
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from db import (
    DB,
    create_capture,
    create_session,
    create_session_summary,
    get_captures,
    get_captures_by_app,
    get_captures_by_time_range,
    get_session,
    get_session_summaries,
    get_sessions_by_time_range,
    init_db,
    search_summaries,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Provide a fresh temp DB path for each test."""
    return tmp_path / "test_focus.db"


@pytest.fixture
def db(db_path):
    """Initialize a fresh database and return the connection."""
    conn = init_db(str(db_path))
    yield conn
    conn.close()


def _make_capture(**overrides):
    """Build a capture dict with sensible defaults."""
    defaults = {
        "ts": "2026-04-05T10:00:00+00:00",
        "app": "Arc",
        "title": "Google Search",
        "text": "search results for focus guardian",
        "text_raw": None,
        "url": "https://google.com/search?q=focus+guardian",
        "idle_s": 0.5,
        "idle": False,
        "filtered": None,
        "transition": False,
        "pid": 1234,
    }
    return {**defaults, **overrides}


def _make_session(**overrides):
    """Build a session dict with sensible defaults."""
    defaults = {
        "start_ts": "2026-04-05T10:00:00+00:00",
        "end_ts": "2026-04-05T10:15:00+00:00",
        "app": "Arc",
        "title": "Google Search",
        "url": "https://google.com",
        "category": "work",
        "capture_count": 12,
        "duration_s": 900.0,
    }
    return {**defaults, **overrides}


def _make_summary(session_id, **overrides):
    """Build a session_summary dict with sensible defaults."""
    defaults = {
        "session_id": session_id,
        "summary": "User searched Google for focus guardian project info",
        "summary_json": None,
        "model": "claude-haiku-4-5-20251001",
        "tokens": 42,
        "created_ts": "2026-04-05T10:16:00+00:00",
    }
    return {**defaults, **overrides}


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

class TestSchemaCreation:
    def test_creates_db_file(self, db_path):
        conn = init_db(str(db_path))
        conn.close()
        assert db_path.exists()

    def test_creates_all_tables(self, db):
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        assert "captures" in tables
        assert "sessions" in tables
        assert "session_summaries" in tables

    def test_creates_fts5_table(self, db):
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'"
        )
        fts_tables = [row[0] for row in cursor.fetchall()]
        assert any("session_summaries_fts" in t for t in fts_tables)

    def test_creates_indexes(self, db):
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_captures_ts" in indexes
        assert "idx_captures_app" in indexes
        assert "idx_sessions_ts" in indexes
        assert "idx_sessions_category" in indexes
        assert "idx_summaries_session" in indexes

    def test_idempotent_init(self, db_path):
        """Calling init_db twice on the same file should not error."""
        conn1 = init_db(str(db_path))
        conn1.close()
        conn2 = init_db(str(db_path))
        conn2.close()

    def test_wal_mode_enabled(self, db):
        """WAL mode should be on for concurrent read/write performance."""
        cursor = db.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_enabled(self, db):
        cursor = db.execute("PRAGMA foreign_keys")
        assert cursor.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Captures: write
# ---------------------------------------------------------------------------

class TestCreateCapture:
    def test_insert_and_return_id(self, db):
        cap = _make_capture()
        cap_id = create_capture(db, cap)
        assert isinstance(cap_id, int)
        assert cap_id >= 1

    def test_sequential_ids(self, db):
        id1 = create_capture(db, _make_capture(ts="2026-04-05T10:00:00+00:00"))
        id2 = create_capture(db, _make_capture(ts="2026-04-05T10:00:03+00:00"))
        assert id2 == id1 + 1

    def test_all_fields_stored(self, db):
        cap = _make_capture(
            text_raw="raw text before normalization",
            idle=True,
            filtered="app_blocked",
            transition=True,
        )
        cap_id = create_capture(db, cap)
        row = db.execute("SELECT * FROM captures WHERE id = ?", (cap_id,)).fetchone()
        assert row is not None
        # Check each field by name
        keys = [desc[0] for desc in db.execute("SELECT * FROM captures").description]
        row_dict = dict(zip(keys, row))
        assert row_dict["ts"] == cap["ts"]
        assert row_dict["app"] == cap["app"]
        assert row_dict["title"] == cap["title"]
        assert row_dict["text"] == cap["text"]
        assert row_dict["text_raw"] == cap["text_raw"]
        assert row_dict["url"] == cap["url"]
        assert row_dict["idle_s"] == cap["idle_s"]
        assert row_dict["idle"] == 1  # SQLite stores bool as int
        assert row_dict["filtered"] == "app_blocked"
        assert row_dict["transition"] == 1  # SQLite stores bool as int
        assert row_dict["pid"] == cap["pid"]

    def test_nullable_fields(self, db):
        """text, text_raw, url, idle_s, filtered, pid can all be None."""
        cap = _make_capture(text=None, text_raw=None, url=None, idle_s=None, filtered=None, pid=None)
        cap_id = create_capture(db, cap)
        row = db.execute("SELECT text, text_raw, url, idle_s, filtered, pid FROM captures WHERE id = ?", (cap_id,)).fetchone()
        assert row == (None, None, None, None, None, None)

    def test_required_fields_enforced(self, db):
        """ts, app, title are NOT NULL — should fail if missing."""
        with pytest.raises(Exception):
            create_capture(db, {"ts": None, "app": "Arc", "title": "Page"})
        with pytest.raises(Exception):
            create_capture(db, {"ts": "2026-04-05T10:00:00+00:00", "app": None, "title": "Page"})
        with pytest.raises(Exception):
            create_capture(db, {"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": None})

    def test_does_not_mutate_input(self, db):
        cap = _make_capture()
        original = cap.copy()
        create_capture(db, cap)
        assert cap == original

    def test_unicode_text(self, db):
        """Chinese, emoji, and mixed content should round-trip."""
        cap = _make_capture(
            text="用户正在编辑笔记 📝 Focus Guardian — AI伴侣设计文档",
            title="Obsidian — 设计文档",
        )
        cap_id = create_capture(db, cap)
        row = db.execute("SELECT text, title FROM captures WHERE id = ?", (cap_id,)).fetchone()
        assert row[0] == cap["text"]
        assert row[1] == cap["title"]

    def test_very_long_text(self, db):
        """Should handle text up to max_text_length (20K chars)."""
        long_text = "x" * 20000
        cap = _make_capture(text=long_text)
        cap_id = create_capture(db, cap)
        row = db.execute("SELECT text FROM captures WHERE id = ?", (cap_id,)).fetchone()
        assert len(row[0]) == 20000

    def test_filtered_field_stored(self, db):
        """filtered column stores the filter reason (app_blocked, private_window, sensitive_page)."""
        cap = _make_capture(filtered="app_blocked")
        cap_id = create_capture(db, cap)
        row = db.execute("SELECT filtered FROM captures WHERE id = ?", (cap_id,)).fetchone()
        assert row[0] == "app_blocked"

    def test_transition_field_stored(self, db):
        """transition column stores whether this capture was an app-switch transition."""
        cap = _make_capture(transition=True)
        cap_id = create_capture(db, cap)
        row = db.execute("SELECT transition FROM captures WHERE id = ?", (cap_id,)).fetchone()
        assert row[0] == 1  # SQLite stores bool as int


# ---------------------------------------------------------------------------
# Captures: read
# ---------------------------------------------------------------------------

class TestGetCaptures:
    def test_get_by_id(self, db):
        cap = _make_capture()
        cap_id = create_capture(db, cap)
        rows = get_captures(db, capture_id=cap_id)
        assert len(rows) == 1
        assert rows[0]["id"] == cap_id
        assert rows[0]["app"] == "Arc"

    def test_returns_dicts(self, db):
        create_capture(db, _make_capture())
        rows = get_captures(db, limit=1)
        assert isinstance(rows[0], dict)
        assert "ts" in rows[0]
        assert "app" in rows[0]

    def test_limit(self, db):
        for i in range(10):
            create_capture(db, _make_capture(ts=f"2026-04-05T10:00:{i:02d}+00:00"))
        rows = get_captures(db, limit=3)
        assert len(rows) == 3

    def test_empty_db_returns_empty(self, db):
        rows = get_captures(db)
        assert rows == []


class TestGetCapturesByTimeRange:
    def test_basic_range(self, db):
        create_capture(db, _make_capture(ts="2026-04-05T09:00:00+00:00"))
        create_capture(db, _make_capture(ts="2026-04-05T10:00:00+00:00"))
        create_capture(db, _make_capture(ts="2026-04-05T11:00:00+00:00"))

        rows = get_captures_by_time_range(
            db,
            start="2026-04-05T09:30:00+00:00",
            end="2026-04-05T10:30:00+00:00",
        )
        assert len(rows) == 1
        assert rows[0]["ts"] == "2026-04-05T10:00:00+00:00"

    def test_inclusive_boundaries(self, db):
        create_capture(db, _make_capture(ts="2026-04-05T10:00:00+00:00"))
        rows = get_captures_by_time_range(
            db,
            start="2026-04-05T10:00:00+00:00",
            end="2026-04-05T10:00:00+00:00",
        )
        assert len(rows) == 1

    def test_empty_range(self, db):
        create_capture(db, _make_capture(ts="2026-04-05T10:00:00+00:00"))
        rows = get_captures_by_time_range(
            db,
            start="2026-04-05T11:00:00+00:00",
            end="2026-04-05T12:00:00+00:00",
        )
        assert rows == []

    def test_ordered_by_ts(self, db):
        create_capture(db, _make_capture(ts="2026-04-05T10:02:00+00:00"))
        create_capture(db, _make_capture(ts="2026-04-05T10:00:00+00:00"))
        create_capture(db, _make_capture(ts="2026-04-05T10:01:00+00:00"))

        rows = get_captures_by_time_range(
            db,
            start="2026-04-05T09:00:00+00:00",
            end="2026-04-05T11:00:00+00:00",
        )
        timestamps = [r["ts"] for r in rows]
        assert timestamps == sorted(timestamps)


class TestGetCapturesByApp:
    def test_filter_single_app(self, db):
        create_capture(db, _make_capture(app="Arc"))
        create_capture(db, _make_capture(app="Obsidian"))
        create_capture(db, _make_capture(app="Arc", ts="2026-04-05T10:00:03+00:00"))

        rows = get_captures_by_app(db, "Arc")
        assert len(rows) == 2
        assert all(r["app"] == "Arc" for r in rows)

    def test_app_not_found(self, db):
        create_capture(db, _make_capture(app="Arc"))
        rows = get_captures_by_app(db, "Nonexistent")
        assert rows == []

    def test_case_sensitive(self, db):
        create_capture(db, _make_capture(app="Arc"))
        rows = get_captures_by_app(db, "arc")
        assert rows == []


# ---------------------------------------------------------------------------
# Sessions: write + read
# ---------------------------------------------------------------------------

class TestSessions:
    def test_create_and_read(self, db):
        sess = _make_session()
        sess_id = create_session(db, sess)
        assert isinstance(sess_id, int)

        result = get_session(db, sess_id)
        assert result is not None
        assert result["app"] == "Arc"
        assert result["category"] == "work"
        assert result["duration_s"] == 900.0

    def test_all_fields_stored(self, db):
        sess = _make_session()
        sess_id = create_session(db, sess)
        result = get_session(db, sess_id)
        for key in ("start_ts", "end_ts", "app", "title", "url", "category", "capture_count", "duration_s"):
            assert result[key] == sess[key], f"Mismatch on {key}: {result[key]} != {sess[key]}"

    def test_nullable_fields(self, db):
        sess = _make_session(title=None, url=None, category=None)
        sess_id = create_session(db, sess)
        result = get_session(db, sess_id)
        assert result["title"] is None
        assert result["url"] is None
        assert result["category"] is None

    def test_time_range_query(self, db):
        create_session(db, _make_session(
            start_ts="2026-04-05T09:00:00+00:00",
            end_ts="2026-04-05T09:30:00+00:00",
        ))
        create_session(db, _make_session(
            start_ts="2026-04-05T10:00:00+00:00",
            end_ts="2026-04-05T10:30:00+00:00",
        ))
        create_session(db, _make_session(
            start_ts="2026-04-05T11:00:00+00:00",
            end_ts="2026-04-05T11:30:00+00:00",
        ))

        rows = get_sessions_by_time_range(
            db,
            start="2026-04-05T09:45:00+00:00",
            end="2026-04-05T10:45:00+00:00",
        )
        assert len(rows) == 1
        assert rows[0]["start_ts"] == "2026-04-05T10:00:00+00:00"

    def test_sessions_ordered_by_start(self, db):
        create_session(db, _make_session(start_ts="2026-04-05T11:00:00+00:00", end_ts="2026-04-05T11:30:00+00:00"))
        create_session(db, _make_session(start_ts="2026-04-05T09:00:00+00:00", end_ts="2026-04-05T09:30:00+00:00"))

        rows = get_sessions_by_time_range(db, start="2026-04-05T08:00:00+00:00", end="2026-04-05T12:00:00+00:00")
        assert rows[0]["start_ts"] < rows[1]["start_ts"]

    def test_does_not_mutate_input(self, db):
        sess = _make_session()
        original = sess.copy()
        create_session(db, sess)
        assert sess == original

    def test_get_nonexistent_session(self, db):
        result = get_session(db, 9999)
        assert result is None


# ---------------------------------------------------------------------------
# Session summaries: write + read + FTS5
# ---------------------------------------------------------------------------

class TestSessionSummaries:
    def test_create_and_read(self, db):
        sess_id = create_session(db, _make_session())
        summary = _make_summary(sess_id)
        sum_id = create_session_summary(db, summary)
        assert isinstance(sum_id, int)

        results = get_session_summaries(db, sess_id)
        assert len(results) == 1
        assert results[0]["summary"] == summary["summary"]
        assert results[0]["model"] == "claude-haiku-4-5-20251001"

    def test_multiple_summaries_per_session(self, db):
        """A session can have multiple summaries (e.g., regenerated)."""
        sess_id = create_session(db, _make_session())
        create_session_summary(db, _make_summary(sess_id, summary="First summary"))
        create_session_summary(db, _make_summary(sess_id, summary="Second summary", created_ts="2026-04-05T10:17:00+00:00"))

        results = get_session_summaries(db, sess_id)
        assert len(results) == 2

    def test_foreign_key_enforced(self, db):
        """Cannot create summary for nonexistent session."""
        with pytest.raises(Exception):
            create_session_summary(db, _make_summary(session_id=9999))

    def test_nullable_fields(self, db):
        sess_id = create_session(db, _make_session())
        summary = _make_summary(sess_id, summary_json=None, tokens=None)
        sum_id = create_session_summary(db, summary)
        results = get_session_summaries(db, sess_id)
        assert results[0]["summary_json"] is None
        assert results[0]["tokens"] is None

    def test_does_not_mutate_input(self, db):
        sess_id = create_session(db, _make_session())
        summary = _make_summary(sess_id)
        original = summary.copy()
        create_session_summary(db, summary)
        assert summary == original


# ---------------------------------------------------------------------------
# FTS5 full-text search
# ---------------------------------------------------------------------------

class TestFTS5Search:
    def _seed_summaries(self, db):
        """Insert several sessions with varied summaries for search testing."""
        sessions_data = [
            ("work", "User researched machine learning papers on arXiv"),
            ("work", "Wrote Python code for the focus guardian database module"),
            ("communication", "Video call with Alice discussing project timeline"),
            ("distraction", "Watched YouTube videos about cooking pasta recipes"),
            ("work", "Edited Obsidian notes about AI companion design document"),
        ]
        for category, summary_text in sessions_data:
            sess_id = create_session(db, _make_session(category=category))
            create_session_summary(db, _make_summary(sess_id, summary=summary_text))

    def test_basic_keyword_search(self, db):
        self._seed_summaries(db)
        results = search_summaries(db, "Python")
        assert len(results) >= 1
        assert any("Python" in r["summary"] for r in results)

    def test_multi_word_search(self, db):
        self._seed_summaries(db)
        results = search_summaries(db, "machine learning")
        assert len(results) >= 1

    def test_no_results(self, db):
        self._seed_summaries(db)
        results = search_summaries(db, "cryptocurrency blockchain")
        assert results == []

    def test_partial_word_match(self, db):
        """FTS5 should match prefix queries with *."""
        self._seed_summaries(db)
        results = search_summaries(db, "cook*")
        assert len(results) >= 1

    def test_case_insensitive(self, db):
        self._seed_summaries(db)
        results_lower = search_summaries(db, "python")
        results_upper = search_summaries(db, "PYTHON")
        assert len(results_lower) == len(results_upper)

    def test_returns_session_info(self, db):
        """Search results should include session metadata, not just summary text."""
        self._seed_summaries(db)
        results = search_summaries(db, "Python")
        assert len(results) >= 1
        result = results[0]
        assert "summary" in result
        assert "session_id" in result


# ---------------------------------------------------------------------------
# DB context manager
# ---------------------------------------------------------------------------

class TestDBContextManager:
    def test_context_manager(self, db_path):
        """DB class should work as a context manager."""
        with DB(str(db_path)) as conn:
            create_capture(conn, _make_capture())
            rows = get_captures(conn)
            assert len(rows) == 1

    def test_context_manager_closes_connection(self, db_path):
        """Connection should be closed after exiting context."""
        with DB(str(db_path)) as conn:
            pass
        # Attempting to use closed connection should fail
        with pytest.raises(Exception):
            conn.execute("SELECT 1")


# ---------------------------------------------------------------------------
# Edge cases and robustness
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_bulk_insert_captures(self, db):
        """Insert 1000 captures — should handle without issue."""
        for i in range(1000):
            ts = f"2026-04-05T{i // 3600:02d}:{(i % 3600) // 60:02d}:{i % 60:02d}+00:00"
            create_capture(db, _make_capture(ts=ts))

        rows = get_captures(db, limit=2000)
        assert len(rows) == 1000

    def test_special_characters_in_text(self, db):
        """SQL injection attempt and special chars should be safe."""
        malicious = "'; DROP TABLE captures; --"
        cap = _make_capture(text=malicious)
        cap_id = create_capture(db, cap)
        row = get_captures(db, capture_id=cap_id)
        assert row[0]["text"] == malicious

    def test_empty_string_vs_null(self, db):
        """Empty string and None should be distinct."""
        id1 = create_capture(db, _make_capture(text=""))
        id2 = create_capture(db, _make_capture(text=None, ts="2026-04-05T10:00:01+00:00"))
        row1 = get_captures(db, capture_id=id1)
        row2 = get_captures(db, capture_id=id2)
        assert row1[0]["text"] == ""
        assert row2[0]["text"] is None

    def test_concurrent_reads_during_write(self, db_path):
        """WAL mode should allow concurrent reads while writing."""
        conn1 = init_db(str(db_path))
        conn2 = init_db(str(db_path))

        create_capture(conn1, _make_capture())
        # conn2 should be able to read even though conn1 hasn't committed
        # (WAL mode allows this)
        rows = get_captures(conn2)
        # The exact behavior depends on autocommit settings,
        # but this should not raise an error
        assert isinstance(rows, list)

        conn1.close()
        conn2.close()
