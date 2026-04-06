# Focus Guardian — Data Layer & AI Pipeline Design (V1 Revision)

> Follow-up to the original Design Doc and Implementation Guide.
> Generated from office-hours session on 2026-04-06.
> Branch: master

## How We Got Here: Conversation Trail

### Starting point: Session 2 — AW Integration

We began this session with a straightforward goal: build `aw_client.py` to integrate
ActivityWatch as the "skeleton" data stream alongside our screen reader ("the flesh"),
as planned in the original design doc.

We built it. REST client primary, direct DB fallback. Tested against the live AW
instance. It worked. Then we started asking harder questions.

### Question 1: Do we actually need AW?

The original design assumed two data streams merged by `buffer.py`. But when we
compared what AW provides (app + title + duration) versus what screen_reader.py
provides (app + title + text + URL + idle), the overlap was almost total. AW's
unique contributions:

- **Precise duration tracking** — but 3s sampling approximates this well enough
- **AFK detection** — this was the strongest argument for keeping AW

We investigated AW's AFK watcher source code. On macOS, the entire implementation is:

```python
from Quartz.CoreGraphics import (
    CGEventSourceSecondsSinceLastEventType,
    kCGEventSourceStateHIDSystemState,
    kCGAnyInputEventType,
)

def seconds_since_last_input() -> float:
    return CGEventSourceSecondsSinceLastEventType(
        kCGEventSourceStateHIDSystemState, kCGAnyInputEventType
    )
```

One function call. We already have `pyobjc-framework-Quartz` in requirements.

**Decision: Drop AW as a dependency.** Added `idle_s` field to every capture and
built idle detection directly into screen_reader.py. `aw_watcher.py` stays in the
repo but is not part of the active pipeline.

### Question 2: How should idle detection work?

We debated several options for idle handling. The key tension: `idle_s` alone doesn't
distinguish "watching a video" from "walked away from computer" — both show low/no
input, but the user's presence is different.

We also discussed how the current dedup (exact string match on app+title+text) interacts
with idle: if you watch a 30-minute video without touching anything, dedup would write
one record and then nothing until you switch away. The `idle_s` field would only appear
on that first record.

**Decision: Two-level system.**
1. `idle_s` as a field on every capture (continuous signal for the judgment engine)
2. Hard idle threshold (180s, configurable) — emit one idle marker, pause captures
   until input resumes

The hope: during video watching, you still touch the trackpad occasionally (keeping
`idle_s` low), while walking away produces sustained high `idle_s` that crosses the
threshold. The dedup handles the "same content" problem; idle detection handles the
"nobody home" problem.

### Question 3: What does the real data look like?

We ran screen_reader.py continuously via tmux for two days. The results exposed
fundamental problems with the naive capture-and-dedup approach:

**April 4:** 1,326 records, 6.3 MB, 65% consecutive same app+title
**April 5:** 3,058 records, 12.2 MB, 84% consecutive same app+title

Three dedup-defeating patterns:

| Pattern | Records | Root cause |
|---------|---------|------------|
| WeChat video call (90 min) | 1,106 | Call timer changes every 3s: `"00:03"` → `"00:06"` |
| Obsidian note editing | 508 | Typing changes text by ~17 chars/capture; status bar word count changes |
| Bilibili video (17 min) | 290 | Video timestamp + comment counter change constantly |

All three share the same root cause: **dynamic UI elements** (timers, counters,
status bars) change the captured text by a few characters each cycle, defeating
exact string comparison.

At this rate: ~18 MB/day, ~6.5 GB/year of raw captures. Not catastrophic for storage,
but unacceptable for LLM context consumption.

### Question 4: What do the AI consumers actually need?

We brainstormed the three AI processes that consume this data:

**Push system (real-time nudges):** Needs the last 5-10 minutes of activity context
+ soul.yaml + calendar/todos. Must be fast and bounded. This is the primary consumer
and the hardest constraint to satisfy.

**Pull system (queries + evening reflection):** "What did I do today?" Needs
searchable history. Evening reflection needs the full day's data to discover
patterns, update soul.yaml, and recompile trigger rules.

**Session summaries (periodic):** Compact AI-generated descriptions of activity
periods. These become the primary interface for both push and pull — raw captures
are the backing store, summaries are the working set.

Key insight from the discussion: **the push system doesn't need RAG or vector search.**
Its context is bounded (recent window + soul.yaml). Historical context comes from
soul.yaml (patterns compiled by reflection) and optionally from DB queries into past
session summaries when the judgment LLM decides it needs specific historical evidence.

The pull system also doesn't need embeddings for V1. Activity data is inherently
temporal and structured — time-range queries + full-text search (FTS5) cover the
search needs. "When did I work on the paper?" is a SQL query, not a cosine similarity.

### Question 5: JSONL or SQLite?

The original design assumed daily JSONL buffer files forever. Two days of real data
made the case for SQLite:

