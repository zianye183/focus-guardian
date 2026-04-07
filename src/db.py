"""
SQLite storage layer for Focus Guardian.

Provides schema creation, write/read helpers for captures, sessions,
and session summaries, plus FTS5 full-text search over summaries.

All database paths are configurable via config.yaml under the `database` key.
"""

import sqlite3
from pathlib import Path

from config import CONFIG

_DB_CFG = CONFIG.get("database", {})
_PROJECT_ROOT = Path(__file__).parent.parent
_DEFAULT_DB_PATH = str(
    _PROJECT_ROOT / _DB_CFG["db_path"]
    if _DB_CFG.get("db_path")
    else _PROJECT_ROOT / "data" / "focus_guardian.db"
)

_SCHEMA_SQL = """
-- Raw captures (sparse, after normalization + dedup)
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    app TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT,
    text_raw TEXT,
    url TEXT,
    idle_s REAL,
    idle BOOLEAN DEFAULT FALSE,
    filtered TEXT,
    transition BOOLEAN DEFAULT FALSE,
    pid INTEGER,
    window_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_captures_ts ON captures(ts);
CREATE INDEX IF NOT EXISTS idx_captures_app ON captures(app);
CREATE INDEX IF NOT EXISTS idx_captures_window_ts ON captures(window_id, ts);

-- Layout snapshots: which windows are visible on screen.
-- New entry only when the set of visible windows changes.
CREATE TABLE IF NOT EXISTS layout (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    panes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_layout_ts ON layout(ts);

-- Sessions (grouped captures representing one coherent activity)
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    app TEXT NOT NULL,
    title TEXT,
    url TEXT,
    category TEXT,
    capture_count INTEGER,
    duration_s REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(start_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_category ON sessions(category);

-- AI-generated session summaries
CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    summary TEXT NOT NULL,
    summary_json TEXT,
    model TEXT,
    tokens INTEGER,
    created_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON session_summaries(session_id);

-- Full-text search over summaries
CREATE VIRTUAL TABLE IF NOT EXISTS session_summaries_fts USING fts5(
    summary,
    content=session_summaries,
    content_rowid=id
);
"""

