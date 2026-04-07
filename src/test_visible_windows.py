"""
Quick test: print visible windows every 3s, plus instant on layout change.
Ctrl+C to stop.
"""

import signal
import sys
import time
from datetime import datetime

signal.signal(signal.SIGINT, lambda *_: (print("\nStopped."), sys.exit(0)))

from visible_windows import get_visible_windows


def _layout_key(windows):
    """Hashable representation of current layout for change detection."""
    return tuple(
        (w["window_id"], w["app"], w["title"])
        for w in windows
    )


def _print_snapshot(windows, reason="poll"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] ({reason}) — {len(windows)} window(s)")
    for w in windows:
        ax = "AX" if w["ax_window"] else "--"
        b = w["bounds"]
        print(
            f"  {w['window_id']:5d} | {w['app']:25s} | "
            f"{w['title'][:50]:50s} | "
            f"{b['Width']}x{b['Height']} | {ax}"
        )


print("Watching visible windows (3s heartbeat + instant on change). Ctrl+C to stop.\n")

last_layout = None

while True:
    windows = get_visible_windows()
    current_layout = _layout_key(windows)

    if current_layout != last_layout:
        reason = "CHANGE" if last_layout is not None else "start"
        _print_snapshot(windows, reason)
        last_layout = current_layout
    else:
        _print_snapshot(windows, "poll")

    time.sleep(3)