- Every downstream consumer needs time-range + app-filtered queries
- JSONL requires parsing the entire file for any query
- SQLite gives indexed reads, aggregations, FTS5, and is already proven at this
  scale (ActivityWatch uses it with peewee)
- Already in our dependency tree via aw-core
- Single file on disk, no server, built into Python stdlib

**Decision: SQLite replaces JSONL** as the storage backend, before building anything
that reads from the buffer.

### Question 6: What about VS Code?

We investigated why VS Code captures showed only toolbar text (`Go Back | Go Forward |
Agent Status`). Found that VS Code's AXWebArea is reachable at depth 10 (our current
config), and text extraction works — but the editor content is rendered via GPU canvas,
not DOM nodes exposed through Accessibility APIs. The AX tree contains the full
sidebar, file explorer, status bar, and toolbar, but not the code itself.

**Decision: Accept for V1.** The window title (`screen_reader.py — focus-guardian`)
is a strong enough signal for "user is coding in this file." Obsidian and browsers
expose full content. VS Code and terminals expose metadata only. This matches the
design doc's "Level 1.5" scope.

### Codex second opinion

An independent Codex review of the session validated the direction and added:

- **"Normalize before dedup"** — strip dynamic UI noise (timers, counters, badges)
  before comparing, rather than tolerating it with a loose similarity threshold.
  Attacks root cause, not symptoms.
- **4-layer data model:** `captures_raw → segments → sessions → session_summaries`.
  The `segments` table (deduplicated activity periods with canonical text) is the
  working unit between raw captures and sessions.
- **FTS5** over segments and summaries for pull queries — no vector DB needed.
- **Screenpipe** as closest OSS (local screen capture + text extraction + search).
  We disagree on adopting it — our AX-based approach is lighter and more
  privacy-friendly than Screenpipe's OCR approach. But worth watching.
- **"Build the segment compressor first. Validate on the three ugly cases."**

---

## Revised Architecture

### Data Flow

```
[macOS AX APIs + idle detection]
        │
        ▼
  screen_reader.py          (capture every 3s, app+title+text+url+idle_s)
        │
        ▼
  normalizer                (strip timers, counters, status bars, badges)
        │
        ▼
  dedup                     (similarity threshold ~80-90%, forced snapshot every 30-60s)
        │
        ▼
  SQLite: captures          (sparse, clean records)
        │
        ▼
  session_grouper           (AI-based: group captures into coherent activity sessions)
        │
        ▼
  SQLite: sessions          (start/end, app, title, category)
        │
        ▼
  session_summarizer        (Haiku: one-line summary per session)
        │
        ▼
  SQLite: session_summaries (compact, LLM-consumable)
        │
        ├──→ push system    (rolling buffer + soul.yaml + calendar + recent summaries → Haiku judgment)
        │
        ├──→ pull system    (user queries + FTS5 search over summaries)
        │
        └──→ reflection     (full day's sessions → pattern analysis → soul.yaml update → rules recompile)
```

### SQLite Schema

```sql
-- Raw captures (sparse, after normalization + dedup)
CREATE TABLE captures (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,              -- ISO8601 UTC
    app TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT,                     -- normalized visible content
    text_raw TEXT,                 -- original text before normalization (for debugging)
    url TEXT,
    idle_s REAL,
    idle BOOLEAN DEFAULT FALSE,   -- idle marker
    pid INTEGER
);
CREATE INDEX idx_captures_ts ON captures(ts);
CREATE INDEX idx_captures_app ON captures(app);

-- Sessions (grouped captures representing one coherent activity)
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    app TEXT NOT NULL,
    title TEXT,
    url TEXT,
    category TEXT,                 -- work, distraction, communication, break, etc.
    capture_count INTEGER,
    duration_s REAL
);
CREATE INDEX idx_sessions_ts ON sessions(start_ts);
CREATE INDEX idx_sessions_category ON sessions(category);

-- AI-generated session summaries
CREATE TABLE session_summaries (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    summary TEXT NOT NULL,         -- one-line AI summary
    summary_json TEXT,             -- structured summary (optional, for machine consumption)
    model TEXT,                    -- which model generated this
    tokens INTEGER,               -- token count for cost tracking
    created_ts TEXT NOT NULL
);
CREATE INDEX idx_summaries_session ON session_summaries(session_id);

-- Full-text search over summaries
CREATE VIRTUAL TABLE session_summaries_fts USING fts5(
    summary,
    content=session_summaries,
    content_rowid=id
);
```

### Module Breakdown

| Module | Role | AI involvement |
|--------|------|---------------|
| `screen_reader.py` | Capture every 3s via AX APIs + idle detection | None |
| `normalizer.py` | Strip dynamic UI noise (timers, counters, badges, status bars) | None — rules-based |
| `db.py` | SQLite connection, schema management, read/write helpers | None |
| `dedup.py` | Similarity comparison, periodic forced snapshots | None — SequenceMatcher |
| `session_grouper.py` | Group captures into sessions | **AI-based** (Haiku) — more flexible than rules |
| `session_summarizer.py` | Generate one-line summaries per session | **AI-based** (Haiku) |
| `judgment.py` | Package context, decide intervene/silence | **AI-based** (Haiku) |
| `notifier.py` | macOS notifications, capture user response | None |
| `reflection.py` | Evening 3-step chain: patterns → soul → rules | **AI-based** (Sonnet) |
| `daemon.py` | Orchestrate capture loop + push system | None |

