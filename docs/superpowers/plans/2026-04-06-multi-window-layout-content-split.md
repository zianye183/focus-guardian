# Multi-Window Layout + Content Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace single-frontmost-app capture with multi-window capture using two independent data streams — a `layout` table (which windows are visible) and per-window content in `captures` (deduped per `window_id`).

**Architecture:** `get_visible_windows()` (already built in `src/visible_windows.py`) provides all on-screen windows with matched AX elements. The capture loop iterates over these, writing per-window content to `captures` with a new `window_id` column, and writing layout snapshots only when the visible window set changes. Dedup compares per-window instead of globally. The NSWorkspace observer, AXObserver, and osascript subprocess are removed — layout changes replace transition detection.

**Tech Stack:** Python 3.12, pyobjc (Quartz/ApplicationServices), SQLite with WAL mode, pytest

**Design doc:** `docs/design-v3-multi-app-capture.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/db.py` | Modify | Add `layout` table, `window_id` column to `captures`, new helpers |
| `src/screen_reader.py` | Modify | Extract `capture_window()`, rewrite `run_continuous()` loop, remove old detection |
| `src/visible_windows.py` | No change | Already built — `get_visible_windows()` returns all on-screen windows |
| `src/ax_observer.py` | Delete | No longer needed — layout table replaces window-switch detection |
| `tests/test_db.py` | Modify | Add tests for `layout` table, `window_id` in captures |
| `tests/test_capture_window.py` | Create | Tests for the extracted `capture_window()` function |
| `tests/test_ax_observer.py` | Delete | Module being removed |

---

### Task 1: Add `layout` table to database schema

**Files:**
- Modify: `src/db.py:23-75` (schema SQL)
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for layout table**

Add to `tests/test_db.py`:

```python
# At the top, update imports:
from db import (
    DB,
    create_capture,
    create_layout,
    create_session,
    create_session_summary,
    get_captures,
    get_captures_by_app,
    get_captures_by_time_range,
    get_latest_layout,
    get_session,
    get_session_summaries,
    get_sessions_by_time_range,
    init_db,
    search_summaries,
)


class TestLayoutTable:
    def test_layout_table_exists(self, db):
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='layout'"
        )
        assert cursor.fetchone() is not None

    def test_layout_index_exists(self, db):
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_layout_ts'"
        )
        assert cursor.fetchone() is not None

    def test_create_layout(self, db):
        layout = {
            "ts": "2026-04-06T10:00:00+00:00",
            "panes": '[{"window_id": 123, "app": "Arc", "pid": 456, "title": "Google", "bounds": {"X": 0, "Y": 39, "Width": 855, "Height": 1068}}]',
        }
        row_id = create_layout(db, layout)
        assert isinstance(row_id, int)
        assert row_id >= 1

    def test_get_latest_layout(self, db):
        create_layout(db, {
            "ts": "2026-04-06T10:00:00+00:00",
            "panes": '[{"window_id": 100, "app": "Arc"}]',
        })
        create_layout(db, {
            "ts": "2026-04-06T10:05:00+00:00",
            "panes": '[{"window_id": 200, "app": "Obsidian"}]',
        })
        result = get_latest_layout(db)
        assert result is not None
        assert result["ts"] == "2026-04-06T10:05:00+00:00"
        assert "Obsidian" in result["panes"]

    def test_get_latest_layout_empty_db(self, db):
        result = get_latest_layout(db)
        assert result is None

    def test_does_not_mutate_input(self, db):
        layout = {
            "ts": "2026-04-06T10:00:00+00:00",
            "panes": '[{"window_id": 123, "app": "Arc"}]',
        }
        original = layout.copy()
        create_layout(db, layout)
        assert layout == original
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/alanye/focus-guardian && python -m pytest tests/test_db.py::TestLayoutTable -v`
Expected: FAIL — `ImportError: cannot import name 'create_layout'`

- [ ] **Step 3: Add layout table to schema and write helpers**

In `src/db.py`, add to `_SCHEMA_SQL` (after the captures table block, before sessions):

```sql
-- Layout snapshots: which windows are visible on screen.
-- New entry only when the set of visible windows changes.
CREATE TABLE IF NOT EXISTS layout (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    panes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_layout_ts ON layout(ts);
```

