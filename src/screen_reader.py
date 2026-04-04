"""
Screen text reader for Focus Guardian.

Reads visible text + app name from the active window using macOS
Accessibility APIs via pyobjc. Uses System Events via AppleScript to
detect the true frontmost application (not just the terminal running
this script). Outputs JSONL records to stdout or appends to a buffer file.

Requires: macOS Accessibility permission in
System Settings → Privacy & Security → Accessibility
(grant to Terminal / iTerm / whatever runs this script)
"""

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
from privacy import is_app_blocked, is_private_window, is_app_hidden, is_secure_field, is_sensitive_page, scrub_url, scrub_text_urls

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
# Frontmost app detection
# ---------------------------------------------------------------------------

def _get_frontmost_app():
    """
    Get the true frontmost application via System Events AppleScript.

    NSWorkspace.frontmostApplication() and isActive() return the app
    that owns the calling process (our terminal), not the user's actual
    foreground app. System Events tracks the real frontmost process
    independently, so we use AppleScript as the primary method.
    """
    try:
        result = subprocess.run(
            ['osascript', '-e',
             'tell application "System Events"\n'
             '  set frontApp to first application process whose frontmost is true\n'
             '  return {name of frontApp, unix id of frontApp}\n'
             'end tell'],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(", ")
            if len(parts) == 2:
                name = parts[0]
                pid = int(parts[1])
                return {"name": name, "pid": pid}
    except Exception:
        pass

    # Fallback: NSWorkspace (less reliable from a terminal process)
    workspace = NSWorkspace.sharedWorkspace()
    for app in workspace.runningApplications():
        if app.isActive():
            return {
                "name": app.localizedName(),
                "pid": app.processIdentifier(),
            }

    return None


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

def capture_active_window():
    """
    Capture the active window's app name, title, and visible text.

    Privacy layers applied in order:
      1. App blocklist — blocked apps return name only, no content
      2. Window state — hidden apps and private browser windows skipped
      3. Secure field filtering — password fields excluded from AX tree walk
      4. URL scrubbing — sensitive query parameters redacted in final text

    Returns a dict with keys: ts, app, title, text, pid, filtered.
    Returns None if no active app is found or if the window is filtered.
    """
    frontmost = _get_frontmost_app()

    if frontmost is None:
        return None

    app_name = frontmost["name"]
    pid = frontmost["pid"]
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
            "idle_s": idle_s,
            "filtered": "app_blocked",
        }

    # Layer 2a: Hidden app detection
    if is_app_hidden(pid):
        return None

    # Create AX element for the app
    ax_app = AXUIElementCreateApplication(pid)

    # Get the focused window
    focused_window = _ax_attr(ax_app, "AXFocusedWindow")
    window_title = ""
    visible_text = ""
    url = None

    if focused_window is not None:
        title_val = _ax_attr(focused_window, "AXTitle")
        if isinstance(title_val, str):
            window_title = title_val.strip()

        # Layer 2b: Private browser window detection
        if is_private_window(app_name, window_title, ax_window=focused_window):
            return {
                "ts": ts,
                "app": app_name,
                "title": "[private window]",
                "text": "",
                "url": None,
                "pid": pid,
                "idle_s": idle_s,
                "filtered": "private_window",
            }

        # For Electron/Chromium apps (Obsidian, VS Code, Slack, etc.),
        # content lives deep inside an AXWebArea node. Find it first
        # and extract text from there with deeper traversal.
        # Layer 3 (secure field skipping) is applied inside _extract_text_from_element.
        web_area = _find_web_area(focused_window, max_depth=_SCREEN_CFG.get("ax_web_area_search_depth", 10))
        if web_area is not None:
            # Extract URL from AXWebArea (works on Chromium, Safari, Electron)
            url_val = _ax_attr(web_area, "AXURL")
            if url_val is not None:
                url = str(url_val)

            raw_texts = _extract_text_from_element(web_area, max_depth=_SCREEN_CFG.get("ax_web_content_depth", 15))
        else:
            # Native apps: shallower traversal is sufficient
            raw_texts = _extract_text_from_element(focused_window, max_depth=_SCREEN_CFG.get("ax_tree_depth", 5))

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
        "idle_s": idle_s,
    }


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
# Output helpers
# ---------------------------------------------------------------------------

