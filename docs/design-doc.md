# Focus Guardian - AI Companion Design Doc

> Source: Obsidian vault — "Focus Guardian - AI Companion Design Doc"

## Problem Statement

Existing tools in this space all get something wrong:
- **Screen time trackers** (RescueTime, Timing.app) report after the fact. No real-time intervention.
- **Focus apps** (Opal, Forest, Flow) block bluntly. They don't understand context.
- **AI assistants** (Claude Code, Copilot) help with tasks but don't know your day.
- **Screen recorders** (Rewind, Recall) capture everything but don't act on it.
- **Littlebird.ai** ($11M raised, March 2026) reads screen content as text, makes it queryable. Closest to the vision, but it's still a **pull** system. You ask it questions. It doesn't come to you.

The gap: nobody combines screen awareness + personal behavioral model + proactive intervention + personality. The product that fills this gap isn't a tool. It's a companion with a model of *you*.

## Core Vision

The "whoa" moment: it catches you *before* you spiral. Not "you spent 2 hours on Twitter today" but "hey, you've been drifting for 10 minutes and you have a deadline in 2 hours. Want to take a walk? You'll still have time."

The 10x version: cross-context awareness. It connects dots across your code, notes, conversations, and browsing. It sees patterns you can't because your attention is fragmented across tabs and apps.

The personality is load-bearing, not decoration. If the companion feels like a notification system, it fails. It has to feel like someone who genuinely knows you.

## Agreed Premises

1. **Push > pull.** Proactive intervention is more valuable than queryable context.
2. **Personality is structural.** If it feels like a notification, it's dead.
3. **Two separate components** (meta-companion + workspace assistant) with a shared context bus. Not one monolithic product.
4. **V1 uses structured memory** (SOUL.md, behavioral logs) as context for a base model. LoRA / finetuning is a research track for later.
5. **V1 observes and advises only.** Computer use (closing tabs, taking actions) is V2. Trust before agency.

## Architecture: The Push System