Add these functions after the captures section (after `get_captures_by_app`):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/alanye/focus-guardian && python -m pytest tests/test_db.py::TestLayoutTable -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Run full db test suite for regressions**

Run: `cd /Users/alanye/focus-guardian && python -m pytest tests/test_db.py -v`
Expected: All existing tests still PASS

- [ ] **Step 6: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat: add layout table to database schema"
```

---

### Task 2: Add `window_id` column to captures table

**Files:**
- Modify: `src/db.py:23-75` (schema SQL), `src/db.py:158-184` (`create_capture`)
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for window_id**

Add to `tests/test_db.py`:

```python
class TestCaptureWindowId:
    def test_window_id_stored(self, db):
        cap = _make_capture(window_id=276330)
        cap_id = create_capture(db, cap)
        row = db.execute("SELECT window_id FROM captures WHERE id = ?", (cap_id,)).fetchone()
        assert row[0] == 276330

    def test_window_id_nullable(self, db):
        """Pre-V3 captures have no window_id."""
        cap = _make_capture()  # no window_id key
        cap_id = create_capture(db, cap)
        row = db.execute("SELECT window_id FROM captures WHERE id = ?", (cap_id,)).fetchone()
        assert row[0] is None

    def test_window_id_index_exists(self, db):
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_captures_window_ts'"
        )
        assert cursor.fetchone() is not None

    def test_get_captures_includes_window_id(self, db):
        cap_id = create_capture(db, _make_capture(window_id=12345))
        rows = get_captures(db, capture_id=cap_id)
        assert rows[0]["window_id"] == 12345
```

Update `_make_capture` to accept `window_id`:

```python
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
        "window_id": None,
    }
    return {**defaults, **overrides}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/alanye/focus-guardian && python -m pytest tests/test_db.py::TestCaptureWindowId -v`
Expected: FAIL — `OperationalError: table captures has no column named window_id`

- [ ] **Step 3: Add window_id to captures schema and create_capture**

In `src/db.py`, modify the `captures` CREATE TABLE in `_SCHEMA_SQL` — add `window_id` after `pid`:

```sql
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
```

In `create_capture`, add `window_id` to the INSERT:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/alanye/focus-guardian && python -m pytest tests/test_db.py::TestCaptureWindowId -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Run full test suite for regressions**

Run: `cd /Users/alanye/focus-guardian && python -m pytest tests/test_db.py -v`
Expected: All tests PASS (existing tests don't set `window_id`, which defaults to None)

- [ ] **Step 6: Handle existing database migration**

The existing `data/focus_guardian.db` was created with the old schema (no `window_id`, no `layout` table). SQLite's `CREATE TABLE IF NOT EXISTS` won't add columns to existing tables.

Add a migration helper to `src/db.py` after `init_db`:

```python
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


def _run_migrations(conn: sqlite3.Connection):
    """
    Apply pending schema migrations.

    Tracks applied migrations in a simple metadata table.
    """
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
            (name, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()),
        )
        conn.commit()
```

Call `_run_migrations(conn)` at the end of `init_db`, before `return conn`.

- [ ] **Step 7: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat: add window_id column to captures table with migration"
```

---

### Task 3: Extract `capture_window()` from `capture_active_window()`

**Files:**
- Modify: `src/screen_reader.py:168-293`
- Create: `tests/test_capture_window.py`

- [ ] **Step 1: Write failing test for capture_window**

Create `tests/test_capture_window.py`:

