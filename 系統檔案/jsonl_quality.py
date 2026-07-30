"""Shared, secret-safe JSONL bad-line accounting."""

from __future__ import annotations

import sys
from pathlib import Path


MAX_BAD_LINE_RATIO = 0.05
MAX_BAD_LINES = 100
SINGLE_BAD_LINE_TOLERANCE = 1


def warn_bad_line(path: Path, line_number: int, reason: str) -> None:
    """Log only location/reason; never echo the rejected line itself."""
    print(
        f"JSONL WARNING：{path.name}:{line_number} 已略過（{reason}）",
        file=sys.stderr,
        flush=True,
    )


def enforce_quality(
    path: Path,
    *,
    nonempty_lines: int,
    bad_lines: int,
) -> None:
    """Reject materially corrupted files while always tolerating one bad line."""
    if bad_lines <= 0:
        return
    denominator = max(1, nonempty_lines)
    bad_ratio = bad_lines / denominator
    ratio_exceeded = (
        bad_lines > SINGLE_BAD_LINE_TOLERANCE
        and bad_ratio > MAX_BAD_LINE_RATIO
    )
    count_exceeded = bad_lines > MAX_BAD_LINES
    summary = (
        f"{path.name} 壞行={bad_lines}/{nonempty_lines}"
        f"（{bad_ratio:.2%}）"
    )
    if ratio_exceeded or count_exceeded:
        raise ValueError(f"{summary}，超過容錯門檻")
    print(
        f"JSONL WARNING：{summary}，其餘資料繼續處理",
        file=sys.stderr,
        flush=True,
    )
