"""
Visible window detection for Focus Guardian.

Uses CGWindowListCopyWindowInfo to enumerate all windows currently
visible on screen, then matches each to its Accessibility API (AX)
window element for content extraction.

Replaces the old _get_frontmost_app() approach (osascript subprocess
every 3 seconds) with an in-process Quartz call that returns ALL
visible windows, not just the frontmost.
"""

from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionOnScreenOnly,
    kCGNullWindowID,
)


# System UI owners that should never be treated as user app windows.
_SYSTEM_OWNERS = frozenset({
    "Control Center",
    "Dock",
    "Finder Desktop",
    "Hidden Bar",
    "Notification Center",
    "Screenshot",
    "SystemUIServer",
    "TextInputMenuAgent",
    "Wallpaper",
    "Window Server",
    "WindowManager",
    "Wispr Flow",
})

# Minimum window dimension (pixels) to filter out tiny helper windows,
# menu bar icons, and floating widgets.
_MIN_WINDOW_SIZE = 100


def _ax_attr(element, attr):
    """Read a single AX attribute. Returns value or None."""
    err, value = AXUIElementCopyAttributeValue(element, attr, None)
    if err == 0:
        return value
    return None


def _get_ax_windows(pid):
    """
    Get all AX window elements for a given PID.

    Returns a list of (title, bounds, ax_element) tuples.
    Bounds are normalised to {X, Y, Width, Height} ints for matching.
    """
    ax_app = AXUIElementCreateApplication(pid)
    ax_windows = _ax_attr(ax_app, "AXWindows")
    if not ax_windows:
        return []

    results = []
    for ax_win in ax_windows:
        title = _ax_attr(ax_win, "AXTitle") or ""
        pos_ref = _ax_attr(ax_win, "AXPosition")
        size_ref = _ax_attr(ax_win, "AXSize")

        bounds = None
        if pos_ref is not None and size_ref is not None:
            ok_p, point = AXValueGetValue(pos_ref, kAXValueCGPointType, None)
            ok_s, size = AXValueGetValue(size_ref, kAXValueCGSizeType, None)
            if ok_p and ok_s:
                bounds = {
                    "X": int(point.x),
                    "Y": int(point.y),
                    "Width": int(size.width),
                    "Height": int(size.height),
                }

        results.append((title, bounds, ax_win))

    return results


def _match_ax_window(cg_title, cg_bounds, ax_windows):
    """
    Find the AX window element that corresponds to a CG window.

    Matching strategy (in order):
      1. Title match — if titles are non-empty and equal, use it.
      2. Bounds match — compare position and size within a tolerance.
      3. First available — fallback if only one AX window exists.

    Returns the AX window element, or None if no match found.
    """
    # Strategy 1: title match
    if cg_title:
        for ax_title, _, ax_win in ax_windows:
            if ax_title == cg_title:
                return ax_win

    # Strategy 2: bounds match (within 5px tolerance for retina rounding)
    if cg_bounds:
        tolerance = 5
        for _, ax_bounds, ax_win in ax_windows:
            if ax_bounds is None:
                continue
            if (
                abs(cg_bounds["X"] - ax_bounds["X"]) <= tolerance
                and abs(cg_bounds["Y"] - ax_bounds["Y"]) <= tolerance
                and abs(cg_bounds["Width"] - ax_bounds["Width"]) <= tolerance
                and abs(cg_bounds["Height"] - ax_bounds["Height"]) <= tolerance
            ):
                return ax_win

    # Strategy 3: single window fallback
    if len(ax_windows) == 1:
        return ax_windows[0][2]

    return None


def get_visible_windows():
    """
    Return all user-visible windows currently on screen.

    Each entry is a dict:
      {
        "window_id": int,       # kCGWindowNumber — stable for window lifetime
        "app": str,             # application name
        "pid": int,             # process ID
        "title": str,           # window title (may be empty)
        "bounds": dict,         # {X, Y, Width, Height}
        "ax_window": element,   # matched AX window element (or None)
      }

    Windows are filtered to:
      - Layer 0 only (normal app windows, not menu bar / dock / desktop)
      - Not in the system owner exclusion list
      - Minimum size threshold (filters tiny helper windows)

    Results are ordered front-to-back (frontmost window first), which is
    the default order from CGWindowListCopyWindowInfo.
    """
    cg_windows = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly, kCGNullWindowID
    )
    if not cg_windows:
        return []

    # Filter to layer 0 app windows
    visible = []
    for w in cg_windows:
        if w.get("kCGWindowLayer", -1) != 0:
            continue

        owner = w.get("kCGWindowOwnerName", "")
        if owner in _SYSTEM_OWNERS:
            continue

        bounds = w.get("kCGWindowBounds", {})
        width = int(bounds.get("Width", 0))
        height = int(bounds.get("Height", 0))
        if width < _MIN_WINDOW_SIZE or height < _MIN_WINDOW_SIZE:
            continue

        visible.append({
            "window_id": w.get("kCGWindowNumber"),
            "app": owner,
            "pid": w.get("kCGWindowOwnerPID"),
            "title": w.get("kCGWindowName", "") or "",
            "bounds": {
                "X": int(bounds.get("X", 0)),
                "Y": int(bounds.get("Y", 0)),
                "Width": width,
                "Height": height,
            },
        })

    # Match each CG window to its AX window element.
    # Group by PID so we only query AXWindows once per app.
    ax_cache = {}
    for win in visible:
        pid = win["pid"]
        if pid not in ax_cache:
            ax_cache[pid] = _get_ax_windows(pid)

        win["ax_window"] = _match_ax_window(
            win["title"], win["bounds"], ax_cache[pid]
        )

    # Guard: during Mission Control / Exposé / Space transitions, macOS
    # briefly composites all windows "on screen." In that state, AX
    # matching fails across the board.  If no window matched its AX
    # element, we're in a transition animation — return empty to let
    # the caller skip this tick.
    if visible and not any(w["ax_window"] for w in visible):
        return []

    return visible


# ---------------------------------------------------------------------------
# CLI: quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    windows = get_visible_windows()
    print(f"Visible windows: {len(windows)}\n")
    for w in windows:
        ax_status = "matched" if w["ax_window"] is not None else "no match"
        print(
            f"  [{w['window_id']:5d}] {w['app']:25s} "
            f"title={w['title'][:40]:40s} "
            f"{w['bounds']['Width']}x{w['bounds']['Height']} "
            f"ax={ax_status}"
        )