```python
"""
Tests for capture_window() — the per-window content extraction function.

These tests verify the function signature and basic contract. Full AX tree
testing requires macOS Accessibility permission and is covered by manual
testing with --once.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from screen_reader import capture_window


class TestCaptureWindowSignature:
    def test_returns_none_for_blocked_app(self):
        """Blocked apps should return a filtered record with no content."""
        with patch("screen_reader.is_app_blocked", return_value=True):
            result = capture_window(
                pid=1234,
                app_name="1Password",
                ax_window=None,
                window_id=100,
            )
        assert result is not None
        assert result["app"] == "1Password"
        assert result["title"] == "[blocked]"
        assert result["text"] == ""
        assert result["filtered"] == "app_blocked"
        assert result["window_id"] == 100

    def test_returns_none_for_hidden_app(self):
        """Hidden apps should return None."""
        with patch("screen_reader.is_app_blocked", return_value=False), \
             patch("screen_reader.is_app_hidden", return_value=True):
            result = capture_window(
                pid=1234,
                app_name="SomeApp",
                ax_window=None,
                window_id=100,
            )
        assert result is None

    def test_returns_dict_with_required_keys(self):
        """Even with no AX window, should return a valid record."""
        with patch("screen_reader.is_app_blocked", return_value=False), \
             patch("screen_reader.is_app_hidden", return_value=False):
            result = capture_window(
                pid=1234,
                app_name="Ghostty",
                ax_window=None,
                window_id=200,
            )
        assert result is not None
        assert "ts" in result
        assert result["app"] == "Ghostty"
        assert result["pid"] == 1234
        assert result["window_id"] == 200
        assert "title" in result
        assert "text" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/alanye/focus-guardian && python -m pytest tests/test_capture_window.py -v`
Expected: FAIL — `ImportError: cannot import name 'capture_window'`

- [ ] **Step 3: Extract capture_window from capture_active_window**

In `src/screen_reader.py`, add `capture_window()` above `capture_active_window()` (around line 168). This is the body of the current `capture_active_window()` but parameterized:

```python
def capture_window(pid, app_name, ax_window, window_id):
    """
    Capture content from a specific window identified by PID and AX element.

    This is the per-window extraction function used by the multi-window
    capture loop. Privacy layers are applied in order:
      1. App blocklist
      2. Hidden app detection
      3. Private window detection
      4. Secure field filtering (inside AX tree walk)
      5. URL scrubbing
      6. Sensitive page detection

    Args:
        pid: Process ID of the app owning this window.
        app_name: Display name of the application.
        ax_window: The matched AX window element (from visible_windows), or None.
        window_id: The CGWindowNumber for this window.

    Returns a dict with keys: ts, app, title, text, url, pid, window_id, idle_s, filtered.
    Returns None if the window should be skipped (hidden, etc.).
    """
    ts = datetime.now(timezone.utc).isoformat()
    idle_s = round(seconds_since_last_input(), 1)

    # Layer 1: App blocklist
    if is_app_blocked(app_name):
        return {
            "ts": ts,
            "app": app_name,
            "title": "[blocked]",
            "text": "",
            "url": None,
            "pid": pid,
            "window_id": window_id,
            "idle_s": idle_s,
            "filtered": "app_blocked",
        }

    # Layer 2a: Hidden app detection
    if is_app_hidden(pid):
        return None

    window_title = ""
    visible_text = ""
    url = None

    if ax_window is not None:
        title_val = _ax_attr(ax_window, "AXTitle")
        if isinstance(title_val, str):
            window_title = title_val.strip()

        # Layer 2b: Private browser window detection
        if is_private_window(app_name, window_title, ax_window=ax_window):
            return {
                "ts": ts,
                "app": app_name,
                "title": "[private window]",
                "text": "",
                "url": None,
                "pid": pid,
                "window_id": window_id,
                "idle_s": idle_s,
                "filtered": "private_window",
            }

        # Extract content from AX tree
        web_area = _find_web_area(ax_window, max_depth=_SCREEN_CFG.get("ax_web_area_search_depth", 10))
        if web_area is not None:
            url_val = _ax_attr(web_area, "AXURL")
            if url_val is not None:
                url_str = str(url_val)
                if not url_str.startswith("file://"):
                    url = url_str

            raw_texts = _extract_text_from_element(web_area, max_depth=_SCREEN_CFG.get("ax_web_content_depth", 15))
        else:
            raw_texts = _extract_text_from_element(ax_window, max_depth=_SCREEN_CFG.get("ax_tree_depth", 5))

        # Deduplicate text fragments, truncate
        seen = set()
        unique_texts = []
        for t in raw_texts:
            if t not in seen and t != window_title:
                seen.add(t)
                unique_texts.append(t)

        max_text = _SCREEN_CFG.get("max_text_length", 20000)
        visible_text = " | ".join(unique_texts)[:max_text]

        # Layer 4: Scrub sensitive URL parameters
        visible_text = scrub_text_urls(visible_text)
        if url is not None:
            url = scrub_url(url)

        # Layer 5: Sensitive page detection
        if is_sensitive_page(window_title, visible_text):
            return {
                "ts": ts,
                "app": app_name,
                "title": "[sensitive page]",
                "text": "",
                "url": None,
                "pid": pid,
                "window_id": window_id,
                "idle_s": idle_s,
                "filtered": "sensitive_page",
            }

    return {
        "ts": ts,
        "app": app_name or "Unknown",
        "title": window_title,
        "text": visible_text,
        "url": url,
        "pid": pid,
        "window_id": window_id,
        "idle_s": idle_s,
    }
```