def record_to_jsonl(record):
    """Serialize a capture record to a JSONL line."""
    return json.dumps(record, ensure_ascii=False)


def append_to_buffer(record, buffer_dir="data/buffer"):
    """Append a record to today's buffer file."""
    today = datetime.now().strftime("%Y-%m-%d")
    buffer_path = Path(buffer_dir) / f"{today}.jsonl"
    buffer_path.parent.mkdir(parents=True, exist_ok=True)
    with open(buffer_path, "a", encoding="utf-8") as f:
        f.write(record_to_jsonl(record) + "\n")


# ---------------------------------------------------------------------------
# Main: continuous capture loop
# ---------------------------------------------------------------------------

def _emit(record, buffer_dir, verbose):
    """Write a record to buffer and/or stdout."""
    if buffer_dir:
        append_to_buffer(record, buffer_dir)
    if verbose:
        print(record_to_jsonl(record))


def run_continuous(interval=3.0, buffer_dir=None, verbose=False):
    """
    Capture active window every `interval` seconds with idle detection.

    Behavior:
      - Every capture includes idle_s (seconds since last input).
      - Dedup: if app + title + text unchanged, skip the write.
      - Idle threshold: when idle_s >= timeout, emit one idle marker
        and pause captures until input resumes.
      - On resume: emit a normal capture immediately.

    The buffer timeline shows:
      - Normal records with idle_s for continuous activity signal
      - An idle marker when the user walks away
      - A gap (no records) during absence
      - A normal record when the user returns
    """
    if not check_accessibility():
        sys.exit(1)

    idle_timeout = _SCREEN_CFG.get("idle_timeout_seconds", 180)

    print(
        f"Screen reader running (interval={interval}s, idle_timeout={idle_timeout}s). "
        "Ctrl+C to stop.",
        file=sys.stderr,
    )

    last_record = None
    is_idle = False

    while True:
        idle_s = round(seconds_since_last_input(), 1)

        # --- Idle state: user has walked away ---
        if idle_s >= idle_timeout:
            if not is_idle:
                # Transition to idle: emit one marker
                idle_record = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "app": last_record["app"] if last_record else "Unknown",
                    "title": last_record["title"] if last_record else "",
                    "text": "",
                    "url": None,
                    "pid": last_record["pid"] if last_record else -1,
                    "idle_s": idle_s,
                    "idle": True,
                }
                _emit(idle_record, buffer_dir, verbose)
                is_idle = True
            # Don't capture while idle — just wait
            time.sleep(interval)
            continue

        # --- Active state: user is present ---
        if is_idle:
            # Just came back from idle
            is_idle = False
            last_record = None  # force next capture to write (no dedup against stale data)

        record = capture_active_window_safe()

        if record is not None:
            # Dedup: skip if app + title + text unchanged
            is_duplicate = (
                last_record is not None
                and record["app"] == last_record["app"]
                and record["title"] == last_record["title"]
                and record["text"] == last_record["text"]
            )

            if not is_duplicate:
                _emit(record, buffer_dir, verbose)
                last_record = record

        time.sleep(interval)


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
        "--buffer-dir", type=str, default=None,
        help="Directory to write daily JSONL buffer files (e.g. data/buffer)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print captures to stdout",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Capture once and print, then exit",
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
            print(f"Waiting {args.delay}s — switch to the app you want to test...", file=sys.stderr)
            time.sleep(args.delay)
        record = capture_active_window_safe()
        if record:
            print(json.dumps(record, indent=2, ensure_ascii=False))
        sys.exit(0)

    run_continuous(
        interval=args.interval,
        buffer_dir=args.buffer_dir,
        verbose=args.verbose,
    )
