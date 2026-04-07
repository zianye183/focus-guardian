"""
Screen text reader for Focus Guardian.

Reads visible text + app name from the active window using macOS
Accessibility APIs via pyobjc. Uses System Events via AppleScript to
detect the true frontmost application (not just the terminal running
this script). Deduplicates captures via similarity matching and stores
results directly in SQLite.

Requires: macOS Accessibility permission in
System Settings → Privacy & Security → Accessibility
(grant to Terminal / iTerm / whatever runs this script)
"""

import json
import signal
import sys
import time
from datetime import datetime, timezone

signal.signal(signal.SIGINT, lambda *_: (print("\nStopped.", file=sys.stderr), sys.exit(0)))

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

_SCREEN_CFG = CONFIG["screen_reader"]
_IDLE_TIMEOUT = _SCREEN_CFG.get("idle_timeout_seconds", 180)


# ---------------------------------------------------------------------------
# Idle detection
# ---------------------------------------------------------------------------

def seconds_since_last_input():
    """Seconds since last keyboard/mouse/trackpad event (macOS HID layer)."""
    return CGEventSourceSecondsSinceLastEventType(
        kCGEventSourceStateHIDSystemState, kCGAnyInputEventType
    )


# ---------------------------------------------------------------------------
# Accessibility helpers
# ---------------------------------------------------------------------------

def _ax_attr(element, attr):
    """Read a single AX attribute. Returns value or None."""
    err, value = AXUIElementCopyAttributeValue(element, attr, None)
    if err == 0:
        return value
    return None


def _find_web_area(element, depth=0, max_depth=10):
    """Find the AXWebArea node in Electron/Chromium apps."""
    role = _ax_attr(element, "AXRole") or ""
    if role == "AXWebArea":
        return element
    children = _ax_attr(element, "AXChildren")
    if children and depth < max_depth:
        for child in children:
            result = _find_web_area(child, depth + 1, max_depth)
            if result:
                return result
    return None


def _extract_text_from_element(element, max_depth=6, _depth=0):
    """
    Recursively walk the AX UI tree and collect visible text.

    Collects AXValue and AXTitle from elements, limited to max_depth
    to avoid crawling massive trees (e.g. web browser DOMs).
    Skips secure text fields (Layer 3 privacy filtering).
    Returns a list of non-empty text strings.
    """
    if _depth > max_depth:
        return []

    # Layer 3: skip password fields and other secure inputs
    if is_secure_field(element):
        return []

    texts = []

    # Collect text from this element
    for attr in ("AXValue", "AXTitle", "AXDescription"):
        val = _ax_attr(element, attr)
        if isinstance(val, str) and val.strip():
            texts.append(val.strip())

    # Recurse into children
    children = _ax_attr(element, "AXChildren")
    if children:
        for child in children:
            texts.extend(_extract_text_from_element(child, max_depth, _depth + 1))

    return texts


# ---------------------------------------------------------------------------
# Core capture function
# ---------------------------------------------------------------------------

def capture_window(pid, app_name, ax_window, window_id):
    """
    Extract content from a specific window identified by pid, app_name,
    AX element, and window_id.

    Privacy layers applied in order:
      1. App blocklist — blocked apps return name only, no content
      2. Window state — hidden apps and private browser windows skipped
      3. Secure field filtering — password fields excluded from AX tree walk
      4. URL scrubbing — sensitive query parameters redacted in final text
      5. Sensitive page detection — known sensitive content filtered

    Returns a dict with keys: ts, app, title, text, pid, window_id, filtered.
    Returns None if the window is hidden or otherwise fully suppressed.
    """
    ts = datetime.now(timezone.utc).isoformat()
    idle_s = round(seconds_since_last_input(), 1)

    # Layer 1: App blocklist — record that the app was used, but read nothing
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

        # For Electron/Chromium apps (Obsidian, VS Code, Slack, etc.),
        # content lives deep inside an AXWebArea node. Find it first
        # and extract text from there with deeper traversal.
        # Layer 3 (secure field skipping) is applied inside _extract_text_from_element.
        web_area = _find_web_area(ax_window, max_depth=_SCREEN_CFG.get("ax_web_area_search_depth", 10))
        if web_area is not None:
            # Extract URL from AXWebArea (works on Chromium, Safari, Electron)
            url_val = _ax_attr(web_area, "AXURL")
            if url_val is not None:
                url_str = str(url_val)
                # Filter out internal Electron/app URLs (file:// to .asar bundles)
                if not url_str.startswith("file://"):
                    url = url_str

            raw_texts = _extract_text_from_element(web_area, max_depth=_SCREEN_CFG.get("ax_web_content_depth", 15))
        else:
            # Native apps: shallower traversal is sufficient
            raw_texts = _extract_text_from_element(ax_window, max_depth=_SCREEN_CFG.get("ax_tree_depth", 5))

        # Deduplicate while preserving order, truncate to avoid huge payloads
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

        # Layer 5: Detect sensitive pages (password managers, banking, etc.)
        # This runs AFTER text extraction so it can inspect the content,
        # but BEFORE returning so sensitive content is never stored.
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


def capture_active_window_safe():
    """
    Wrapper that catches pyobjc/accessibility errors gracefully.
    Returns None on failure instead of crashing.
    """
    try:
        return capture_active_window()
    except Exception as e:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "app": "ERROR",
            "title": "",
            "text": f"capture failed: {e}",
            "pid": -1,
        }


# ---------------------------------------------------------------------------
# Accessibility permission check
# ---------------------------------------------------------------------------

def check_accessibility():
    """Check if this process has Accessibility permission."""
    trusted = AXIsProcessTrusted()
    if not trusted:
        print(
            "ERROR: Accessibility permission not granted.\n"
            "Go to System Settings → Privacy & Security → Accessibility\n"
            "and add your terminal app (Terminal, iTerm2, etc.).",
            file=sys.stderr,
        )
    return trusted


# ---------------------------------------------------------------------------
# Storage: dedup + write to SQLite
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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
