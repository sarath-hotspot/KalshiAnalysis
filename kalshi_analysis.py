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
VOLUME_SNAPSHOT_THRESHOLDS = [0, 1_000, 10_000, 100_000, 500_000, 1_000_000]
OPEN_INTEREST_THRESHOLDS = [0, 100, 500, 1_000, 5_000, 10_000, 50_000]


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
        "lower": lower,
        "upper": upper,
    }


def _strategy_pnl(stats: dict) -> tuple[float, float, float, float]:
    """Return (yes_cost, yes_pnl, no_cost, no_pnl) in dollars for a given stats row.

    Yes strategy: buy YES at `upper` cents (worst-case price in the band).
      cost = total * upper cents
      win  = correct * 100 cents
      pnl  = win - cost

    No strategy: buy NO at (100 - lower) cents (worst-case price in the band).
      cost = total * (100 - lower) cents
      win  = wrong * 100 cents   (favourite lost → NO wins, paid $1)
      pnl  = win - cost
    """
    upper = stats["upper"]
    lower = stats["lower"]
    correct = stats["correct"]
    wrong = stats["incorrect"]
    total = correct + wrong

    yes_cost = (total * upper) / 100.0
    yes_pnl = (correct * 100 - total * upper) / 100.0

    no_cost = (total * (100 - lower)) / 100.0
    no_pnl = (wrong * 100 - total * (100 - lower)) / 100.0
    return yes_cost, yes_pnl, no_cost, no_pnl


def _fmt_pnl(v: float) -> str:
    """Format a dollar P&L value with sign and colour-neutral alignment."""
    return f"${v:>+.2f}"


def _fmt_cost(v: float) -> str:
    """Format a dollar cost value (always positive, no sign)."""
    return f"${v:>.2f}"


def _print_stats_table(table: list[dict], cumulative: dict, cumulative_all: dict) -> None:
    """Shared helper to print a confidence-band table with strategy P&L columns."""
    hdr = (f"{'Confidence':<14} {'Total':>6} {'Correct':>8} {'Wrong':>6} "
           f"{'Accuracy':>9} {'Yes_Cost':>10} {'Yes_PnL':>11} {'No_Cost':>10} {'No_PnL':>11}")
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)
    for s in table:
        yc, yp, nc, np_ = _strategy_pnl(s)
        print(f"{s['range']:<14} {s['total']:>6} {s['correct']:>8} {s['incorrect']:>6} "
              f"{s['accuracy']:>8.2f}% {_fmt_cost(yc):>10} {_fmt_pnl(yp):>11} {_fmt_cost(nc):>10} {_fmt_pnl(np_):>11}")
    print(sep)
    # Sum across all bands for cumulative rows
    cum_yc = sum(_strategy_pnl(s)[0] for s in table if s["upper"] <= 90)
    cum_yp = sum(_strategy_pnl(s)[1] for s in table if s["upper"] <= 90)
    cum_nc = sum(_strategy_pnl(s)[2] for s in table if s["upper"] <= 90)
    cum_np = sum(_strategy_pnl(s)[3] for s in table if s["upper"] <= 90)
    print(f"{cumulative['range']:<14} {cumulative['total']:>6} {cumulative['correct']:>8} "
          f"{cumulative['incorrect']:>6} {cumulative['accuracy']:>8.2f}% "
          f"{_fmt_cost(cum_yc):>10} {_fmt_pnl(cum_yp):>11} {_fmt_cost(cum_nc):>10} {_fmt_pnl(cum_np):>11}")
    total_yc = sum(_strategy_pnl(s)[0] for s in table)
    total_yp = sum(_strategy_pnl(s)[1] for s in table)
    total_nc = sum(_strategy_pnl(s)[2] for s in table)
    total_np = sum(_strategy_pnl(s)[3] for s in table)
    print(f"{cumulative_all['range']:<16} {cumulative_all['total']:>4} {cumulative_all['correct']:>8} "
          f"{cumulative_all['incorrect']:>6} {cumulative_all['accuracy']:>8.2f}% "
          f"{_fmt_cost(total_yc):>10} {_fmt_pnl(total_yp):>11} {_fmt_cost(total_nc):>10} {_fmt_pnl(total_np):>11}")


def _volume(row: dict) -> float:
    """Return volume in dollars."""
    return float(row.get("VolumeInDollar", 0) or 0)


def _fmt_vol(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.0f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def _open_interest(row: dict) -> int:
    """Return open interest (contracts outstanding at snapshot time)."""
    return int(row.get("OpenInterest", 0) or 0)


def _volume_at_snapshot(row: dict) -> float:
    """Return dollar volume up to the snapshot time."""
    return float(row.get("VolumeAtSnapshot", 0) or 0)


def _fmt_contracts(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.0f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def _fmt_oi(v: int) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.0f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    return f"{v}"


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
    _print_stats_table(table, cumulative, cumulative_all)


def print_confidence_for_open_interest_ranges(rows: list[dict]) -> None:
    """Print a confidence-band accuracy table for each open interest range."""
    for i, oi_lo in enumerate(OPEN_INTEREST_THRESHOLDS):
        oi_hi = OPEN_INTEREST_THRESHOLDS[i + 1] if i + 1 < len(OPEN_INTEREST_THRESHOLDS) else float("inf")
        label = f"{_fmt_oi(oi_lo)}-{_fmt_oi(int(oi_hi))}" if oi_hi != float("inf") else f">{_fmt_oi(oi_lo)}"

        subset = [r for r in rows if oi_lo <= _open_interest(r) < oi_hi]
        if not subset:
            print(f"\n=== Open Interest {label}: 0 markets — skipping ===")
            continue

        competitive = [r for r in subset if _favourite_prob(r) <= 0.90]
        done_games = len(subset) - len(competitive)

        print(f"\n=== Open Interest {label} ===")
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

        _print_stats_table(table, cumulative, cumulative_all)


def print_confidence_for_volume_snapshot_ranges(rows: list[dict]) -> None:
    """Print a confidence-band accuracy table for each volume-at-snapshot range."""
    for i, vs_lo in enumerate(VOLUME_SNAPSHOT_THRESHOLDS):
        vs_hi = VOLUME_SNAPSHOT_THRESHOLDS[i + 1] if i + 1 < len(VOLUME_SNAPSHOT_THRESHOLDS) else float("inf")
        label = f"{_fmt_contracts(vs_lo)}-{_fmt_contracts(int(vs_hi))}" if vs_hi != float("inf") else f">{_fmt_contracts(vs_lo)}"

        subset = [r for r in rows if vs_lo <= _volume_at_snapshot(r) < vs_hi]
        if not subset:
            print(f"\n=== Volume@Snapshot {label}: 0 markets — skipping ===")
            continue

        competitive = [r for r in subset if _favourite_prob(r) <= 0.90]
        done_games = len(subset) - len(competitive)

        print(f"\n=== Volume@Snapshot {label} contracts ===")
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

        _print_stats_table(table, cumulative, cumulative_all)


def main():
    rows = load_data(DATA_FILE)
    print(f"Loaded {len(rows)} rows from {DATA_FILE.name}\n")
    print_accuracy_table(rows)
    print("\n" + "=" * 60)
    print("  ANALYSIS BY OPEN INTEREST")
    print("=" * 60)
    print_confidence_for_open_interest_ranges(rows)
    print("\n" + "=" * 60)
    print("  ANALYSIS BY VOLUME AT SNAPSHOT")
    print("=" * 60)
    print_confidence_for_volume_snapshot_ranges(rows)


if __name__ == "__main__":
    main()
