# Focus Guardian - Implementation Guide

> Source: Obsidian vault — "Focus Guardian - Implementation Guide"

## Approach: Data First, Logic Second

The biggest risk is building a judgment system before you understand what the data actually looks like. Get ActivityWatch running, prototype the screen text reader, and look at the raw output for a couple days before writing any trigger logic.

## Project Structure

Build as a pipeline of small, independent modules:

```
focus-guardian/
├── soul.yaml              # SOUL.md — manually seeded, then LLM-maintained
├── rules.yaml             # Trigger rules — default set, then evening reflection maintains
├── config.yaml            # User config (distraction apps, thresholds, API keys)
├── data/
│   ├── buffer/            # Daily JSONL activity buffers (rotated daily)
│   └── soul-backups/      # soul.YYYY-MM-DD.yaml backups
├── src/
│   ├── screen_reader.py   # Reads active window text via pyobjc, outputs JSONL
│   ├── aw_client.py       # Polls ActivityWatch API, outputs structured events
│   ├── buffer.py          # Manages rolling activity buffer (both streams)
│   ├── rules.py           # Evaluates YAML trigger rules against buffer
│   ├── judgment.py        # Packages context, calls Haiku, returns nudge or silence
│   ├── notifier.py        # Sends macOS notifications, captures user response
│   ├── reflection.py      # Evening reflection: full day → updated soul.yaml + rules.yaml
│   └── daemon.py          # Orchestrates the loops, ties everything together
└── prompts/
    ├── judgment.txt       # Judgment prompt template
    └── reflection.txt     # Evening reflection prompt template (3-step chain)
```

Each module does one thing. You can test them independently. `screen_reader.py` works without `judgment.py`. `reflection.py` works on saved data files without the daemon running.

## Build Order (Claude Code Sessions)

| Session | Module | What to build | Why this order |
|---------|--------|--------------|----------------|
| 1 | `screen_reader.py` | Read visible text from active window via pyobjc | Validate pyobjc works, see what data you get |
| 2 | `aw_client.py` | Connect to ActivityWatch REST API | See what structured data ActivityWatch provides |
| 3 | `buffer.py` | Merge both streams into JSONL buffer | Run for a day, collect real data |
| 4 | `soul.yaml` + `judgment.py` | Prompt engineering for "should I intervene?" | The hard part — use real data from session 3 |
| 5 | `notifier.py` | macOS notifications with Thanks/Not now actions | Simple plumbing |
| 6 | `rules.py` + default `rules.yaml` | YAML trigger rule engine | Evaluation logic + bundled starter rules |
| 7 | `daemon.py` | Orchestrate everything, run live | Tie it all together |
| 8 | `reflection.py` | Evening reflection (3-step chain) | Needs several days of real data to be meaningful |

Sessions 1-3 are pure plumbing. Session 4 is where the product lives. Sessions 5-7 are wiring. Session 8 needs real data.

## Module Notes

### screen_reader.py — Session 1

Core snippet to build around:

```python
from AppKit import NSWorkspace
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionOnScreenOnly,
    kCGNullWindowID
)

# Get active app
active_app = NSWorkspace.sharedWorkspace().activeApplication()
app_name = active_app['NSApplicationName']
```

For reading actual window text, use the Accessibility API via `pyobjc-framework-ApplicationServices`. This requires the **Accessibility permission** in System Settings -> Privacy & Security -> Accessibility.

Test this first. If pyobjc accessibility is too painful, fall back to just window titles + ActivityWatch (still useful for V1).

**Output format (JSONL):**
```json
{"ts": "2026-04-04T14:32:05Z", "app": "Google Chrome", "title": "Hacker News", "text": "Show HN: I built a screen-aware..."}
{"ts": "2026-04-04T14:32:08Z", "app": "VS Code", "title": "train_mixture.py", "text": "def forward(self, x, t):..."}
```

### aw_client.py — Session 2

ActivityWatch exposes a local REST API at `http://localhost:5600/api/`.

Key endpoints:
- `GET /api/0/buckets` — list all buckets (watchers)
- `GET /api/0/buckets/{id}/events?limit=N` — recent events from a bucket
- Key buckets: `aw-watcher-window` (app + title), `aw-watcher-web` (URLs from browser extension), `aw-watcher-afk` (idle detection)

### buffer.py — Session 3

