"""
Tests for the dedup module.

Tests use both synthetic records and real captured JSONL data
from data/buffer/ to validate against the three known dedup-defeating
patterns: WeChat video calls, Obsidian editing, Bilibili video watching.
"""

import json
from pathlib import Path

import pytest

# Ensure src/ is importable
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dedup import text_similarity, should_keep, dedup_records


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent.parent / "data" / "buffer"


def _load_jsonl(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


@pytest.fixture
def day1_records():
    path = DATA_DIR / "2026-04-04.jsonl"
    if not path.exists():
        pytest.skip("Real data not available")
    return _load_jsonl(path)


@pytest.fixture
def day2_records():
    path = DATA_DIR / "2026-04-05.jsonl"
    if not path.exists():
        pytest.skip("Real data not available")
    return _load_jsonl(path)


# ---------------------------------------------------------------------------
# Unit tests: text_similarity
# ---------------------------------------------------------------------------

class TestTextSimilarity:
    def test_identical(self):
        assert text_similarity("hello world", "hello world") == 1.0

    def test_empty_strings(self):
        assert text_similarity("", "") == 1.0

    def test_one_empty(self):
        assert text_similarity("hello", "") == 0.0
        assert text_similarity("", "hello") == 0.0

    def test_completely_different(self):
        assert text_similarity("aaaa", "zzzz") < 0.1

    def test_wechat_timer_high_similarity(self):
        """WeChat call timer changing should produce high similarity."""
        a = "00:03 | Mic is on | Hang Up | Speaker is on | Close Camera"
        b = "00:06 | Mic is on | Hang Up | Speaker is on | Close Camera"
        sim = text_similarity(a, b)
        assert sim > 0.85, f"WeChat timer similarity {sim:.3f} should be > 0.85"

    def test_wechat_timer_long_call(self):
        """Timer going from minutes to different minutes."""
        a = "01:23:45 | Mic is on | Hang Up | Speaker is on | Close Camera"
        b = "01:24:02 | Mic is on | Hang Up | Speaker is on | Close Camera"
        sim = text_similarity(a, b)
        assert sim > 0.85

    def test_obsidian_word_count_change(self):
        """Obsidian status bar word count changing."""
        base = "Some note content here | Go to file (⌘ Y) | 0 backlinks | 6 properties | Live Preview"
        a = f"{base} | 820 words | 4231 characters"
        b = f"{base} | 837 words | 4312 characters"
        sim = text_similarity(a, b)
        assert sim > 0.85

    def test_meaningful_content_change(self):
        """Completely different page content should be low similarity."""
        a = "Google Search | Search results for machine learning | Result 1 | Result 2"
        b = "YouTube | How to cook pasta | Subscribe | 1.2M views | Comments"
        sim = text_similarity(a, b)
        assert sim < 0.5


# ---------------------------------------------------------------------------
# Unit tests: should_keep
# ---------------------------------------------------------------------------

class TestShouldKeep:
    def test_first_record_always_kept(self):
        record = {"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": "Google", "text": "hello"}
        assert should_keep(record, None) is True

    def test_app_change_kept(self):
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": "Google", "text": "hello"}
        record = {"ts": "2026-04-05T10:00:03+00:00", "app": "Obsidian", "title": "Note", "text": "hello"}
        assert should_keep(record, last) is True

    def test_title_change_kept(self):
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": "Tab 1", "text": "hello"}
        record = {"ts": "2026-04-05T10:00:03+00:00", "app": "Arc", "title": "Tab 2", "text": "hello"}
        assert should_keep(record, last) is True

    def test_url_change_kept(self):
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": "Page", "text": "hello", "url": "https://a.com"}
        record = {"ts": "2026-04-05T10:00:03+00:00", "app": "Arc", "title": "Page", "text": "hello", "url": "https://b.com"}
        assert should_keep(record, last) is True

    def test_url_appears_kept(self):
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": "Page", "text": "hello"}
        record = {"ts": "2026-04-05T10:00:03+00:00", "app": "Arc", "title": "Page", "text": "hello", "url": "https://a.com"}
        assert should_keep(record, last) is True

    def test_exact_duplicate_discarded(self):
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": "Page", "text": "hello"}
        record = {"ts": "2026-04-05T10:00:03+00:00", "app": "Arc", "title": "Page", "text": "hello"}
        assert should_keep(record, last) is False

    def test_near_duplicate_discarded(self):
        """Timer-like change should be discarded."""
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "WeChat", "title": "Video Call", "text": "00:03 | Mic is on | Hang Up"}
        record = {"ts": "2026-04-05T10:00:03+00:00", "app": "WeChat", "title": "Video Call", "text": "00:06 | Mic is on | Hang Up"}
        assert should_keep(record, last) is False

    def test_forced_snapshot_after_timeout(self):
        """Even duplicates should be kept after forced_snapshot_s."""
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "WeChat", "title": "Video Call", "text": "00:03 | Mic is on"}
        record = {"ts": "2026-04-05T10:01:00+00:00", "app": "WeChat", "title": "Video Call", "text": "01:03 | Mic is on"}
        assert should_keep(record, last, forced_snapshot_s=45) is True

    def test_forced_snapshot_not_premature(self):
        """Should not force snapshot before the timeout."""
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "WeChat", "title": "Video Call", "text": "00:03 | Mic is on"}
        record = {"ts": "2026-04-05T10:00:30+00:00", "app": "WeChat", "title": "Video Call", "text": "00:33 | Mic is on"}
        assert should_keep(record, last, forced_snapshot_s=45) is False

    def test_meaningful_text_change_kept(self):
        """Significant text change (below threshold) should be kept."""
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": "Page", "text": "Article about machine learning and neural networks"}
        record = {"ts": "2026-04-05T10:00:03+00:00", "app": "Arc", "title": "Page", "text": "Shopping cart: 3 items | Checkout | Free shipping available"}
        assert should_keep(record, last) is True

    def test_custom_threshold(self):
        """Should respect custom similarity threshold."""
        last = {"ts": "2026-04-05T10:00:00+00:00", "app": "App", "title": "T", "text": "hello world foo"}
        record = {"ts": "2026-04-05T10:00:03+00:00", "app": "App", "title": "T", "text": "hello world bar"}
        # With very strict threshold (0.99), small change is kept
        assert should_keep(record, last, similarity_threshold=0.99) is True
        # With loose threshold (0.5), same change is discarded
        assert should_keep(record, last, similarity_threshold=0.5) is False