# FTS5 triggers to keep the index in sync with the content table.
_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS session_summaries_fts_insert
AFTER INSERT ON session_summaries BEGIN
    INSERT INTO session_summaries_fts(rowid, summary)
    VALUES (new.id, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS session_summaries_fts_delete
AFTER DELETE ON session_summaries BEGIN
    INSERT INTO session_summaries_fts(session_summaries_fts, rowid, summary)
    VALUES ('delete', old.id, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS session_summaries_fts_update
AFTER UPDATE ON session_summaries BEGIN
    INSERT INTO session_summaries_fts(session_summaries_fts, rowid, summary)
    VALUES ('delete', old.id, old.summary);
    INSERT INTO session_summaries_fts(rowid, summary)
    VALUES (new.id, new.summary);
END;
"""


def init_db(db_path: str | None = None) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database at db_path and ensure schema exists.

    Returns a Connection with WAL mode and foreign keys enabled.
    """
    path = db_path or _DEFAULT_DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript(_SCHEMA_SQL)
    conn.executescript(_FTS_TRIGGERS_SQL)
    conn.commit()

    _run_migrations(conn)

    return conn


_MIGRATIONS = [
    # V3: Multi-window capture
    (
        "v3_window_id",
        [
            "ALTER TABLE captures ADD COLUMN window_id INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_captures_window_ts ON captures(window_id, ts)",
        ],
    ),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    """
    Apply pending schema migrations.

    Tracks applied migrations in a simple metadata table so each migration
    runs exactly once, even across fresh schema creation and upgrades.
    """
    import datetime

    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            name TEXT PRIMARY KEY,
            applied_ts TEXT NOT NULL
        )
    """)
    conn.commit()

    applied = {
        row[0]
        for row in conn.execute("SELECT name FROM _migrations").fetchall()
    }

    for name, statements in _MIGRATIONS:
        if name in applied:
            continue
        for stmt in statements:
            try:
                conn.execute(stmt)
            except Exception:
                pass  # Column/index may already exist from fresh schema
        conn.execute(
            "INSERT INTO _migrations (name, applied_ts) VALUES (?, ?)",
            (name, datetime.datetime.now(datetime.timezone.utc).isoformat()),
        )
        conn.commit()


class DB:
    """Context manager for database connections."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path

    def __enter__(self) -> sqlite3.Connection:
        self._conn = init_db(self._db_path)
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._conn.close()
        return False


# ---------------------------------------------------------------------------
# Helpers: row → dict
# ---------------------------------------------------------------------------

def _row_to_dict(cursor: sqlite3.Cursor, row: tuple) -> dict:
    """Convert a sqlite3 row tuple to a dict using column names."""
    keys = [desc[0] for desc in cursor.description]
    return dict(zip(keys, row))


def _fetchall_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """Fetch all rows from a cursor as a list of dicts."""
    keys = [desc[0] for desc in cursor.description]
    return [dict(zip(keys, row)) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Captures
# ---------------------------------------------------------------------------

def create_capture(conn: sqlite3.Connection, capture: dict) -> int:
    """
    Insert a capture record. Returns the new row ID.

    Does not mutate the input dict.
    """
    cursor = conn.execute(
        """
        INSERT INTO captures (ts, app, title, text, text_raw, url, idle_s, idle, filtered, transition, pid, window_id)
        VALUES (:ts, :app, :title, :text, :text_raw, :url, :idle_s, :idle, :filtered, :transition, :pid, :window_id)
        """,
        {
            "ts": capture["ts"],
            "app": capture["app"],
            "title": capture["title"],
            "text": capture.get("text"),
            "text_raw": capture.get("text_raw"),
            "url": capture.get("url"),
            "idle_s": capture.get("idle_s"),
            "idle": capture.get("idle", False),
            "filtered": capture.get("filtered"),
            "transition": capture.get("transition", False),
            "pid": capture.get("pid"),
            "window_id": capture.get("window_id"),
        },
    )
    conn.commit()
    return cursor.lastrowid


def get_captures(
    conn: sqlite3.Connection,
    capture_id: int | None = None,
    limit: int = 500,
) -> list[dict]:
    """
    Retrieve captures. If capture_id is given, return that single row.
    Otherwise return the most recent captures up to limit.
    """
    if capture_id is not None:
        cursor = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,))
    else:
        cursor = conn.execute(
            "SELECT * FROM captures ORDER BY ts DESC LIMIT ?", (limit,)
        )
    return _fetchall_dicts(cursor)


def get_captures_by_time_range(
    conn: sqlite3.Connection,
    start: str,
    end: str,
) -> list[dict]:
    """
    Retrieve captures within [start, end] (inclusive), ordered by ts ASC.
    """
    cursor = conn.execute(
        "SELECT * FROM captures WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
        (start, end),
    )
    return _fetchall_dicts(cursor)


def get_captures_by_app(
    conn: sqlite3.Connection,
    app: str,
    limit: int = 500,
) -> list[dict]:
    """Retrieve captures for a specific app (case-sensitive), ordered by ts DESC."""
    cursor = conn.execute(
        "SELECT * FROM captures WHERE app = ? ORDER BY ts DESC LIMIT ?",
        (app, limit),
    )
    return _fetchall_dicts(cursor)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def create_layout(conn: sqlite3.Connection, layout: dict) -> int:
    """
    Insert a layout snapshot. Returns the new row ID.

    layout["panes"] should be a JSON string of visible window info.
    Does not mutate the input dict.
    """
    cursor = conn.execute(
        """
        INSERT INTO layout (ts, panes)
        VALUES (:ts, :panes)
        """,
        {
            "ts": layout["ts"],
            "panes": layout["panes"],
        },
    )
    conn.commit()
    return cursor.lastrowid


def get_latest_layout(conn: sqlite3.Connection) -> dict | None:
    """Return the most recent layout snapshot, or None if empty."""
    cursor = conn.execute(
        "SELECT * FROM layout ORDER BY ts DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if row is None:
        return None
    keys = [desc[0] for desc in cursor.description]
    return dict(zip(keys, row))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(conn: sqlite3.Connection, session: dict) -> int:
    """
    Insert a session record. Returns the new row ID.

    Does not mutate the input dict.
    """
    cursor = conn.execute(
        """
        INSERT INTO sessions (start_ts, end_ts, app, title, url, category, capture_count, duration_s)
        VALUES (:start_ts, :end_ts, :app, :title, :url, :category, :capture_count, :duration_s)
        """,
        {
            "start_ts": session["start_ts"],
            "end_ts": session["end_ts"],
            "app": session["app"],
            "title": session.get("title"),
            "url": session.get("url"),
            "category": session.get("category"),
            "capture_count": session.get("capture_count"),
            "duration_s": session.get("duration_s"),
        },
    )
    conn.commit()
    return cursor.lastrowid


def get_session(conn: sqlite3.Connection, session_id: int) -> dict | None:
    """Retrieve a single session by ID. Returns None if not found."""
    cursor = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    keys = [desc[0] for desc in cursor.description]
    return dict(zip(keys, row))


def get_sessions_by_time_range(
    conn: sqlite3.Connection,
    start: str,
    end: str,
) -> list[dict]:
    """
    Retrieve sessions where start_ts falls within [start, end], ordered by start_ts ASC.
    """
    cursor = conn.execute(
        "SELECT * FROM sessions WHERE start_ts >= ? AND start_ts <= ? ORDER BY start_ts ASC",
        (start, end),
    )
    return _fetchall_dicts(cursor)


# ---------------------------------------------------------------------------
# Session summaries
# ---------------------------------------------------------------------------

def create_session_summary(conn: sqlite3.Connection, summary: dict) -> int:
    """
    Insert a session summary. Returns the new row ID.

    Raises IntegrityError if session_id does not reference an existing session.
    Does not mutate the input dict.
    """
    cursor = conn.execute(
        """
        INSERT INTO session_summaries (session_id, summary, summary_json, model, tokens, created_ts)
        VALUES (:session_id, :summary, :summary_json, :model, :tokens, :created_ts)
        """,
        {
            "session_id": summary["session_id"],
            "summary": summary["summary"],
            "summary_json": summary.get("summary_json"),
            "model": summary.get("model"),
            "tokens": summary.get("tokens"),
            "created_ts": summary["created_ts"],
        },
    )
    conn.commit()
    return cursor.lastrowid


def get_session_summaries(
    conn: sqlite3.Connection,
    session_id: int,
) -> list[dict]:
    """Retrieve all summaries for a given session, ordered by created_ts ASC."""
    cursor = conn.execute(
        "SELECT * FROM session_summaries WHERE session_id = ? ORDER BY created_ts ASC",
        (session_id,),
    )
    return _fetchall_dicts(cursor)


# ---------------------------------------------------------------------------
# FTS5 search
# ---------------------------------------------------------------------------

def search_summaries(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[dict]:
    """
    Full-text search over session summaries using FTS5.

    Returns dicts with session_id, summary, and FTS rank.
    """
    cursor = conn.execute(
        """
        SELECT
            ss.id,
            ss.session_id,
            ss.summary,
            ss.model,
            ss.created_ts,
            rank
        FROM session_summaries_fts fts
        JOIN session_summaries ss ON ss.id = fts.rowid
        WHERE session_summaries_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    )
    return _fetchall_dicts(cursor)
