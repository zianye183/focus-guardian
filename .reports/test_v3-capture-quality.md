# test_v3 Capture Quality Report

**Date:** 2026-04-08
**Dataset:** `data/test_v3.db` — 1,325 captures across ~5 hours (23:39 Apr 7 – 04:27 Apr 8)
**Apps observed:** 24 distinct apps

---

## Summary

| Rating | Apps |
|--------|------|
| Excellent | Arc (websites), Ghostty, DB Browser, Obsidian |
| Good | Mail, Finder, Music, Fantastical, Things |
| Partial | WeChat, Dictionary, Google Chrome |
| Failed | Claude, Preview.app (PDFs), Slay the Spire 2, The Bazaar, Littlebird, Steam/CrossOver, IDLE |

---

## Excellent — Rich, Meaningful Text

### Arc (browser) — 375 captures, avg 4,625 chars
- Best performer. Full page text from most websites.
- See website breakdown below for per-site details.

### Ghostty (terminal) — 63 captures, avg 13,210 chars
- Captures full terminal buffer including Claude Code TUI.
- Box-drawing characters and layout preserved.

### DB Browser for SQLite — 30 captures, avg 16,583 chars
- Full table data, SQL queries, UI state.

### Obsidian — 160 captures, avg 3,708 chars
- Note content, sidebar file lists, vault structure.
- One capture at 20,000 chars (max).

---

## Good — Usable With Caveats

### Mail — 22 captures, avg 794 chars
- Email previews, mailbox structure, sender/subject lines.
- 4/22 empty (no focused text element).

### Finder — 109 captures, avg 387 chars
- File/folder names, path info. Shallow but contextually useful.

### Music — 12 captures, avg 813 chars
- Track names, player controls, album info.

### Fantastical — 2 captures, avg 2,007 chars
- Calendar event details captured.

### Things — 2 captures, avg 758 chars
- Task list content captured.

---

## Partial — Text Captured But Missing Key Content

### WeChat — 16 captures, avg 107 chars
- Gets UI chrome: contact names, button labels, navigation.
- **No message content** — likely macOS AX privacy restriction on chat text.
- 5/16 completely empty.

### Dictionary — 16 captures, avg 180 chars
- Only captures "Type a word to look up in… | New Oxford American Dictionary".
- **Actual definition text not extracted** despite being on screen.

### Google Chrome — 4 captures, avg 561 chars
- 1/4 empty. Limited sample but text extraction works when present.

---

## Failed — Zero or Useless Text

### Claude (desktop app) — 106 captures, 95% empty
- **The biggest gap.** 101/106 captures have null/empty text.
- The 4 non-empty captures (825 chars each) contain only the macOS menu bar:
  `"Apple | File | Edit | View | Prototypes | Debug | Window | Help | About This Mac..."`
- **Conversation content is invisible to AX.** Electron app does not expose it.
- Example: `id=466` — title "Claude", text is just menu bar.

### Preview.app (PDFs) — 269 captures, avg 461 chars
- **All captures return only toolbar chrome** (194 chars):
  `"document | View | 1 | Page | Inspector | 100% | Scale | Zoom..."`
- **Zero PDF document text extracted.**
- Contrast with browser: Arxiv PDF in Arc (`id=318`) got 20,000 chars of actual paper text.
- Example IDs: `9, 11, 12, 13, 14` — all `cost_distribution_T200.pdf`, all 194 chars of toolbar only.

### Games — 100% empty
| App | Captures | Text |
|-----|----------|------|
| Slay the Spire 2 | 52 | 0 chars every capture |
| The Bazaar | 26 | 0 chars every capture |
| BackpackBattles.exe | 1 | 0 chars |
- GPU-rendered, no AX tree. Expected behavior.

### Littlebird — 44 captures, 100% empty
- Electron app. AX tree not exposed.
- Same root cause as Claude desktop.

### Steam Helper — 9 captures, 100% empty
### IDLE (Python) — 3 captures, 100% empty
### Steam/steam.exe — 2 captures, 100% empty

---

## Website Breakdown (Arc browser)

### Excellent text extraction
| Site | Captures | Avg Chars | Notes |
|------|----------|-----------|-------|
| Google Blog | 2 | 10,642 | Full article body |
| npm docs | 5 | 20,000 | Full package documentation |
| ProductHunt | 6 | 17,190 | Product descriptions, comments |
| Wikipedia | ~9 | 5,900–20,000 | Full article text |
| Scholarpedia | 1 | 10,736 | Full article |
| Bilibili (video pages) | ~85 | 5,600–18,800 | Title, description, danmaku comments, recommendations |
| Google Search | 40 | 3,969 | Full results with snippets |
| **Arxiv PDF render** | **1** | **20,000** | **Full paper text via browser PDF viewer** |
| Arxiv abstract page | 1 | 4,617 | Metadata, abstract, links |
| SIAM/math journals | 2 | 4,457 | Article content |
| Delta.com | ~11 | 1,000–8,500 | Flight search, booking details |
| HuggingFace | 2 | 2,235 | Model/collection cards |

### Moderate text extraction
| Site | Captures | Avg Chars | Notes |
|------|----------|-----------|-------|
| YouTube | ~50 | 1,400–3,000 | Title, controls, some description. No transcript. |
| Marp.app | 3 | 4,196 | Landing page content |
| StackExchange | 1 | 5,450 | Q&A content |
| ScienceDirect | 3 | 1,187 | Paywall limits visible content |

### Poor text extraction
| Site | Captures | Avg Chars | Notes |
|------|----------|-----------|-------|
| X/Twitter | 2 | 234 | Login gate blocked content |
| Overleaf | 6 | 1,331 | Mostly login/error pages, not editor |
| AMS.org | 1 | 46 | Almost nothing extracted |

### Other observations
- **Chinese content works fine** — Bilibili, Google in Chinese, WeChat labels all captured correctly.
- **Privacy filter working** — 4 captures correctly marked `filtered=sensitive_page`.

---

## Priority Fix List

Ranked by impact (frequency x information value):

| Priority | App | Captures | Issue | Potential Fix |
|----------|-----|----------|-------|---------------|
| **P0** | Preview.app (PDFs) | 269 | Only toolbar chrome, no doc text | Investigate AX tree for PDF content pane; may need `AXDocument` role traversal or fallback to PDFKit text extraction |
| **P0** | Claude (desktop) | 106 | Electron app — AX tree unexposed | Investigate `AXWebArea` role in Electron; may need chromium accessibility flags or fallback strategy |
| **P1** | Littlebird | 44 | Same Electron issue as Claude | Same fix as Claude likely applies |
| **P1** | Dictionary | 16 | Definition text not extracted | Investigate AX tree — definition pane may use a different role |
| **P2** | WeChat | 16 | Message content blocked by AX privacy | May be unfixable (OS-level restriction); document as known limitation |
| **P3** | Games | 78 | GPU-rendered, no AX | Unfixable via AX; could detect and skip to save resources |