Then rewrite `capture_active_window()` as a thin wrapper (for `--once` CLI mode):

```python
def capture_active_window():
    """
    Capture the frontmost app's active window. Used by --once CLI mode.

    For the continuous capture loop, use capture_window() directly
    with windows from get_visible_windows().
    """
    frontmost = _get_frontmost_app()
    if frontmost is None:
        return None
    return capture_window(
        pid=frontmost["pid"],
        app_name=frontmost["name"],
        ax_window=None,  # --once mode: let capture_window create AX element
        window_id=None,
    )
```

Note: `capture_active_window` uses `ax_window=None` and `window_id=None` for backwards compat with `--once` mode. In `capture_window`, when `ax_window is None` and the app isn't blocked/hidden, we still get an empty title/text. This is acceptable for the `--once` diagnostic mode. The continuous loop always provides `ax_window` from `get_visible_windows()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/alanye/focus-guardian && python -m pytest tests/test_capture_window.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Verify existing --once mode still works**

Run: `cd /Users/alanye/focus-guardian/src && python screen_reader.py --once --delay 3`
Expected: Prints a JSON capture dict (switch to another app during the 3s delay)

- [ ] **Step 6: Commit**

```bash
git add src/screen_reader.py tests/test_capture_window.py
git commit -m "refactor: extract capture_window() for per-window content capture"
```

---

### Task 4: Rewrite `run_continuous()` for multi-window capture

**Files:**
- Modify: `src/screen_reader.py:351-523` (`run_continuous` and CLI)

- [ ] **Step 1: Add import for visible_windows and json at top of screen_reader.py**

At the top of `src/screen_reader.py`, add to imports:

```python
from visible_windows import get_visible_windows
```

Remove these imports that are no longer needed:

```python
import subprocess  # was used by _get_frontmost_app
from ax_observer import start_observing, stop_observing  # replaced by layout table
```

- [ ] **Step 2: Rewrite run_continuous**

Replace `run_continuous()` (lines 351-468) with:

