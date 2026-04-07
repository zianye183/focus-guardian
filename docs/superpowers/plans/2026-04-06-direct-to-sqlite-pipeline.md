# Direct-to-SQLite Capture Pipeline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire screen_reader directly to SQLite via dedup, eliminating the JSONL buffer intermediate step.

**Architecture:** `capture_active_window()` produces a record dict. `should_keep()` from dedup.py decides whether it's worth storing. If yes, `create_capture()` from db.py writes it to SQLite. Both the poll loop and the NSWorkspace observer use the same `_try_store()` function, giving a single dedup standard and a single write path. JSONL buffer code is removed.

**Tech Stack:** Python 3.12, SQLite (WAL mode), pyobjc, existing dedup.py + db.py modules

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/db.py` | Modify (schema + `create_capture`) | Add `filtered` and `transition` columns |
| `tests/test_db.py` | Modify (update fixtures + add tests) | Cover new columns |
| `src/screen_reader.py` | Modify (rewrite `run_continuous` + remove JSONL code) | Wire capture -> dedup -> SQLite |
| `tests/test_dedup.py` | No change | Already covers `should_keep()` |

---

### Task 1: Add `filtered` and `transition` columns to DB schema

**Files:**
- Modify: `src/db.py:22-35` (schema SQL)
- Modify: `src/db.py:155-179` (`create_capture` function)
- Modify: `tests/test_db.py:52-65` (`_make_capture` helper)
- Modify: `tests/test_db.py` (add new tests)

- [ ] **Step 1: Write failing test for `filtered` column**

In `tests/test_db.py`, add a new test to `TestCreateCapture`:

```python
def test_filtered_field_stored(self, db):
    """filtered column stores the filter reason (app_blocked, private_window, sensitive_page)."""
    cap = _make_capture(filtered="app_blocked")
    cap_id = create_capture(db, cap)
    row = db.execute("SELECT filtered FROM captures WHERE id = ?", (cap_id,)).fetchone()
    assert row[0] == "app_blocked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_db.py::TestCreateCapture::test_filtered_field_stored -v`
Expected: FAIL — `filtered` column doesn't exist in schema, and `create_capture` doesn't handle it.

- [ ] **Step 3: Add `filtered` and `transition` columns to schema SQL**

In `src/db.py`, replace the captures table definition (lines 24-35):

```python
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
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_captures_ts ON captures(ts);
CREATE INDEX IF NOT EXISTS idx_captures_app ON captures(app);
```

Note: the rest of `_SCHEMA_SQL` (sessions, summaries, FTS) stays the same.

- [ ] **Step 4: Update `create_capture()` to handle new fields**

In `src/db.py`, replace `create_capture` (lines 155-179):

```python
def create_capture(conn: sqlite3.Connection, capture: dict) -> int:
    """
    Insert a capture record. Returns the new row ID.

    Does not mutate the input dict.
    """
    cursor = conn.execute(
        """
        INSERT INTO captures (ts, app, title, text, text_raw, url, idle_s, idle, filtered, transition, pid)
        VALUES (:ts, :app, :title, :text, :text_raw, :url, :idle_s, :idle, :filtered, :transition, :pid)
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
        },
    )
    conn.commit()
    return cursor.lastrowid
```

- [ ] **Step 5: Update `_make_capture` test helper**

In `tests/test_db.py`, replace `_make_capture` (lines 52-65):

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
    }
    return {**defaults, **overrides}
```

- [ ] **Step 6: Run the filtered field test**

Run: `python -m pytest tests/test_db.py::TestCreateCapture::test_filtered_field_stored -v`
Expected: PASS

- [ ] **Step 7: Write test for `transition` column**

Add to `TestCreateCapture` in `tests/test_db.py`:

```python
def test_transition_field_stored(self, db):
    """transition column stores whether this capture was an app-switch transition."""
    cap = _make_capture(transition=True)
    cap_id = create_capture(db, cap)
    row = db.execute("SELECT transition FROM captures WHERE id = ?", (cap_id,)).fetchone()
    assert row[0] == 1  # SQLite stores bool as int
```

- [ ] **Step 8: Run transition test**

Run: `python -m pytest tests/test_db.py::TestCreateCapture::test_transition_field_stored -v`
Expected: PASS (schema and create_capture already updated in steps 3-4)

- [ ] **Step 9: Update `test_all_fields_stored` to cover new columns**

Replace `test_all_fields_stored` in `TestCreateCapture`:

```python
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
    assert row_dict["transition"] == 1
    assert row_dict["pid"] == cap["pid"]
