"""
Test script: prints the frontmost app every 2 seconds for 30 seconds.
Run this, then switch between apps to verify detection works.

Usage:
    source .venv/bin/activate
    python3 test_frontmost.py
"""
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGINT, lambda *_: (print("\nStopped."), sys.exit(0)))

print("Watching frontmost app for 30 seconds. Switch between apps!")
print("Ctrl+C to stop early.")
print("=" * 60)

for i in range(15):
    result = subprocess.run(
        ['osascript', '-e',
         'tell application "System Events"\n'
         '  set frontApp to first application process whose frontmost is true\n'
         '  return {name of frontApp, unix id of frontApp}\n'
         'end tell'],
        capture_output=True, text=True, timeout=2,
    )
    if result.returncode == 0:
        print(f"[{i*2:2d}s] {result.stdout.strip()}")
    else:
        print(f"[{i*2:2d}s] ERROR: {result.stderr.strip()}")
    time.sleep(2)

print("=" * 60)
print("Done.")