The hardest problem in this design is how the push system works without a naive LLM polling loop. A looping LLM call every 1-5 minutes is:
- Artificial and rigid (fragments mental energy, doesn't match human rhythms)
- Computationally expensive and not commercially/individually feasible

Rules-based systems are too weak. The solution is a **dual data stream** feeding a **two-tier nervous system** with an **LLM-as-compiler** model.

### Data Streams: The Skeleton and the Flesh

Two complementary streams run continuously:

**ActivityWatch = the skeleton.** Structured, quantitative, complete. Every app switch, every URL, every duration, timestamped. Machine-readable. No gaps, no interpretation needed. "You spent 47 minutes in VS Code, 12 minutes on Twitter, 8 minutes in Slack." This is the backbone for evening reflection and quantitative pattern discovery.

**Screen text reader = the flesh.** Qualitative, contextual. Every 2-5 seconds, reads the visible text + app name from the active window (via macOS Accessibility APIs or `pyobjc`). Appends to a JSONL buffer, rotated daily. This is what tells the LLM *what it meant*: were those 47 minutes of VS Code deep work on `train_mixture.py`, or staring at a blank file? Were those 12 minutes of Twitter doomscrolling, or reading a thread about your research?

**Importance hierarchy in screen text:** Not all screen content is equal. The LLM learns to weight:
- Meeting notes, project plans, to-do lists -> high importance. These define "the plan" and should be remembered.
- Code editor content -> medium importance. Tells you what the user is working on.
- Browser content -> varies. Docs/research = on-task. Social media = potential drift.
- The LLM can learn what "important" means for *you* over time via SOUL.md.

Together: ActivityWatch tells you **what happened**. Screen text tells you **what it meant**. Evening reflection gets both.

### Tier 1: Detection (cheap, always-on)

The daemon runs two loops:
1. **ActivityWatch poll** (every 5 seconds): structured app/URL/duration data. Change detection against previous poll.
2. **Screen text capture** (every 2-5 seconds): visible text + app name from active window. Appended to JSONL buffer.

Both feed into threshold-based evaluation. Events that trigger checks:
- App switched
- Idle for N minutes
- Distraction app opened during a focus block
- Calendar event starting soon
- Long focused session ended (>45 min same app)

**Promotion criteria (Tier 1 -> Tier 2):** Not every event triggers an LLM call. An event promotes to Tier 2 when:
- A YAML rule threshold is exceeded (e.g., distraction app open > 10 min during focus block). Distraction apps are a user-configured list (e.g., `[Twitter, Reddit, YouTube]`); the evening reflection can suggest additions based on observed patterns.
- Context-switch frequency crosses a threshold (e.g., 8+ app switches in 5 min)
- Significant content shift detected in the screen text buffer (e.g., went from code to social media)
- A significant state transition occurs. V1 exhaustive list: (a) deep focus session ends (>30 min same app followed by app switch), (b) calendar event starts within 10 min, (c) return from idle >5 min during a focus block.
- **Cooldown**: minimum 10 minutes between Tier 2 calls to prevent notification fatigue. Cooldown resets if the user explicitly interacts with a nudge.

### Tier 2: LLM Judgment (expensive, rare)

On **promoted triggers**, the system packages:
- Recent screen text buffer (last 5-10 minutes of qualitative context)
- Recent ActivityWatch summary (last 10-15 minutes of quantitative app/URL data)
- Current calendar context
- Current to-do list
- SOUL.md (learned patterns, preferences, sensitivities)

...and sends it to a small/fast LLM (Haiku-class, ~200ms, ~$0.001/call). The LLM's job is **judgment, not detection**:
- **Intervene**: generate a nudge with personality
- **Stay quiet**: nothing worth saying right now
- **Update memory**: note a new pattern for SOUL.md

Result: maybe 3-5 LLM calls per day during deep focus, 10-15 during a scattered day. Not 300.

### The LLM-as-Compiler Model

The LLM doesn't just judge in real-time. It also **compiles rules** during reflection:

- **Evening reflection**: bigger LLM reviews the full day's data -- ActivityWatch's quantitative timeline + screen text buffer's qualitative annotations + current SOUL.md. Discovers patterns ("every day after your 2pm meeting you spend 20 minutes on Twitter before getting back to work"). Rewrites the trigger ruleset for tomorrow. Updates SOUL.md with new learned patterns.
- **Morning briefing** (optional): LLM looks at today's calendar + to-dos + SOUL.md, generates operating guidance for the day with specific trigger conditions.

The rules are **declarative data** (YAML), not code. The evening reflection regenerates the full ruleset fresh each night. Old rules that never fire get pruned. Rules the user dismissed get weakened. No redundancy accumulation.

```yaml
# Example compiled rules (generated by evening reflection LLM)
- trigger: distraction_app
  threshold: 10min
  context: during_deep_work
  response: gentle_redirect

- trigger: context_switch_frequency
  threshold: 8_switches_in_5min
  context: any
  response: check_in

- trigger: post_meeting_drift
  threshold: 15min
  context: after_calendar_event
  response: gentle_redirect
  note: "Alan usually needs 5-10 min decompression after meetings. Only nudge after 15."
```

Non-technical users never see this. They just talk to the companion: "Stop bugging me about Twitter, I use it for research." The evening reflection incorporates that into the next day's rules.

**Default trigger rules (ships with V1):** A starter ruleset is bundled so the system works before the first evening reflection. Covers basics: distraction app detection (configurable app list), idle-after-focus detection, calendar event reminders. The first evening reflection replaces this with a personalized set.

**Evening reflection is chained, not monolithic:** To ensure reliable structured output:
1. **Pattern analysis** -- LLM reviews activity log, identifies behavioral patterns (text output)
2. **SOUL.md update** -- LLM takes current SOUL.md + pattern analysis, outputs updated SOUL.md (YAML)
3. **Rule compilation** -- LLM takes updated SOUL.md + pattern analysis, outputs trigger rules (YAML)
4. Each YAML output is validated before writing. On parse failure, retry once with the error message.

### SOUL.md: The Memory Layer

Structured memory that enables co-evolution:
- Learned energy patterns and rhythms
- Known triggers and sensitivities
- Preferences for how/when to be nudged
- Historical patterns ("always crashes after 45 min of meetings")
- User-stated preferences ("don't interrupt me when I'm in VS Code for >20 min")

Updated incrementally by the evening reflection. SOUL.md is backed up before each write (`soul.YYYY-MM-DD.yaml`). If the updated file fails validation, the daemon loads the previous backup.

For the first 7 days, the evening reflection *appends* learned rules to the default set rather than replacing it. After 7 days, it switches to full regeneration. The default rules remain available as a fallback.

Week 1 it knows your apps. Month 2 it knows your rhythms. Month 6 it knows your triggers.

## Screen Awareness Depth

V1 uses two complementary levels simultaneously:

| Level | What it captures | How | Version |
|-------|-----------------|-----|---------|
| 0 | Window title only | ActivityWatch (free) | V1 (backbone) |
| 1 | Window title + URL + active file path | ActivityWatch + browser/editor extensions | V1 (backbone) |
| 1.5 | Visible text from active window + app name | Screen text reader via pyobjc / Accessibility APIs, every 2-5 sec | V1 (flesh layer) |
| 2 | Deep structured content (UI elements, form fields) | Full macOS Accessibility API traversal (AXUIElement tree) | V2 |
| 3 | Full text content (Littlebird-style) | Deep Accessibility APIs + OCR fallback (Apple Vision) | V2+ |

**V1 = Level 0-1 (ActivityWatch) + Level 1.5 (screen text reader).** ActivityWatch provides the quantitative backbone: every app, URL, duration, timestamped. The screen text reader adds qualitative context: what you were actually *looking at*. Together, this is enough for the LLM to make real judgments and for the evening reflection to discover behavioral patterns.

## Versioning Roadmap

### V1 -- "Focus Guardian" (weekend to 1 week)

All Python. Single user. Local only.

**Components:**
- **ActivityWatch** -- quantitative screen monitoring backbone (install, don't rebuild)
- **Screen text reader** -- reads visible text + app name from active window every 2-5 sec via pyobjc. Appends to JSONL buffer.
- **Python daemon** -- polls ActivityWatch REST API, runs screen text reader, manages event buffer, evaluates trigger rules
- **Anthropic SDK** (Haiku) -- judgment calls on interesting triggers
- **Anthropic SDK** (Sonnet/Opus) -- evening reflection, SOUL.md updates
- **osascript / terminal-notifier** -- macOS native notification delivery
- **mcp-ical server** -- calendar context
- **Things3 AppleScript bridge** -- to-do context (best-effort; cached once at morning briefing, not queried per judgment call)
- **YAML files** -- trigger rules (compiled by evening reflection)
- **YAML file** -- SOUL.md (manually seeded, then LLM-maintained). YAML chosen over JSON for human readability and consistency with trigger rules.

**Notification UX:**
- Plain macOS notifications with two actions: "Thanks" (positive signal) and "Not now" (negative signal)
- "Not now" extends the Tier 2 cooldown to 30 minutes (3x default). "Thanks" resets cooldown to default 10 minutes. Both signals logged with nudge context for evening reflection.
- No inline responses in V1 (conversational interface is V2)

**Daemon lifecycle:**
- V1: manual start via `python daemon.py`. Runs in terminal or tmux session.
- Graceful degradation: if ActivityWatch is unreachable, daemon retries every 60s silently. If Anthropic API is down, Tier 2 calls are skipped (no nudges, events still logged). If Things3/calendar unavailable, judgment calls proceed without that context.

**Features:**
- Drift detection (planned vs. actual activity mismatch)
- Nudge generation with fixed tone (warm, direct, slightly witty) -- V1 uses a prompt-level persona, not an evolving personality system
- Configurable intervention thresholds
- End-of-day summary: planned vs. actual
- Evening reflection that updates SOUL.md and recompiles trigger rules

**Explicitly not in V1:**
- No custom UI (notifications only)
- No computer use (observe and advise only)
- No deep screen reading (Level 1.5 max -- visible text, not UI tree traversal)
- No LoRA / finetuning
- No workspace-aware coding assistant

### V2 -- "Aware Companion"

**Confirmed features:**
- **Evolving personality layer** -- companion's tone, humor, and interaction style adapt over time based on interaction history. Distinct from V1's fixed prompt persona.
- **Menubar app / floating widget** (Swift UI layer)
- **Level 2 screen reading** -- macOS Accessibility APIs for richer context
- **Computer use** -- closing tabs, showing messages, taking actions. Trust earned in V1 gets exercised.
- **Swift daemon** replaces Python for battery life and polish

**Research tracks (require validation from V1 data):**
- **Cognitive energy estimation** -- screen behavior proxies (typing cadence, tab-switch frequency, idle patterns) as signals for cognitive state. Hypothesis: these correlate meaningfully. V1 collects the data; V2 acts on it if the hypothesis holds.
- **LoRA / in-context finetuning** -- deeper personalization beyond structured memory.

### V2+ -- Future Vision

Cross-context awareness: connecting dots across code, notes, conversations, and browsing via a shared context bus. Workspace-aware coding assistant as a separate component. Details to be designed when V2 is underway.

## Landscape & Prior Art

| Product | What it does | Gap |
|---------|-------------|-----|
| Littlebird.ai | Screen reading -> queryable context | Pull, not push. No personality. No intervention. |
| ActivityWatch | Open source screen time tracking | Passive observation only. REST API is useful as our data source. |
| Rewind / Recall | Screenshot-based recording | Heavy, privacy concerns, passive. No intelligence layer. |
| RescueTime / Timing.app | Activity dashboards | After-the-fact reporting. No real-time awareness. |
| Opal / Forest / Flow | Distraction blocking | Blunt force. No understanding of context. |
| Precogni | Screen-aware assistant | Closest to push, but no personality or co-evolution. |
| Desktop Companion | Gemini Flash screen analysis | Real-time but reactive (you ask), not proactive. |

The unique position: **push-based + personality + co-evolution + event-driven architecture**. Nobody is doing this combination.

## Tech Stack Summary

| Layer | Technology | Why |
|-------|-----------|-----|
| Screen monitoring (quantitative) | ActivityWatch | Battle-tested, 12k GitHub stars, REST API, privacy-first |
| Screen monitoring (qualitative) | pyobjc screen text reader | Reads visible text from active window every 2-5 sec. Lightweight, local. |
| Language (V1) | Python | Fast iteration, good LLM SDK support, pyobjc for macOS APIs |
| Language (V2 daemon) | Swift | Native macOS APIs, battery efficiency, SwiftUI for UI |
| LLM (judgment) | Claude Haiku | Fast (~200ms), cheap (~$0.001/call), good enough for nudge decisions |
| LLM (reflection) | Claude Sonnet/Opus | Deeper reasoning for pattern discovery and SOUL.md evolution |
| Notifications | macOS native (osascript) | Zero dependencies, native feel |
| Calendar | mcp-ical server | Already built, local macOS calendar access |
| To-dos | Things3 AppleScript | Already in use, native integration |
| Rules format | YAML | Human-readable, LLM-writable, no code generation needed |
| Memory format | YAML (SOUL.md) | Human-readable, consistent with rules format, LLM-writable |

## Open Questions

1. **Notification fatigue**: How do you calibrate intervention frequency so the companion is helpful without being annoying? The SOUL.md should track dismissed nudges and learn, but the cold-start period might be rough.
2. **Privacy model**: All data is local in V1. But the LLM calls send activity context to Anthropic's API. Is that acceptable? Would local models (Ollama + small LLM) be preferred even at lower quality?
3. **Cognitive energy hypothesis**: Does screen behavior actually correlate with cognitive state? Needs data collection in V1 to validate before building V2 features on this assumption.
4. **Personality design**: What does "feels human" actually mean in implementation? Tone? Humor? Unpredictability? Memory callbacks? This needs more design work before V2.
5. **Evening reflection quality**: How much activity data is needed before the evening reflection produces useful pattern insights? Probably 1-2 weeks minimum.

## Success Criteria

- **V1 ships in a weekend** and you leave it running Monday
- **You don't turn it off after day 3** -- the nudges are useful, not annoying
- **SOUL.md has real patterns after 2 weeks** -- the evening reflection is discovering things about your behavior you didn't explicitly tell it
- **The push system fires <10 times per day** -- quality over quantity
- **You catch yourself thinking "how did it know?"** at least once in the first month
