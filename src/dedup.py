"""
Deduplication engine for Focus Guardian screen captures.

Compares incoming capture records against the last kept record using
text similarity (SequenceMatcher). Designed to collapse near-duplicate
records caused by dynamic UI elements (call timers, word counts,
video timestamps) while preserving meaningful transitions.

All tunable parameters are in config.yaml under the `dedup` key.
"""

from difflib import SequenceMatcher

from config import CONFIG

_DEDUP_CFG = CONFIG.get("dedup", {})
_SIMILARITY_THRESHOLD = _DEDUP_CFG.get("similarity_threshold", 0.85)
_FORCED_SNAPSHOT_S = _DEDUP_CFG.get("forced_snapshot_seconds", 45)
_MAX_TEXT_COMPARE = _DEDUP_CFG.get("max_text_compare_length", 2000)


def text_similarity(a: str, b: str) -> float:
    """
    Compute similarity ratio between two strings.

    Truncates both strings to max_text_compare_length before comparing
    to bound CPU cost on large captures.

    Returns a float in [0.0, 1.0].
    """
    a = a[:_MAX_TEXT_COMPARE]
    b = b[:_MAX_TEXT_COMPARE]

    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    return SequenceMatcher(None, a, b).ratio()


def _parse_ts(ts_str: str) -> float:
    """Parse ISO8601 timestamp string to Unix epoch seconds."""
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(ts_str)
    return dt.timestamp()


def _seconds_between(ts_a: str, ts_b: str) -> float:
    """Seconds elapsed between two ISO8601 timestamps."""
    return abs(_parse_ts(ts_b) - _parse_ts(ts_a))


def should_keep(
    record: dict,
    last_kept: dict | None,
    similarity_threshold: float = _SIMILARITY_THRESHOLD,
    forced_snapshot_s: float = _FORCED_SNAPSHOT_S,
) -> bool:
    """
    Decide whether to keep a capture record or discard as duplicate.

    Rules (evaluated in order):
      1. No previous record → keep (first record).
      2. App changed → keep (transition).
      3. URL changed → keep (navigation).
      4. Title changed → keep (window/tab switch).
      5. Forced snapshot: if enough time has elapsed since last kept
         record, keep regardless of similarity.
      6. Text similarity below threshold → keep (meaningful change).
      7. Otherwise → discard (near-duplicate).
    """
    if last_kept is None:
        return True

    # Rule 2: app transition
    if record.get("app") != last_kept.get("app"):
        return True

    # Rule 3: URL navigation
    new_url = record.get("url")
    old_url = last_kept.get("url")
    if new_url and old_url and new_url != old_url:
        return True
    if new_url and not old_url:
        return True

    # Rule 4: title change
    if record.get("title") != last_kept.get("title"):
        return True

    # Rule 5: forced snapshot
    elapsed = _seconds_between(last_kept["ts"], record["ts"])
    if elapsed >= forced_snapshot_s:
        return True

    # Rule 6: text similarity check
    sim = text_similarity(
        record.get("text", ""),
        last_kept.get("text", ""),
    )
    if sim < similarity_threshold:
        return True

    # Rule 7: near-duplicate, discard
    return False


def dedup_records(
    records: list[dict],
    similarity_threshold: float = _SIMILARITY_THRESHOLD,
    forced_snapshot_s: float = _FORCED_SNAPSHOT_S,
) -> list[dict]:
    """
    Filter a list of capture records, keeping only non-duplicate entries.

    Useful for batch processing existing JSONL data.
    Returns a new list (no mutation of input).
    """
    kept = []
    last_kept = None

    for record in records:
        if should_keep(record, last_kept, similarity_threshold, forced_snapshot_s):
            kept.append(record)
            last_kept = record

    return kept