```python
def _layout_key(windows):
    """Hashable representation of current layout for change detection."""
    return tuple(
        (w["window_id"], w["app"], w["title"])
        for w in windows
    )


def run_continuous(interval=3.0, verbose=False):
    """
    Capture all visible windows every `interval` seconds, dedup per window,
    and store in SQLite.

    Two data streams written independently:
      1. Layout table: written only when the set of visible windows changes.
      2. Captures table: per-window content, deduped per window_id.

    Behavior:
      - Every capture includes idle_s (seconds since last input).
      - Dedup via should_keep(): similarity-based, per window_id.
      - Idle threshold: when idle_s >= timeout, emit one idle marker
        and pause captures until input resumes.
    """
    if not check_accessibility():
        sys.exit(1)

    idle_timeout = _SCREEN_CFG.get("idle_timeout_seconds", 180)

    conn = init_db()

    print(
        f"Screen reader running (interval={interval}s, idle_timeout={idle_timeout}s, "
        f"db=SQLite, multi_window=on). Ctrl+C to stop.",
        file=sys.stderr,
    )

    # Per-window dedup state: {window_id: last_kept_record}
    last_kept = {}
    last_layout = None
    is_idle = False

    while True:
        idle_s = round(seconds_since_last_input(), 1)

        # --- Idle state: user has walked away ---
        if idle_s >= idle_timeout:
            if not is_idle:
                idle_record = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "app": "IDLE",
                    "title": "",
                    "text": "",
                    "url": None,
                    "pid": -1,
                    "window_id": None,
                    "idle_s": idle_s,
                    "idle": True,
                }
                try:
                    create_capture(conn, idle_record)
                except Exception as e:
                    print(f"[screen_reader] DB write failed: {e}", file=sys.stderr)
                if verbose:
                    print(json.dumps(idle_record, ensure_ascii=False))
                is_idle = True
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, interval, False)
            continue

        # --- Active state: user is present ---
        if is_idle:
            is_idle = False
            last_kept.clear()
            last_layout = None

        windows = get_visible_windows()

        # Skip animation frames (empty = Mission Control / Expose)
        if not windows:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, interval, False)
            continue

        # --- Layout change detection ---
        current_layout = _layout_key(windows)
        if current_layout != last_layout:
            panes_json = json.dumps([
                {
                    "window_id": w["window_id"],
                    "app": w["app"],
                    "pid": w["pid"],
                    "title": w["title"],
                    "bounds": w["bounds"],
                }
                for w in windows
            ], ensure_ascii=False)
            try:
                create_layout(conn, {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "panes": panes_json,
                })
            except Exception as e:
                print(f"[screen_reader] Layout write failed: {e}", file=sys.stderr)
            last_layout = current_layout

        # --- Per-window content capture ---
        for win in windows:
            try:
                record = capture_window(
                    pid=win["pid"],
                    app_name=win["app"],
                    ax_window=win["ax_window"],
                    window_id=win["window_id"],
                )
            except Exception as e:
                print(f"[screen_reader] Capture failed for {win['app']}: {e}", file=sys.stderr)
                continue

            if record is None:
                continue

            wid = win["window_id"]
            if should_keep(record, last_kept.get(wid)):
                try:
                    create_capture(conn, record)
                except Exception as e:
                    print(f"[screen_reader] DB write failed: {e}", file=sys.stderr)
                    continue
                last_kept[wid] = record
                if verbose:
                    print(json.dumps(record, ensure_ascii=False))

        CFRunLoopRunInMode(kCFRunLoopDefaultMode, interval, False)
```

- [ ] **Step 3: Update imports at top of screen_reader.py**

The full import block should now be:

```python
import json
import signal
import sys
import time
from datetime import datetime, timezone

signal.signal(signal.SIGINT, lambda *_: (print("\nStopped.", file=sys.stderr), sys.exit(0)))

from AppKit import NSRunningApplication, NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
)
from CoreFoundation import (
    CFRunLoopGetCurrent,
    CFRunLoopRunInMode,
    kCFRunLoopDefaultMode,
)
from Quartz.CoreGraphics import (
    CGEventSourceSecondsSinceLastEventType,
    kCGEventSourceStateHIDSystemState,
    kCGAnyInputEventType,
)

from config import CONFIG
from db import init_db, create_capture, create_layout
from dedup import should_keep
from privacy import is_app_blocked, is_private_window, is_app_hidden, is_secure_field, is_sensitive_page, scrub_url, scrub_text_urls
from visible_windows import get_visible_windows
```

Removed: `import subprocess`, `from ax_observer import start_observing, stop_observing`

- [ ] **Step 4: Verify the continuous loop starts without errors**

Run: `cd /Users/alanye/focus-guardian/src && python screen_reader.py --verbose`
Expected: Prints JSON captures for each visible window every 3s. Ctrl+C to stop after a few cycles.

Check:
- Multiple windows appear per tick (if split-screen)
- Each record has a `window_id` field
- Layout changes are detected on app switch
- No osascript processes spawned (check with `ps aux | grep osascript`)

- [ ] **Step 5: Verify --once mode still works**

Run: `cd /Users/alanye/focus-guardian/src && python screen_reader.py --once --delay 3`
Expected: Prints a single JSON capture. (Note: `--once` still uses the old `capture_active_window()` wrapper — this is fine for diagnostics.)

- [ ] **Step 6: Commit**

```bash
git add src/screen_reader.py
git commit -m "feat: rewrite capture loop for multi-window layout+content split"
```

