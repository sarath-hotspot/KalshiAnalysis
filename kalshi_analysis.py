"""
Kalshi Sports Market Analysis
Analyses how well Kalshi users predict game outcomes.

Reads kalshi_sports_data.csv and computes prediction accuracy:
  - "Correct" = (YesProb > 0.50 and result is yes) OR (NoProb > 0.50 and result is no)
  - i.e. the market favourite won
  - Markets with favourite prob > 90% are excluded as "done games"
"""

import csv
from pathlib import Path

DATA_FILE = Path(__file__).with_name("kalshi_sports_data.csv")

CONFIDENCE_THRESHOLDS = [50, 55, 60, 65, 70, 75, 80, 85, 90]
VOLUME_THRESHOLDS = [0, 1_000, 10_000, 100_000, 500_000, 1_000_000]


def load_data(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Deduplicate by (SeriesTicker, EventTicker, MarketTicker)
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    dupes = 0
    for row in rows:
        key = (row["SeriesTicker"], row["EventTicker"], row["MarketTicker"])
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        unique.append(row)

    if dupes:
        print(f"Removed {dupes} duplicate rows.\n")

    return unique


def _favourite_prob(row: dict) -> float:
    """Return the probability of the market favourite (always >= 0.50)."""
    yes_prob = float(row["YesProb"])
    return max(yes_prob, 1.0 - yes_prob)


def _is_correct(row: dict) -> bool:
    """True if the market favourite matches the actual result."""
    yes_prob = float(row["YesProb"])
    result = row["EventResult"].strip().lower()
    if yes_prob > 0.50:
        return result == "yes"
    elif yes_prob < 0.50:
        return result == "no"
    return False  # 50/50 — not a clear prediction


def accuracy_at_threshold(rows: list[dict], lower: int, upper: int) -> dict:
    """
    Compute accuracy for markets where the favourite's probability
    is in (lower/100, upper/100].  upper=100 means no upper cap.
    """
    lo = lower / 100.0
    hi = upper / 100.0

    total = correct = incorrect = 0
    for row in rows:
        fav = _favourite_prob(row)
        if fav <= lo:
            continue
        if hi < 1.0 and fav > hi:
            continue
        total += 1
        if _is_correct(row):
            correct += 1
        else:
            incorrect += 1

    return {
        "range": f">{lower}%" if upper == 100 else f"{lower}-{upper}%",
        "total": total,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy": (correct / total * 100) if total else 0,
    }


def _volume(row: dict) -> float:
    """Return volume in dollars."""
    return float(row.get("VolumeInDollar", 0) or 0)


def _fmt_vol(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.0f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def print_accuracy_table(rows: list[dict]) -> None:
    # Filter out "done games" (favourite prob > 90%)
    competitive = [r for r in rows if _favourite_prob(r) <= 0.90]
    done_games = len(rows) - len(competitive)

    print(f"Total markets: {len(rows)}")
    print(f"Done games (favourite >90%): {done_games} — excluded from analysis")
    print(f"Competitive markets: {len(competitive)}\n")

    # Build table rows for each threshold band
    table = []
    for i, lo in enumerate(CONFIDENCE_THRESHOLDS):
        hi = CONFIDENCE_THRESHOLDS[i + 1] if i + 1 < len(CONFIDENCE_THRESHOLDS) else 100
        # Use all rows for the >90% band, competitive for the rest
        source = rows if lo >= 90 else competitive
        stats = accuracy_at_threshold(source, lo, hi)
        table.append(stats)

    # Also add a cumulative ">50%" row (all competitive markets)
    cumulative = accuracy_at_threshold(competitive, 50, 100)
    cumulative["range"] = "ALL >50%"

    # Cumulative including done games
    cumulative_all = accuracy_at_threshold(rows, 50, 100)
    cumulative_all["range"] = "ALL (incl >90%)"

    # Print table
    hdr = f"{'Confidence':<14} {'Total':>6} {'Correct':>8} {'Wrong':>6} {'Accuracy':>9}"
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)
    for s in table:
        print(f"{s['range']:<14} {s['total']:>6} {s['correct']:>8} {s['incorrect']:>6} {s['accuracy']:>8.2f}%")
    print(sep)
    print(f"{cumulative['range']:<14} {cumulative['total']:>6} {cumulative['correct']:>8} {cumulative['incorrect']:>6} {cumulative['accuracy']:>8.2f}%")
    print(f"{cumulative_all['range']:<16} {cumulative_all['total']:>4} {cumulative_all['correct']:>8} {cumulative_all['incorrect']:>6} {cumulative_all['accuracy']:>8.2f}%")


def print_confidence_for_volume_ranges(rows: list[dict]) -> None:
    """Print a confidence-band accuracy table for each volume range."""
    for i, vol_lo in enumerate(VOLUME_THRESHOLDS):
        vol_hi = VOLUME_THRESHOLDS[i + 1] if i + 1 < len(VOLUME_THRESHOLDS) else float("inf")
        label = f"{_fmt_vol(vol_lo)}-{_fmt_vol(vol_hi)}" if vol_hi != float("inf") else f">{_fmt_vol(vol_lo)}"

        subset = [r for r in rows if vol_lo <= _volume(r) < vol_hi]
        if not subset:
            print(f"\n=== Volume {label}: 0 markets — skipping ===")
            continue

        competitive = [r for r in subset if _favourite_prob(r) <= 0.90]
        done_games = len(subset) - len(competitive)

        print(f"\n=== Volume {label} ===")
        print(f"Markets: {len(subset)} | Competitive: {len(competitive)} | Done games (>90%): {done_games}\n")

        table = []
        for j, lo in enumerate(CONFIDENCE_THRESHOLDS):
            hi = CONFIDENCE_THRESHOLDS[j + 1] if j + 1 < len(CONFIDENCE_THRESHOLDS) else 100
            source = subset if lo >= 90 else competitive
            stats = accuracy_at_threshold(source, lo, hi)
            table.append(stats)

        cumulative = accuracy_at_threshold(competitive, 50, 100)
        cumulative["range"] = "ALL >50%"
        cumulative_all = accuracy_at_threshold(subset, 50, 100)
        cumulative_all["range"] = "ALL (incl >90%)"

        hdr = f"{'Confidence':<14} {'Total':>6} {'Correct':>8} {'Wrong':>6} {'Accuracy':>9}"
        sep = "-" * len(hdr)
        print(hdr)
        print(sep)
        for s in table:
            print(f"{s['range']:<14} {s['total']:>6} {s['correct']:>8} {s['incorrect']:>6} {s['accuracy']:>8.2f}%")
        print(sep)
        print(f"{cumulative['range']:<14} {cumulative['total']:>6} {cumulative['correct']:>8} {cumulative['incorrect']:>6} {cumulative['accuracy']:>8.2f}%")
        print(f"{cumulative_all['range']:<16} {cumulative_all['total']:>4} {cumulative_all['correct']:>8} {cumulative_all['incorrect']:>6} {cumulative_all['accuracy']:>8.2f}%")


def main():
    rows = load_data(DATA_FILE)
    print(f"Loaded {len(rows)} rows from {DATA_FILE.name}\n")
    print_accuracy_table(rows)
    print_confidence_for_volume_ranges(rows)


if __name__ == "__main__":
    main()
