#!/usr/bin/env python3
"""
Probe Preview.app's AX tree to understand why PDF text isn't captured.
Run with a PDF open in Preview, e.g.:
    python3 scripts/probe_preview_ax.py --delay 3
"""
import sys
import time
import argparse

from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyAttributeNames,
)
from AppKit import NSWorkspace


def ax_attr(element, attr):
    err, value = AXUIElementCopyAttributeValue(element, attr, None)
    return value if err == 0 else None


def ax_attrs(element):
    err, names = AXUIElementCopyAttributeNames(element, None)
    return list(names) if err == 0 and names else []


def dump_tree(element, depth=0, max_depth=12, indent="  "):
    """Dump the full AX tree with all attributes."""
    if depth > max_depth:
        print(f"{indent * depth}... (max depth)")
        return

    role = ax_attr(element, "AXRole") or "?"
    subrole = ax_attr(element, "AXSubrole") or ""
    title = ax_attr(element, "AXTitle") or ""
    value = ax_attr(element, "AXValue")
    desc = ax_attr(element, "AXDescription") or ""
    role_desc = ax_attr(element, "AXRoleDescription") or ""

    # Truncate long values for readability
    value_str = ""
    if value is not None:
        value_str = str(value)
        if len(value_str) > 200:
            value_str = value_str[:200] + f"... ({len(str(value))} chars)"

    attrs = ax_attrs(element)

    prefix = indent * depth
    print(f"{prefix}[{role}] subrole={subrole} roleDesc={role_desc}")
    if title:
        print(f"{prefix}  title: {title[:150]}")
    if value_str:
        print(f"{prefix}  value: {value_str}")
    if desc:
        print(f"{prefix}  desc: {desc[:150]}")

    # Show interesting attributes not already printed
    interesting = set(attrs) - {
        "AXRole", "AXSubrole", "AXTitle", "AXValue", "AXDescription",
        "AXRoleDescription", "AXChildren", "AXParent", "AXWindow",
        "AXTopLevelUIElement", "AXPosition", "AXSize", "AXFocused",
        "AXEnabled", "AXFrame",
    }
    for a in sorted(interesting):
        v = ax_attr(element, a)
        if v is not None:
            vs = str(v)
            if len(vs) > 200:
                vs = vs[:200] + f"... ({len(str(v))} chars)"
            # Skip empty/default values
            if vs and vs not in ("0", "0.0", "False", "()", "[]", ""):
                print(f"{prefix}  {a}: {vs}")

    children = ax_attr(element, "AXChildren")
    if children:
        print(f"{prefix}  ({len(children)} children)")
        for child in children:
            dump_tree(child, depth + 1, max_depth, indent)


def find_preview_pid():
    """Find Preview.app's PID."""
    workspace = NSWorkspace.sharedWorkspace()
    for app in workspace.runningApplications():
        if app.localizedName() == "Preview" and app.isFinishedLaunching():
            return app.processIdentifier()
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--max-depth", type=int, default=12)
    parser.add_argument("--pid", type=int, default=None)
    args = parser.parse_args()

    if not AXIsProcessTrusted():
        print("ERROR: No accessibility permission", file=sys.stderr)
        sys.exit(1)

    if args.delay > 0:
        print(f"Waiting {args.delay}s...", file=sys.stderr)
        time.sleep(args.delay)

    pid = args.pid or find_preview_pid()
    if not pid:
        print("Preview.app not running", file=sys.stderr)
        sys.exit(1)

    print(f"Preview.app PID: {pid}")
    app_ref = AXUIElementCreateApplication(pid)

    # Get windows
    windows = ax_attr(app_ref, "AXWindows")
    if not windows:
        print("No windows found")
        sys.exit(1)

    print(f"\n=== {len(windows)} window(s) ===\n")
    for i, win in enumerate(windows):
        title = ax_attr(win, "AXTitle") or "(untitled)"
        print(f"--- Window {i}: {title} ---")
        dump_tree(win, max_depth=args.max_depth)
        print()


if __name__ == "__main__":
    main()