---

### Task 5: Remove old transition detection machinery

**Files:**
- Modify: `src/screen_reader.py` — remove `_get_frontmost_app`, NSWorkspace observer, AXObserver references
- Delete: `src/ax_observer.py`
- Delete: `tests/test_ax_observer.py`

- [ ] **Step 1: Remove `_get_frontmost_app()` from screen_reader.py**

Delete the entire `_get_frontmost_app` function (lines 66-102 in the current file). This was the osascript subprocess call.

Also remove `import subprocess` from imports if not already removed in Task 4.

- [ ] **Step 2: Remove NSWorkspace/AXObserver code from run_continuous**

The old `run_continuous` had:
- `_on_app_activated` callback
- `_on_window_changed` callback
- NSWorkspace observer registration
- `start_observing()` call

These should already be gone after Task 4's rewrite. Verify no references remain:

Run: `cd /Users/alanye/focus-guardian && grep -n "start_observing\|stop_observing\|_on_app_activated\|_on_window_changed\|NSNotificationCenter" src/screen_reader.py`
Expected: No output (no matches)

- [ ] **Step 3: Remove unused imports**

Verify these are gone from `src/screen_reader.py`:
- `from AppKit import NSRunningApplication, NSWorkspace` — `NSRunningApplication` is no longer used. `NSWorkspace` is no longer used. Remove the entire line.
- `from ax_observer import start_observing, stop_observing` — already removed.
- `import subprocess` — already removed.

Note: Keep `from AppKit import ...` only if other AppKit imports are still needed. Check if `NSRunningApplication` or `NSWorkspace` appear anywhere else in the file. If `capture_active_window()` (the `--once` wrapper) still calls `_get_frontmost_app()`, update it to use `get_visible_windows()[0]` or remove it.

Update `capture_active_window` to not depend on `_get_frontmost_app`:

```python
def capture_active_window():
    """
    Capture the frontmost visible window. Used by --once CLI mode.
    """
    windows = get_visible_windows()
    if not windows:
        return None
    w = windows[0]  # First = frontmost (CGWindowList order)
    return capture_window(
        pid=w["pid"],
        app_name=w["app"],
        ax_window=w["ax_window"],
        window_id=w["window_id"],
    )
```

- [ ] **Step 4: Delete ax_observer.py and its tests**

```bash
git rm src/ax_observer.py tests/test_ax_observer.py
```

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/alanye/focus-guardian && python -m pytest tests/ -v --ignore=tests/test_aw_watcher.py`
Expected: All tests PASS. No imports of `ax_observer` remain.

- [ ] **Step 6: Verify continuous mode still works after cleanup**

Run: `cd /Users/alanye/focus-guardian/src && python screen_reader.py --verbose`
Expected: Multi-window captures, no errors, no osascript processes.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: remove osascript subprocess, NSWorkspace observer, and ax_observer module"
```

---

### Task 6: Clean up test_visible_windows.py

**Files:**
- Delete: `src/test_visible_windows.py` (was a manual test harness, not a pytest test)

- [ ] **Step 1: Remove the manual test script**

```bash
git rm src/test_visible_windows.py
```

- [ ] **Step 2: Commit**

```bash
git commit -m "chore: remove manual test harness for visible_windows"
```

---

## Post-Implementation Verification

After all tasks are complete, verify the full pipeline end-to-end:

1. **Start the reader:** `cd src && python screen_reader.py --buffer-dir ../data/buffer --verbose`
2. **Switch between apps** — verify layout entries appear in DB
3. **Split screen two apps** — verify both windows captured independently
4. **Open two windows of same app** (e.g., two Preview PDFs) — verify separate `window_id` entries
5. **Check DB directly:**
   ```bash
   sqlite3 data/focus_guardian.db "SELECT COUNT(*) FROM layout"
   sqlite3 data/focus_guardian.db "SELECT COUNT(*), COUNT(DISTINCT window_id) FROM captures WHERE window_id IS NOT NULL"
   ```
6. **Verify no osascript:** `ps aux | grep osascript` should show nothing from this process
7. **Run full test suite:** `python -m pytest tests/ -v --ignore=tests/test_aw_watcher.py`
