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