Merge both streams into a single append-only JSONL file per day (`data/buffer/YYYY-MM-DD.jsonl`). Each line tagged with source:

```json
{"ts": "...", "source": "screen", "app": "Chrome", "title": "...", "text": "..."}
{"ts": "...", "source": "aw", "event": "app_switch", "from": "Chrome", "to": "VS Code", "duration_s": 340}
```

Run this for at least a full workday before moving to session 4.

### soul.yaml — Define Before Coding

Write this by hand first. This is the contract that `reflection.py` writes to and `judgment.py` reads from.

```yaml
user:
  name: Alan

energy_patterns:
  - peak_focus: "9am-12pm"
  - post_lunch_dip: "1pm-2:30pm"
  - second_wind: "3pm-5pm"

triggers:
  - "After meetings longer than 30min, needs 10-15min decompression"
  - "Twitter browsing during focus blocks usually means fatigue, not laziness"

preferences:
  - "Don't interrupt during VS Code sessions >20min"
  - "Gentle tone, not demanding"

nudge_history:
  effective:
    - "Reminded about deadline during Twitter drift — returned to work in 2min"
  dismissed:
    - "Nudged during post-meeting decompression — too early"
```

### judgment.py — Session 4 (The Hard Part)

This is the product. The prompt engineering for "should I intervene?" matters more than any other code.

Key principles:
- **Most calls should return silence.** If it nudges too often, the whole thing fails.
- **Iterate on the prompt with real data.** Use actual buffer output from session 3, not synthetic examples.
- **The prompt gets: screen text buffer (last 5-10 min) + ActivityWatch summary (last 10-15 min) + soul.yaml + calendar + to-dos.** It returns one of: intervene (with nudge text), stay quiet, or update memory (note a new pattern).
- **Fixed tone in V1** — warm, direct, slightly witty. Baked into the prompt, not a separate personality system.

### notifier.py — Session 5

```python
import subprocess

def notify(title, message):
    subprocess.run([
        'osascript', '-e',
        f'display notification "{message}" with title "{title}" buttons {{"Thanks", "Not now"}}'
    ])
```

Or use `terminal-notifier` for actionable notifications with callbacks. Capture which button was pressed, log it with the nudge context.

- "Not now" -> extend cooldown to 30 min
- "Thanks" -> reset cooldown to 10 min

### reflection.py — Session 8

Run manually first. Write a script that takes today's data files and runs the three-step chain:

1. **Pattern analysis** — LLM reviews ActivityWatch quantitative data + screen text qualitative data. Identifies behavioral patterns. (text output)
2. **SOUL.md update** — LLM takes current soul.yaml + pattern analysis, outputs updated soul.yaml. (YAML output, validated)
3. **Rule compilation** — LLM takes updated soul.yaml + pattern analysis, outputs trigger rules. (YAML output, validated)

Review the output yourself. Tune the prompts. Only automate into a cron job once you trust the output.

### daemon.py — Session 7

Just a `while True` loop with two timers and a trigger evaluator. Keep it dumb. The intelligence is in the prompts, not the orchestration.

```python
# Pseudocode
while True:
    screen_data = screen_reader.capture()
    buffer.append(screen_data)

    if time_for_aw_poll():
        aw_data = aw_client.poll()
        buffer.append(aw_data)

    triggered = rules.evaluate(buffer, current_rules)

    if triggered and not in_cooldown():
        result = judgment.judge(buffer, soul, calendar, todos)
        if result.action == "intervene":
            response = notifier.notify(result.nudge)
            update_cooldown(response)

    sleep(2)  # screen reader interval
```

## Key Practices

1. **One module per Claude Code session.** Don't scaffold the whole project at once. Build, test, verify, move on.
2. **Test with real data early.** The prompt engineering is the product. You need actual examples of your screen behavior to iterate on it.
3. **The judgment prompt is the product.** Spend more time on it than on any code. It needs to be excellent at saying "nothing worth saying right now."
4. **Don't over-engineer the daemon.** It's a loop. The intelligence is in the prompts.
5. **Evening reflection: manual first.** Don't automate until you trust the output.
6. **soul.yaml format is a contract.** Define it by hand before any code reads or writes it. Validate YAML output from LLM before writing.
7. **Graceful degradation everywhere.** ActivityWatch down? Keep running, skip AW data. API down? Skip nudges, keep logging. Things3 unavailable? Judge without to-do context.