# ---------------------------------------------------------------------------
# Unit tests: dedup_records (batch)
# ---------------------------------------------------------------------------

class TestDedupRecords:
    def test_empty_input(self):
        assert dedup_records([]) == []

    def test_single_record(self):
        records = [{"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": "P", "text": "hello"}]
        assert dedup_records(records) == records

    def test_does_not_mutate_input(self):
        records = [
            {"ts": "2026-04-05T10:00:00+00:00", "app": "Arc", "title": "P", "text": "hello"},
            {"ts": "2026-04-05T10:00:03+00:00", "app": "Arc", "title": "P", "text": "hello"},
        ]
        original = [r.copy() for r in records]
        dedup_records(records)
        assert records == original

    def test_synthetic_wechat_call(self):
        """Simulate a 5-minute WeChat call with timer changing every 3s."""
        records = []
        for i in range(100):
            secs = i * 3
            mm, ss = divmod(secs, 60)
            records.append({
                "ts": f"2026-04-05T10:{mm:02d}:{ss:02d}+00:00",
                "app": "WeChat",
                "title": "Video Call - Alice",
                "text": f"{mm:02d}:{ss:02d} | Mic is on | Hang Up | Speaker is on | Close Camera",
            })

        kept = dedup_records(records, forced_snapshot_s=45)

        # Should be massively reduced: ~1 per 45s = ~7 records for 5 min
        assert len(kept) < 15, f"Expected <15, got {len(kept)} (from {len(records)})"
        # First record always kept
        assert kept[0] == records[0]
        # Should have periodic snapshots
        assert len(kept) >= 5