```

- [ ] **Step 10: Update `test_nullable_fields` to cover new columns**

Replace `test_nullable_fields` in `TestCreateCapture`:

```python
def test_nullable_fields(self, db):
    """text, text_raw, url, idle_s, filtered, pid can all be None."""
    cap = _make_capture(text=None, text_raw=None, url=None, idle_s=None, filtered=None, pid=None)
    cap_id = create_capture(db, cap)
    row = db.execute("SELECT text, text_raw, url, idle_s, filtered, pid FROM captures WHERE id = ?", (cap_id,)).fetchone()
    assert row == (None, None, None, None, None, None)
```

- [ ] **Step 11: Run full test suite**

Run: `python -m pytest tests/test_db.py -v`
Expected: ALL PASS

- [ ] **Step 12: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat: add filtered and transition columns to captures table"
```

---

### Task 2: Wire screen_reader to SQLite via dedup

**Files:**
- Modify: `src/screen_reader.py:1-37` (imports)
- Modify: `src/screen_reader.py:356-502` (remove JSONL helpers, rewrite `run_continuous`)
- Modify: `src/screen_reader.py:505-562` (CLI args)

- [ ] **Step 1: Update imports**

In `src/screen_reader.py`, replace the import block (lines 14-37):

```python
import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

signal.signal(signal.SIGINT, lambda *_: (print("\nStopped.", file=sys.stderr), sys.exit(0)))

from AppKit import NSRunningApplication, NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
)
from Quartz.CoreGraphics import (
    CGEventSourceSecondsSinceLastEventType,
    kCGEventSourceStateHIDSystemState,
    kCGAnyInputEventType,
)

from config import CONFIG
from db import init_db, create_capture
from dedup import should_keep
from privacy import is_app_blocked, is_private_window, is_app_hidden, is_secure_field, is_sensitive_page, scrub_url, scrub_text_urls
```

- [ ] **Step 2: Remove JSONL helpers, add `_try_store`**

Replace lines 354-381 (`_is_transition`, `record_to_jsonl`, `append_to_buffer`, `_emit`) with:

```python
# ---------------------------------------------------------------------------
# Storage: dedup + write to SQLite
# ---------------------------------------------------------------------------

def _try_store(record, state, conn, verbose):
    """
    Decide whether to keep this record (via dedup) and store it in SQLite.

    Updates state["last_kept"] on success.
    """
    if should_keep(record, state["last_kept"]):
        create_capture(conn, record)
        state["last_kept"] = record
        if verbose:
            print(json.dumps(record, ensure_ascii=False))
```

Note: `_is_transition` (lines 326-350) is also removed — `should_keep` handles all transition detection (app change, URL change, title change).

- [ ] **Step 3: Rewrite `run_continuous`**

Replace `run_continuous` (lines 383-502) with:

```python
def run_continuous(interval=3.0, verbose=False):
    """
    Capture active window every `interval` seconds, dedup, and store in SQLite.

    Two capture modes run simultaneously:
      1. Polling (every `interval` seconds): steady-state monitoring with dedup.
      2. NSWorkspace observer: fires instantly on app activation. Captures
         the moment of transition before the poll would catch it.

    Behavior:
      - Every capture includes idle_s (seconds since last input).
      - Dedup via should_keep(): similarity-based, with forced snapshots.
      - Idle threshold: when idle_s >= timeout, store one idle marker
        and pause captures until input resumes.
      - On resume: store a normal capture immediately.
      - Transition captures are tagged with transition=True.
    """
    if not check_accessibility():
        sys.exit(1)

    idle_timeout = _SCREEN_CFG.get("idle_timeout_seconds", 180)

    conn = init_db()

    print(
        f"Screen reader running (interval={interval}s, idle_timeout={idle_timeout}s, "
        f"db=SQLite, instant_transitions=on). Ctrl+C to stop.",
        file=sys.stderr,
    )

    # Shared state between the observer callback and the poll loop
    state = {
        "last_kept": None,
        "is_idle": False,
    }

    def _on_app_activated(notification):
        """
        NSWorkspace observer callback: fires instantly when user switches apps.
        Captures screen text immediately so we catch what pulled their attention.
        """
        record = capture_active_window_safe()
        if record is None:
            return

        record = {**record, "transition": True}
        _try_store(record, state, conn, verbose)

    # Register the NSWorkspace observer for app activation events
    from AppKit import NSNotificationCenter
    workspace = NSWorkspace.sharedWorkspace()
    nc = workspace.notificationCenter()
    nc.addObserverForName_object_queue_usingBlock_(
        "NSWorkspaceDidActivateApplicationNotification",
        None,   # observe all objects
        None,   # deliver on posting thread
        _on_app_activated,
    )

    is_idle = False

    while True:
        idle_s = round(seconds_since_last_input(), 1)

        # --- Idle state: user has walked away ---
        if idle_s >= idle_timeout:
            if not is_idle:
                last = state["last_kept"]
                idle_record = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "app": last["app"] if last else "Unknown",
                    "title": last["title"] if last else "",
                    "text": "",
                    "url": None,
                    "pid": last["pid"] if last else -1,
                    "idle_s": idle_s,
                    "idle": True,
                }
                _try_store(idle_record, state, conn, verbose)
                is_idle = True
            time.sleep(interval)
            continue

        # --- Active state: user is present ---
        if is_idle:
            is_idle = False
            state["last_kept"] = None

        record = capture_active_window_safe()

        if record is not None:
            _try_store(record, state, conn, verbose)

        time.sleep(interval)
```