### Revised Build Order

| Step | What to build | Why this order |
|------|---------------|----------------|
| 1 | `normalizer.py` + improved dedup | Validate on existing 2-day JSONL dataset. Must handle WeChat calls, Obsidian editing, Bilibili videos. |
| 2 | `db.py` + SQLite schema | Storage foundation. Migrate existing JSONL data as validation. |
| 3 | `session_grouper.py` | AI-based grouping of captures into sessions. Needs real data in SQLite. |
| 4 | `session_summarizer.py` | Haiku generates summaries per session. Needs sessions to exist. |
| 5 | Pull system: `reflection.py` + query interface | Evening reflection consumes sessions/summaries. Query interface for "what did I do today?" |
| 6 | Push system: `rules.py` + `judgment.py` + `notifier.py` + `daemon.py` | The product. Consumes rolling buffer + soul.yaml + calendar + session summaries. |

Steps 1-2 are data infrastructure. Steps 3-4 are the AI processing pipeline.
Steps 5-6 are the two consumer systems. The push system comes last because it
needs everything upstream to be working.

---

## Comparison to Original Roadmap

### What changed

| Aspect | Original | Revised | Why |
|--------|----------|---------|-----|
| Data sources | AW + screen reader (two streams) | Screen reader only (one stream) | AW is redundant; idle detection is one function call |
| Storage | Daily JSONL files | SQLite with indexed tables | Real data proved JSONL unqueryable at scale |
| Dedup | Exact string match | Text normalization + similarity threshold | Dynamic UI elements defeat exact matching |
| Data model | Raw captures → judgment | Raw → normalize → dedup → sessions → summaries → judgment | New intermediate processing layers needed |
| Session concept | None | First-class entity with AI grouping + summaries | Judgment and reflection need compact activity descriptions, not raw text dumps |
| Pull queries | Only evening reflection | Reflection + interactive queries + FTS5 | Full queryable history is a first-class consumer |
| Build order | Capture → judgment → notification → reflection | Capture → data pipeline → pull system → push system | Data layer must be right before anything downstream works |
| AI in data path | Only at judgment (Tier 2) and reflection | Also in session grouping and summarization | Session boundaries and summaries require judgment, not just rules |

### What stayed the same

- `screen_reader.py` as the capture engine
- `soul.yaml` as the co-evolving user model
- `rules.yaml` as declarative trigger rules, LLM-maintained
- Two-tier nervous system: cheap Tier 1 detection → expensive Tier 2 judgment
- Evening reflection as 3-step chain (patterns → soul → rules)
- "The judgment prompt is the product"
- Graceful degradation everywhere
- One module per session, test with real data early

---

## Design Decisions Still Open

1. **Session grouper: AI vs rules?** We leaned AI-based (Haiku) for flexibility.
   But a rules-based grouper (split on app+title change, merge if gap < 2 min)
   might be good enough and cheaper. Need to test both approaches.

2. **Normalization rules:** What exactly to strip? Timers (`00:03 / 15:27`),
   word counts (`339 words`), comment counters, video progress bars, spinner
   characters. Need to catalog these from real data and build the ruleset.

3. **rules.py in the push system:** Does it stay as a standalone YAML rule
   evaluator (Tier 1), or does the session grouper + push system absorb that
   responsibility? The original design has rules as cheap always-on detection
   that promotes to LLM judgment. This still makes sense but needs to interface
   with the new session/summary model.

4. **Max text length:** Currently 20,000 chars. Should probably drop to 2,000-4,000
   given that most useful signal is in the first few hundred chars (title, headings,
   opening content). Reduces storage without meaningful signal loss.

5. **Summarization frequency:** Per-session (when session closes), periodic (every
   1-2 hours), or both? Per-session is cleaner but requires good session boundary
   detection. Periodic is simpler but produces arbitrary time-slice summaries.

6. **MCP integrations:** Calendar (mcp-ical), email, to-do (Things3) are needed
   for the judgment prompt to produce personalized nudges. Timing TBD.

---

## Success Criteria (unchanged from original)

- V1 ships and you leave it running
- You don't turn it off after day 3
- soul.yaml has real patterns after 2 weeks
- The push system fires <10 times per day
- You catch yourself thinking "how did it know?" at least once

## Key Data Targets

- Daily storage after dedup: <1 MB (down from current 12-18 MB)
- Records per day after dedup: <200 (down from current 3,000+)
- Judgment prompt context: <8K tokens (segments + soul.yaml + calendar)
- Session summaries: 1 line each, <50 tokens
