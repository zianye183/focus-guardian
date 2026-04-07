# Focus Guardian — Multi-App Capture & Layout/Content Split

> Follow-up to V2 Data Layer Design.
> Generated from design session on 2026-04-06.
> Branch: master

## How We Got Here

### The starting question: what about split-screen?

The V2 data model assumes one app per capture — the frontmost application. But users
routinely split the screen: docs on the left, code on the right, Slack in a corner.
These apps form a **workspace** together. Capturing only the frontmost one loses the
context of what else was visible and being referenced.

This raises a concrete question: can we capture *every app visible on the current
screen* simultaneously?

### Answer: yes, macOS exposes this

`CGWindowListCopyWindowInfo` returns every on-screen window with app name, PID,
bounds (position + size), and layer. We can know exactly which apps share the screen
at any moment. The data is there.

### The data model breaks

The real problem isn't capture — it's storage. The V2 schema is a flat stream:

```
{ts, app, title, text, ...}
{ts, app, title, text, ...}
```

One timestamp, one record, one app. Multi-app capture needs a **snapshot** concept:
one timestamp maps to multiple apps. Three options emerged:

| Option | Description | Tradeoffs |
|--------|-------------|-----------|
| A. Snapshot with references | Each snapshot lists all visible panes; unchanged panes reference their last content entry | Couples layout and content; reference management adds complexity |
| B. Separate streams | Layout table (which apps are visible) + content table (per-app text, deduped independently) | Clean separation; simple dedup; easy to query |
| C. Event-sourced | Only store deltas: "window appeared," "content changed," "window disappeared" | Most storage-efficient; hardest to query; state reconstruction required |

### Decision: Option B — Layout + Content split

Two independent append-only tables, joined at query time.

**Why this wins:**

1. **Layout changes infrequently.** You split-screen and stay there for minutes or
   hours. The layout table is sparse — maybe a few entries per hour.

2. **Content changes at different rates per app.** Your code editor churns while the
   reference doc is static. Independent per-app dedup handles this naturally.

3. **Dedup is simple.** Each app's content stream compares only against that app's
   previous entry. No cross-app comparison needed.

4. **Querying is straightforward.** "What was on screen at time T?" = get layout at T,
   then get latest content for each app at or before T. Standard "as-of" query pattern.

---

## The Dedup Problem This Solves