# ---------------------------------------------------------------------------
# Integration tests: real JSONL data
# ---------------------------------------------------------------------------

class TestRealDataDay2:
    """Tests against April 5 data (3,088 records, 84% duplicates)."""

    def test_overall_reduction(self, day2_records):
        """Should reduce 3000+ records to well under 1000."""
        kept = dedup_records(day2_records)
        reduction = 1 - len(kept) / len(day2_records)
        assert reduction > 0.5, (
            f"Only {reduction:.0%} reduction ({len(day2_records)} → {len(kept)}). "
            f"Expected >50%."
        )

    def test_wechat_video_call_collapsed(self, day2_records):
        """1,106 WeChat video call records should collapse dramatically."""
        video_call = [r for r in day2_records if "Video Call" in (r.get("title") or "")]
        kept_video = dedup_records(video_call)

        assert len(video_call) > 100, "Expected many video call records in test data"
        reduction = 1 - len(kept_video) / len(video_call)
        assert reduction > 0.8, (
            f"Video call: {len(video_call)} → {len(kept_video)} "
            f"({reduction:.0%} reduction, expected >80%)"
        )

    def test_obsidian_editing_collapsed(self, day2_records):
        """Obsidian editing with minor text changes should be reduced."""
        obsidian = [r for r in day2_records if r.get("app") == "Obsidian"]
        kept_obs = dedup_records(obsidian)

        assert len(obsidian) > 50, "Expected many Obsidian records in test data"
        reduction = 1 - len(kept_obs) / len(obsidian)
        assert reduction > 0.3, (
            f"Obsidian: {len(obsidian)} → {len(kept_obs)} "
            f"({reduction:.0%} reduction, expected >30%)"
        )

    def test_transitions_preserved(self, day2_records):
        """App transitions should never be lost."""
        kept = dedup_records(day2_records)

        # Count unique app sequences in original
        original_apps = [r["app"] for r in day2_records]
        original_transitions = sum(
            1 for i in range(1, len(original_apps))
            if original_apps[i] != original_apps[i - 1]
        )

        # Count in deduped
        kept_apps = [r["app"] for r in kept]
        kept_transitions = sum(
            1 for i in range(1, len(kept_apps))
            if kept_apps[i] != kept_apps[i - 1]
        )

        assert kept_transitions == original_transitions, (
            f"Lost transitions: original={original_transitions}, kept={kept_transitions}"
        )

    def test_target_under_1000_records(self, day2_records):
        """Should reduce 3000+ records to under 1000 with dedup alone."""
        kept = dedup_records(day2_records)
        assert len(kept) < 1000, (
            f"Kept {len(kept)} records, target is <1000"
        )


class TestRealDataDay1:
    """Tests against April 4 data (1,326 records, 65% duplicates)."""

    def test_overall_reduction(self, day1_records):
        kept = dedup_records(day1_records)
        reduction = 1 - len(kept) / len(day1_records)
        assert reduction > 0.3, (
            f"Only {reduction:.0%} reduction ({len(day1_records)} → {len(kept)}). "
            f"Expected >30%."
        )

    def test_no_data_loss_on_app_switches(self, day1_records):
        """Every app switch in the original should appear in deduped output."""
        kept = dedup_records(day1_records)

        original_transitions = []
        for i in range(1, len(day1_records)):
            if day1_records[i]["app"] != day1_records[i - 1]["app"]:
                original_transitions.append(
                    (day1_records[i - 1]["app"], day1_records[i]["app"])
                )

        kept_transitions = []
        for i in range(1, len(kept)):
            if kept[i]["app"] != kept[i - 1]["app"]:
                kept_transitions.append((kept[i - 1]["app"], kept[i]["app"]))

        assert len(kept_transitions) == len(original_transitions)
