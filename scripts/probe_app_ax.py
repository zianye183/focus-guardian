#!/usr/bin/env python3
"""
Probe any app's AX tree to measure depth and text distribution.
Usage: python3 scripts/probe_app_ax.py "AppName" [--max-depth 15]
"""
import sys
import time
import argparse

from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
)
from AppKit import NSWorkspace


def ax_attr(element, attr):
    err, value = AXUIElementCopyAttributeValue(element, attr, None)
    return value if err == 0 else None


def count_text_by_depth(element, depth=0, max_depth=15):
    """Return dict of {depth: [text_values]} showing where text lives."""
    if depth > max_depth:
        return {}

    results = {}
    role = ax_attr(element, "AXRole") or ""

    for attr in ("AXValue", "AXTitle", "AXDescription"):
        val = ax_attr(element, attr)
        if isinstance(val, str) and val.strip() and len(val.strip()) > 1:
            results.setdefault(depth, []).append((role, attr, val.strip()[:100]))

    children = ax_attr(element, "AXChildren")
    if children:
        for child in children:
            child_results = count_text_by_depth(child, depth + 1, max_depth)
            for d, texts in child_results.items():
                results.setdefault(d, []).extend(texts)

    return results


def find_app_pid(name):
    workspace = NSWorkspace.sharedWorkspace()
    for app in workspace.runningApplications():
        if app.localizedName() == name and app.isFinishedLaunching():
            return app.processIdentifier()
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app_name")
    parser.add_argument("--max-depth", type=int, default=15)
    args = parser.parse_args()

    if not AXIsProcessTrusted():
        print("ERROR: No accessibility permission", file=sys.stderr)
        sys.exit(1)

    pid = find_app_pid(args.app_name)
    if not pid:
        print(f"{args.app_name} not running", file=sys.stderr)
        sys.exit(1)

    print(f"{args.app_name} PID: {pid}")
    app_ref = AXUIElementCreateApplication(pid)
    windows = ax_attr(app_ref, "AXWindows")
    if not windows:
        print("No windows")
        sys.exit(1)

    for i, win in enumerate(windows[:3]):  # first 3 windows max
        title = ax_attr(win, "AXTitle") or "(untitled)"
        print(f"\n=== Window {i}: {title} ===")
        results = count_text_by_depth(win, max_depth=args.max_depth)
        total_texts = 0
        for depth in sorted(results.keys()):
            texts = results[depth]
            total_texts += len(texts)
            print(f"  Depth {depth:2d}: {len(texts):3d} text nodes")
            for role, attr, val in texts[:3]:  # show first 3 samples
                print(f"           [{role}].{attr} = {val}")
            if len(texts) > 3:
                print(f"           ... and {len(texts) - 3} more")
        print(f"  TOTAL: {total_texts} text nodes, max depth with text: {max(results.keys()) if results else 0}")


if __name__ == "__main__":
    main()