The V2 design had a subtle flaw: quickly switching between apps A and B (without
changing either app's content) defeated dedup.

**Why:** The old single-stream dedup compared each new record against the *last record
regardless of app*. So A → B → A produced three records even though neither app's
content changed. The dedup saw A, then B (different! write it), then A again
(different from B! write it).

**The fix is structural, not algorithmic.** With separate tables:

- **Content table:** Each app's stream dedupes against its own previous entry.
  A's content hasn't changed → no new content record for A. B's content hasn't
  changed → no new content record for B. Switching between them writes nothing
  to the content table.

- **Layout table:** Records which apps are visible. App switches write here.
  This is the correct place for that signal — it's a layout change, not a
  content change.

No extra dedup logic needed. The two-table design resolves it by separating concerns.

---

## Timestamp Alignment

A natural concern: layout and content tables will have different timestamps.
The layout changes when you rearrange windows; content changes on the heartbeat.
These won't align.

**This is fine.** Both tables are append-only event logs. Reconstruction uses
"as-of" queries:

```sql
-- 1. Layout at time T (most recent layout entry ≤ T)
SELECT * FROM layout WHERE ts <= ?T ORDER BY ts DESC LIMIT 1;
-- → [VS Code, Safari]

-- 2. For each app in that layout, latest content ≤ T
SELECT * FROM content
WHERE app = 'VS Code' AND ts <= ?T
ORDER BY ts DESC LIMIT 1;

SELECT * FROM content
WHERE app = 'Safari' AND ts <= ?T
ORDER BY ts DESC LIMIT 1;
```

It doesn't matter that Safari's content was captured at T-45s and VS Code's at T-2s.
The query returns the correct state at T regardless.

---

## Transition Captures Are No Longer Needed

The V2 design used an `NSWorkspace` observer to fire instant captures on app switch
(tagged `"transition": true`). This created uneven, messy recordings interleaved
with the regular 3-second heartbeat.

With the layout/content split, transitions are handled cleanly:

- **Layout table:** Gets a new entry when visible apps change. This is the
  transition signal.
- **Content table:** Keeps ticking on the heartbeat. No forced off-cycle captures.
  The next heartbeat picks up the new app's content within 3 seconds.

3 seconds of latency is acceptable for analytics. The layout table records *when*
the switch happened with precision. The content table records *what was there*
on the next tick.

---

## Revised Schema

Replaces the `captures` table from V2.

```sql
-- Which apps are visible on screen at each point in time.
-- New entry only when the set of visible apps changes.
CREATE TABLE layout (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,                -- ISO8601 UTC
    panes TEXT NOT NULL              -- JSON array: [{app, pid, bounds}]
);
CREATE INDEX idx_layout_ts ON layout(ts);

-- Per-app content captures, deduped independently per app.
-- Written on the heartbeat; skipped if content unchanged from
-- that app's previous entry.
CREATE TABLE content (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,                -- ISO8601 UTC
    app TEXT NOT NULL,
    pid INTEGER,
    title TEXT NOT NULL,
    text TEXT,
    text_raw TEXT,                   -- pre-normalization (debugging)
    url TEXT,
    idle_s REAL,
    idle BOOLEAN DEFAULT FALSE
);
CREATE INDEX idx_content_ts ON content(ts);
CREATE INDEX idx_content_app_ts ON content(app, ts);
```

The downstream tables (`sessions`, `session_summaries`) and FTS5 indexes from V2
remain unchanged — they consume from `content` the same way they consumed from
`captures`.

---

## Capture Loop Changes

### Current (V2)

```
every 3s:
  get frontmost app
  capture content
  dedup against last record (any app)
  write if changed
```

### Proposed (V3)

```
every 3s:
  get all visible windows (CGWindowListCopyWindowInfo)
  for each visible app:
    capture content
    dedup against that app's last content entry
    write to content table if changed
  compare visible app set against last layout entry
  write to layout table if set changed
```

### Key API

```python
from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly, kCGNullWindowID

windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
# Returns list of dicts with: kCGWindowOwnerName, kCGWindowOwnerPID,
# kCGWindowBounds, kCGWindowLayer, kCGWindowName, etc.
```

Filter to layer 0 (normal windows) and exclude system UI (menubar, Dock,
Notification Center) by owner name.

---

## What Changes from V2

| Aspect | V2 | V3 | Why |
|--------|----|----|-----|
| Capture target | Frontmost app only | All visible apps | Split-screen context |
| Storage model | Single `captures` table | `layout` + `content` tables | Separate concerns |
| Dedup comparison | Against last record (any app) | Against last record for *same app* | Correct per-app dedup |
| App-switch handling | Instant transition capture + heartbeat | Layout table entry + heartbeat | Cleaner, no uneven recordings |
| Frontmost detection | AppleScript subprocess | `CGWindowListCopyWindowInfo` | Already needed for multi-window; replaces osascript |

## What Stays the Same

- 3-second heartbeat interval
- Idle detection via `CGEventSourceSecondsSinceLastEventType`
- Privacy filtering layers (blocklist, private windows, secure fields, URL scrubbing, sensitive pages)
- Normalization → dedup → sessions → summaries pipeline
- All downstream consumers (push system, pull system, reflection)
- `soul.yaml`, `rules.yaml`, judgment prompt

---

## Design Decisions Still Open

1. **Window filtering heuristics.** `CGWindowListCopyWindowInfo` returns *everything*:
   menu bar, Dock, Spotlight, notification banners, picture-in-picture overlays.
   Need to define which windows count as "visible apps." Layer 0 + minimum size
   threshold + owner name exclusion list is the likely approach.

2. **Bounds tracking granularity.** The `panes` JSON in the layout table could store
   full bounds `{x, y, w, h}` or just app presence `[app1, app2]`. Full bounds
   enables "which app occupied more screen space" analysis but increases storage.
   Start with full bounds; simplify later if unused.

3. **AX tree traversal cost.** Currently we walk the AX tree for one app per tick.
   With 2-3 visible apps, this multiplies. May need to stagger: capture layout
   every tick, but rotate which app gets a full AX tree walk. Or accept the cost
   if it stays under ~100ms total.

4. **Content capture for background apps.** AX APIs can read content from non-frontmost
   windows, but some apps may not expose their full tree unless focused. Need to
   test with common split-screen combos (browser + editor, browser + terminal).

5. **Migration from V2.** Existing `captures` data can be migrated to the `content`
   table directly (it's the same shape). Layout table starts empty — no historical
   layout data exists.