- [ ] **Step 4: Update CLI args**

Replace the CLI block (lines 509-562) with:

```python
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Focus Guardian screen text reader")
    default_interval = _SCREEN_CFG.get("interval_seconds", 3.0)
    parser.add_argument(
        "--interval", type=float, default=default_interval,
        help=f"Capture interval in seconds (default: {default_interval})",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print captures to stdout as JSON",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Capture once and print, then exit (does not store to DB)",
    )
    parser.add_argument(
        "--delay", type=float, default=0,
        help="[TESTING ONLY] Seconds to wait before capturing (use with --once to switch apps first)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check accessibility permission and exit",
    )

    args = parser.parse_args()

    if args.check:
        ok = check_accessibility()
        if ok:
            print("Accessibility: OK")
        sys.exit(0 if ok else 1)

    if args.once:
        if not check_accessibility():
            sys.exit(1)
        if args.delay > 0:
            print(f"Waiting {args.delay}s -- switch to the app you want to test...", file=sys.stderr)
            time.sleep(args.delay)
        record = capture_active_window_safe()
        if record:
            print(json.dumps(record, indent=2, ensure_ascii=False))
        sys.exit(0)

    run_continuous(
        interval=args.interval,
        verbose=args.verbose,
    )
```

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS — dedup and db tests don't touch screen_reader imports. screen_reader has no tests (so no failures either).

- [ ] **Step 6: Manual smoke test**

Run: `cd src && python screen_reader.py --once --delay 3`
Expected: Prints a JSON capture of the frontmost window. Verify it includes `ts`, `app`, `title`, `text`, `pid`, `idle_s` fields.

Then: `cd src && python screen_reader.py --verbose --interval 5`
Expected: Prints captures to stdout. Switch apps a couple times — should see transition records and dedup in action (not every 5s poll emitted). Check that `data/focus_guardian.db` exists and has rows:

```bash
sqlite3 data/focus_guardian.db "SELECT count(*) FROM captures"
```

Ctrl+C to stop.

- [ ] **Step 7: Commit**

```bash
git add src/screen_reader.py
git commit -m "feat: wire screen_reader directly to SQLite via dedup, remove JSONL buffer"
```

---

### Task 3: Delete dead JSONL buffer data references

**Files:**
- Modify: `src/screen_reader.py` — verify no remaining references to `append_to_buffer`, `record_to_jsonl`, `_emit`, `buffer_dir`
- No code changes expected — Task 2 already removed them. This is a verification step.

- [ ] **Step 1: Grep for dead references**

Run:
```bash
grep -rn "buffer_dir\|append_to_buffer\|record_to_jsonl\|_emit\|jsonl" src/screen_reader.py
```

Expected: No matches (all removed in Task 2). The docstring at the top (line 8) mentions "JSONL" — update it.

- [ ] **Step 2: Update module docstring**

Replace the module docstring at the top of `src/screen_reader.py` (lines 1-12):

```python
"""
Screen text reader for Focus Guardian.

Reads visible text + app name from the active window using macOS
Accessibility APIs via pyobjc. Uses System Events via AppleScript to
detect the true frontmost application (not just the terminal running
this script). Deduplicates captures via similarity matching and stores
results directly in SQLite.

Requires: macOS Accessibility permission in
System Settings -> Privacy & Security -> Accessibility
(grant to Terminal / iTerm / whatever runs this script)
"""
```

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS (152 tests)

- [ ] **Step 4: Commit**

```bash
git add src/screen_reader.py
git commit -m "chore: update screen_reader docstring, remove JSONL references"
```

---

## Post-Implementation Verification

After all tasks complete, run:

```bash
# 1. Full test suite
python -m pytest tests/ -v

# 2. Smoke test: single capture
cd src && python screen_reader.py --once --delay 3

# 3. Smoke test: continuous (run for 30s, switch apps, then Ctrl+C)
cd src && python screen_reader.py --verbose --interval 3

# 4. Verify DB has data
sqlite3 data/focus_guardian.db "SELECT count(*) FROM captures"
sqlite3 data/focus_guardian.db "SELECT app, title, filtered, transition FROM captures LIMIT 10"

# 5. Verify dedup is working (should see fewer rows than elapsed_seconds / interval)
sqlite3 data/focus_guardian.db "SELECT count(*) FROM captures"
```
