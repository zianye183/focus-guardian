# AX Window Focus Observer

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect window switches within the same app (e.g., switching between two PDFs in Preview) instantly, instead of relying on the 3s poll interval.

**Architecture:** Add an AXObserver per frontmost app that watches `kAXFocusedWindowChangedNotification`. When the NSWorkspace observer fires (app switch), tear down the old AX observer and create one for the new app's PID. The AX observer callback captures and stores via the same `_try_store()` path as the existing poll and NSWorkspace observer. A new module `src/ax_observer.py` encapsulates all AXObserver lifecycle management to keep screen_reader.py focused.

**Tech Stack:** pyobjc (`ApplicationServices.AXObserverCreate`, `CoreFoundation.CFRunLoop*`), existing `_try_store` pipeline

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/ax_observer.py` | Create | AXObserver lifecycle: create, attach to run loop, tear down, swap on app change |
| `src/screen_reader.py` | Modify (imports + `run_continuous`) | Integrate ax_observer into the capture loop |
| `tests/test_ax_observer.py` | Create | Unit tests for observer lifecycle (mocked pyobjc APIs) |

---

### Task 1: Create `ax_observer.py` with observer lifecycle management

**Files:**
- Create: `src/ax_observer.py`
- Create: `tests/test_ax_observer.py`

This module manages one AXObserver at a time, pointed at the frontmost app's PID. It exposes two functions: `start_observing(pid, callback)` and `stop_observing()`.

- [ ] **Step 1: Write failing tests for `start_observing` and `stop_observing`**

Create `tests/test_ax_observer.py`:

```python
"""
Tests for ax_observer module — AXObserver lifecycle management.

All pyobjc/CoreFoundation APIs are mocked since they require macOS
Accessibility permissions and a running GUI app to observe.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def mock_ax_apis():
    """Mock all pyobjc AXObserver and CoreFoundation APIs."""
    with patch("ax_observer.AXObserverCreate") as mock_create, \
         patch("ax_observer.AXObserverAddNotification") as mock_add, \
         patch("ax_observer.AXObserverRemoveNotification") as mock_remove, \
         patch("ax_observer.AXObserverGetRunLoopSource") as mock_source, \
         patch("ax_observer.AXUIElementCreateApplication") as mock_ax_app, \
         patch("ax_observer.CFRunLoopGetCurrent") as mock_loop, \
         patch("ax_observer.CFRunLoopAddSource") as mock_add_source, \
         patch("ax_observer.CFRunLoopRemoveSource") as mock_remove_source:

        mock_observer = MagicMock(name="AXObserverRef")
        mock_create.return_value = (0, mock_observer)
        mock_add.return_value = 0
        mock_source.return_value = MagicMock(name="CFRunLoopSourceRef")
        mock_ax_app.return_value = MagicMock(name="AXUIElementRef")
        mock_loop.return_value = MagicMock(name="CFRunLoopRef")

        yield {
            "create": mock_create,
            "add_notification": mock_add,
            "remove_notification": mock_remove,
            "get_source": mock_source,
            "ax_app": mock_ax_app,
            "get_loop": mock_loop,
            "add_source": mock_add_source,
            "remove_source": mock_remove_source,
            "observer": mock_observer,
            "source": mock_source.return_value,
            "loop": mock_loop.return_value,
        }


class TestStartObserving:
    def test_creates_observer_for_pid(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()  # ensure clean state

        callback = MagicMock()
        result = start_observing(1234, callback)

        assert result is True
        mock_ax_apis["create"].assert_called_once()
        # First arg is the PID
        assert mock_ax_apis["create"].call_args[0][0] == 1234

    def test_registers_focused_window_notification(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1234, callback)

        mock_ax_apis["add_notification"].assert_called_once()
        call_args = mock_ax_apis["add_notification"].call_args[0]
        # Third arg is the notification name
        assert call_args[2] == "AXFocusedWindowChanged"

    def test_adds_source_to_run_loop(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1234, callback)

        mock_ax_apis["get_source"].assert_called_once()
        mock_ax_apis["add_source"].assert_called_once()

    def test_returns_false_on_create_failure(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        mock_ax_apis["create"].return_value = (-25200, None)  # kAXErrorFailure

        callback = MagicMock()
        result = start_observing(1234, callback)

        assert result is False

    def test_returns_false_on_add_notification_failure(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        mock_ax_apis["add_notification"].return_value = -25207  # unsupported

        callback = MagicMock()
        result = start_observing(1234, callback)

        assert result is False

    def test_tears_down_previous_observer_before_new_one(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1111, callback)
        start_observing(2222, callback)

        # Should have removed the run loop source from the first observer
        mock_ax_apis["remove_source"].assert_called_once()
        # Should have created two observers
        assert mock_ax_apis["create"].call_count == 2

    def test_skips_if_same_pid(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1234, callback)
        start_observing(1234, callback)

        # Should only create once — second call is a no-op
        assert mock_ax_apis["create"].call_count == 1


class TestStopObserving:
    def test_removes_source_from_run_loop(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1234, callback)
        stop_observing()

        mock_ax_apis["remove_source"].assert_called_once_with(
            mock_ax_apis["loop"],
            mock_ax_apis["source"],
            "kCFRunLoopDefaultMode",
        )

    def test_noop_when_no_observer(self, mock_ax_apis):
        from ax_observer import stop_observing
        stop_observing()
        # Should not raise
        stop_observing()

    def test_clears_state_after_stop(self, mock_ax_apis):
        from ax_observer import start_observing, stop_observing
        stop_observing()

        callback = MagicMock()
        start_observing(1234, callback)
        stop_observing()

        # Starting a new observer should work (state was cleared)
        start_observing(5678, callback)
        assert mock_ax_apis["create"].call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ax_observer.py -v`
Expected: FAIL — `ax_observer` module doesn't exist yet.

- [ ] **Step 3: Implement `ax_observer.py`**

Create `src/ax_observer.py`:

```python
"""
AXObserver lifecycle management for Focus Guardian.

Watches kAXFocusedWindowChangedNotification on a single app at a time.
When the frontmost app changes, the caller tears down the old observer
and creates a new one for the new app's PID.

All pyobjc AXObserver APIs require macOS Accessibility permission.
"""

import sys

import objc
from ApplicationServices import (
    AXObserverCreate,
    AXObserverAddNotification,
    AXObserverRemoveNotification,
    AXObserverGetRunLoopSource,
    AXUIElementCreateApplication,
)
from CoreFoundation import (
    CFRunLoopGetCurrent,
    CFRunLoopAddSource,
    CFRunLoopRemoveSource,
    kCFRunLoopDefaultMode,
)

_NOTIFICATION = "AXFocusedWindowChanged"

# Module-level state for the current observer
_state = {
    "observer": None,
    "source": None,
    "pid": None,
}

# The user-provided callback, stored at module level so the
# @objc.callbackFor decorated function can reach it.
_user_callback = None


@objc.callbackFor(AXObserverCreate, argIndex=1)
def _ax_callback(observer, element, notification_name, refcon):
    """
    pyobjc-compatible callback for AXObserver.

    Delegates to the user-provided callback stored at module level.
    """
    if _user_callback is not None:
        _user_callback()


def start_observing(pid, callback):
    """
    Start observing kAXFocusedWindowChangedNotification for a specific app PID.

    If already observing a different PID, tears down the old observer first.
    If already observing the same PID, returns True (no-op).

    Args:
        pid: Process ID of the app to observe.
        callback: Zero-argument callable invoked when the focused window changes.

    Returns:
        True if the observer was set up successfully, False on failure.
    """
    global _user_callback

    # Already observing this PID
    if _state["pid"] == pid and _state["observer"] is not None:
        return True

    # Tear down previous observer if any
    if _state["observer"] is not None:
        stop_observing()

    _user_callback = callback

    # Create observer for the target PID
    err, observer = AXObserverCreate(pid, _ax_callback, None)
    if err != 0:
        print(f"[ax_observer] AXObserverCreate failed for PID {pid}: error {err}", file=sys.stderr)
        return False

    # Create AX element for the app and register notification
    ax_app = AXUIElementCreateApplication(pid)
    err = AXObserverAddNotification(observer, ax_app, _NOTIFICATION, None)
    if err != 0:
        print(f"[ax_observer] AXObserverAddNotification failed for PID {pid}: error {err}", file=sys.stderr)
        return False

    # Add the observer's run loop source to the current run loop
    source = AXObserverGetRunLoopSource(observer)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopDefaultMode)

    _state["observer"] = observer
    _state["source"] = source
    _state["pid"] = pid

    return True


def stop_observing():
    """
    Tear down the current AXObserver if one exists.

    Removes the run loop source and clears all state.
    Safe to call when no observer is active (no-op).
    """
    global _user_callback

    if _state["observer"] is None:
        return

    if _state["source"] is not None:
        CFRunLoopRemoveSource(
            CFRunLoopGetCurrent(),
            _state["source"],
            kCFRunLoopDefaultMode,
        )

    _state["observer"] = None
    _state["source"] = None
    _state["pid"] = None
    _user_callback = None
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_ax_observer.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: 154+ tests pass (existing tests unaffected)

- [ ] **Step 6: Commit**

```bash
git add src/ax_observer.py tests/test_ax_observer.py
git commit -m "feat: add ax_observer module for window focus change detection"
```

---

### Task 2: Integrate ax_observer into screen_reader

**Files:**
- Modify: `src/screen_reader.py:24-38` (imports)
- Modify: `src/screen_reader.py:345-437` (`run_continuous`)

- [ ] **Step 1: Add import**

In `src/screen_reader.py`, after line 38 (`from privacy import ...`), add:

```python
from ax_observer import start_observing, stop_observing
```

- [ ] **Step 2: Update `_on_app_activated` to swap AX observer**

In `run_continuous()`, replace the `_on_app_activated` function (lines 380-390) with:

```python
    def _on_app_activated(notification):
        """
        NSWorkspace observer callback: fires instantly when user switches apps.
        Captures screen text immediately so we catch what pulled their attention.
        Also sets up an AXObserver on the new app to detect window switches.
        """
        record = capture_active_window_safe()
        if record is None:
            return

        record = {**record, "transition": True}
        _try_store(record, state, conn, verbose)

        # Set up AX observer on the new app for window focus changes
        pid = record.get("pid")
        if pid and pid > 0:
            start_observing(pid, _on_window_changed)
```

- [ ] **Step 3: Add `_on_window_changed` callback**

Inside `run_continuous()`, before `_on_app_activated`, add:

```python
    def _on_window_changed():
        """
        AXObserver callback: fires when focused window changes within an app.
        Captures screen text immediately to detect window switches (e.g.,
        switching between two PDFs in Preview).
        """
        record = capture_active_window_safe()
        if record is None:
            return

        record = {**record, "transition": True}
        _try_store(record, state, conn, verbose)
```

- [ ] **Step 4: Set up initial AX observer after NSWorkspace registration**

After the NSWorkspace observer registration (after line 401), add:

```python
    # Set up AX observer for the currently frontmost app
    initial = _get_frontmost_app()
    if initial and initial.get("pid"):
        start_observing(initial["pid"], _on_window_changed)
```

- [ ] **Step 5: Update the startup log message**

Replace the print statement (lines 369-372):

```python
    print(
        f"Screen reader running (interval={interval}s, idle_timeout={idle_timeout}s, "
        f"db=SQLite, instant_transitions=on, window_observer=on). Ctrl+C to stop.",
        file=sys.stderr,
    )
```

- [ ] **Step 6: Update docstring**

Replace the `run_continuous` docstring (lines 346-360):

```python
    """
    Capture active window every `interval` seconds, dedup, and store in SQLite.

    Three capture modes run simultaneously:
      1. Polling (every `interval` seconds): steady-state monitoring with dedup.
      2. NSWorkspace observer: fires instantly on app activation (app switch).
      3. AXObserver: fires instantly on focused window change within an app
         (e.g., switching between two PDFs in Preview).

    Behavior:
      - Every capture includes idle_s (seconds since last input).
      - Dedup via should_keep(): similarity-based, with forced snapshots.
      - Idle threshold: when idle_s >= timeout, store one idle marker
        and pause captures until input resumes.
      - On resume: store a normal capture immediately.
      - Transition captures are tagged with transition=True.
    """
```

- [ ] **Step 7: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 8: Manual smoke test**

Run: `cd src && python screen_reader.py --verbose --interval 5`

Test scenario:
1. Open Preview with two different PDFs in separate windows
2. Switch between the PDF windows (Cmd+` or click)
3. Each switch should produce a capture immediately (not wait for 5s poll)
4. Verify in output: both PDF titles appear, each with `"transition": true`

Then verify DB:
```bash
sqlite3 data/focus_guardian.db "SELECT app, title, transition FROM captures ORDER BY ts DESC LIMIT 10"
```

- [ ] **Step 9: Commit**

```bash
git add src/screen_reader.py
git commit -m "feat: integrate AX window observer for intra-app window switch detection"
```

---

## Post-Implementation Verification

After all tasks complete:

```bash
# 1. Full test suite
python -m pytest tests/ -v

# 2. Smoke test: continuous with window switching
cd src && python screen_reader.py --verbose --interval 5

# Test: switch between apps (NSWorkspace fires)
# Test: switch between windows in same app (AXObserver fires)
# Test: go idle for 3+ minutes, come back (idle detection)
# Test: open 1Password (should show [blocked])

# 3. Verify DB
sqlite3 data/focus_guardian.db "SELECT app, title, transition FROM captures ORDER BY ts DESC LIMIT 20"
```
