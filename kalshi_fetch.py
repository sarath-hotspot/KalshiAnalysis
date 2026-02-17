"""
Kalshi Sports Market Data Fetcher
Fetches settled sports markets from Kalshi, retrieves the YES/NO probability
~24 hours before market close via candlestick data, and outputs a CSV.

Output columns:
  Time, EventEndTime, VolumeInDollar, YesProb, NoProb, EventResult

Public endpoints - no API key required.
"""

import csv
import json
import re
import sys
import time
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SPORTS_CATEGORY = "Sports"
BASKETBALL_TAG = "Basketball"  # tag used by both pro (NBA) and college (NCAA) basketball
GAME_SUFFIXES = ("GAME")  # series tickers ending with these are per-game outcomes
REQUEST_DELAY = 0.25  # seconds between paginated requests to stay polite
OUTPUT_FILE = Path(__file__).with_name("kalshi_sports_data.csv")
STATE_FILE = Path(__file__).with_name("kalshi_fetch_state.json")
FIELDNAMES = ["SeriesTicker", "SeriesName", "EventTicker", "MarketTicker", "Time",
              "EventEndTime", "VolumeInDollar", "OpenInterest", "YesProb", "NoProb", "EventResult",
              "RulesPrimary", "KalshiURL", "Tags"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update({"Accept": "application/json"})


def _slugify(text: str) -> str:
    """Convert text to a URL-friendly slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def build_kalshi_url(series_ticker: str, series_name: str, event_ticker: str) -> str:
    """Construct the Kalshi market URL."""
    return f"https://kalshi.com/markets/{series_ticker.lower()}/{_slugify(series_name)}/{event_ticker.lower()}"

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict:
    """GET request with basic retry / back-off."""
    url = f"{BASE_URL}{path}"
    for attempt in range(1, 4):
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 5))
                log.warning("Rate-limited, waiting %ds …", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.warning("Request failed (attempt %d): %s", attempt, exc)
            time.sleep(2 ** attempt)
    log.error("Giving up on %s", url)
    return {}


def paginate(path: str, collection_key: str, params: dict | None = None,
             limit: int = 200) -> list:
    """Auto-paginate a Kalshi list endpoint."""
    params = dict(params or {})
    params["limit"] = limit
    cursor = ""
    items: list = []
    while True:
        if cursor:
            params["cursor"] = cursor
        data = _get(path, params)
        if not data:
            break
        batch = data.get(collection_key, [])
        items.extend(batch)
        cursor = data.get("cursor", "")
        if not cursor or not batch:
            break
        time.sleep(REQUEST_DELAY)
    return items


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def get_basketball_series() -> list[dict]:
    """Return sports series tagged with 'Basketball' (pro + college)."""
    log.info("Fetching all series …")
    all_series = paginate("/series", "series")
    basketball = [
        s for s in all_series
        if s.get("category", "").lower() == SPORTS_CATEGORY.lower()
        and BASKETBALL_TAG.lower() in [t.lower() for t in (s.get("tags") or [])]
    ]
    log.info("Found %d basketball series out of %d total.", len(basketball), len(all_series))
    return basketball


def get_settled_markets(series_ticker: str, limit=1000) -> list[dict]:
    """Return settled markets for a given series ticker."""
    return paginate(
        "/markets",
        "markets",
        params={"series_ticker": series_ticker, "status": "settled"},
        limit=limit,
    )


def _extract_price(candle: dict) -> int | None:
    """Extract a usable price in cents from a candlestick, or None."""
    price_info = candle.get("price", {})
    return (
        price_info.get("close")
        or price_info.get("mean")
        or price_info.get("previous")
    )


def get_yes_prob_before_close(series_ticker: str, market: dict, hours_before: float = 6) -> tuple:
    """
    Use hourly candlestick data to find the YES probability and open interest
    approximately *hours_before* hours before the market's close_time.

    Strategy:
      1. Look in a 2-hour window centred on the target.
      2. If no candle with price data exists there, return (None, None, None).

    Returns (probability, snapshot_time_iso, open_interest) or (None, None, None)
    if unavailable.
    """
    close_time_str = market.get("close_time")
    if not close_time_str:
        return None, None, None

    close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
    target_dt = close_dt - timedelta(hours=hours_before)
    target_ts = int(target_dt.timestamp())

    # --- Pass 1: narrow 2-hour window around target ---
    start_ts = target_ts - 3600
    end_ts = target_ts + 3600

    data = _get(
        f"/series/{series_ticker}/markets/{market['ticker']}/candlesticks",
        params={
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": 60,  # 1-hour candles
        },
    )

    candles = data.get("candlesticks", [])
    # Filter to candles that have actual price data
    priced = [c for c in candles if _extract_price(c) is not None]

    if not priced:
        return None, None, None

    # Pick the candle closest to the target timestamp
    best = min(priced, key=lambda c: abs(c.get("end_period_ts", 0) - target_ts))
    price_cents = _extract_price(best)

    # The actual snapshot time from the candlestick
    snap_ts = best.get("end_period_ts", target_ts)
    snap_time = datetime.fromtimestamp(snap_ts, tz=timezone.utc).isoformat(timespec="seconds")

    # Open interest from the candlestick (contracts outstanding at end of period)
    open_interest = best.get("open_interest", 0) or 0

    return round(price_cents / 100.0, 4), snap_time, open_interest  # cents → probability


def compute_volume_dollars(market: dict) -> str:
    """
    Return volume in dollars.
    volume_fp is a fixed-point string (e.g. "10.00") representing dollar value.
    Falls back to volume (contract count) × notional_value_dollars when available.
    """
    # volume_fp is the volume in dollars as a fixed-point string
    vol_fp = market.get("volume_fp")
    if vol_fp:
        return vol_fp

    # fallback: volume × notional_value (cents) → dollars
    vol = market.get("volume", 0)
    notional_cents = market.get("notional_value", 100)  # default $1 contract
    return f"{vol * notional_cents / 100:.2f}"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_state(state_path: Path) -> dict:
    """Load persisted state from JSON file."""
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not load state file %s: %s", state_path, exc)
    return {"completed_series": [], "total_rows": 0}


def _save_state(state_path: Path, state: dict) -> None:
    """Persist state to JSON file."""
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _append_rows_to_csv(csv_path: Path, rows: list[dict], write_header: bool) -> None:
    """Append rows to the CSV file, optionally writing the header first."""
    mode = "w" if write_header else "a"
    with open(csv_path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch Kalshi sports market data.")
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=OUTPUT_FILE,
        help="Output CSV path (default: %(default)s)",
    )
    parser.add_argument(
        "--limit-series",
        type=int,
        default=0,
        help="Process only the first N sports series (0 = all, useful for testing).",
    )
    parser.add_argument(
        "--limit-markets",
        type=int,
        default=0,
        help="Process only the first N markets per series (0 = all).",
    )
    parser.add_argument(
        "--games-only",
        action="store_true",
        default=False,
        help="Only include per-game series (GAME/SPREAD/TOTAL), exclude futures like championships, MVP, draft, etc.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        default=False,
        help="Ignore saved state and start fresh (deletes state file and overwrites CSV).",
    )
    parser.add_argument(
        "--hours-before",
        type=float,
        default=6,
        help="Snapshot probability N hours before market close (default: %(default)s).",
    )
    args = parser.parse_args()

    state_path = args.output.with_suffix(".state.json")

    # --- State & resume handling ---
    if args.fresh:
        if state_path.exists():
            state_path.unlink()
            log.info("Deleted state file %s", state_path)
        state = {"completed_series": [], "total_rows": 0}
        # Write a fresh CSV with header
        _append_rows_to_csv(args.output, [], write_header=True)
    else:
        state = _load_state(state_path)
        if state["completed_series"]:
            log.info("Resuming – %d series already completed (%d rows so far).",
                     len(state["completed_series"]), state["total_rows"])
        else:
            # First run – write header
            _append_rows_to_csv(args.output, [], write_header=True)

    completed_set = set(state["completed_series"])

    sports_series = get_basketball_series()
    if not sports_series:
        log.error("No basketball series found. Exiting.")
        sys.exit(1)

    if args.games_only:
        before = len(sports_series)
        sports_series = [
            s for s in sports_series
            if s["ticker"].upper().endswith(GAME_SUFFIXES)
        ]
        log.info("--games-only: filtered %d → %d game-outcome series.", before, len(sports_series))

    if args.limit_series:
        sports_series = sports_series[: args.limit_series]

    total_rows = state["total_rows"]

    for idx, series in enumerate(sports_series, 1):
        sticker = series["ticker"]

        if sticker in completed_set:
            log.info("[%d/%d] Series: %s – already completed, skipping.",
                     idx, len(sports_series), sticker)
            continue

        log.info("[%d/%d] Series: %s – %s",
                 idx, len(sports_series), sticker, series.get("title", ""))

        markets = get_settled_markets(sticker, limit=args.limit_markets if args.limit_markets else 1000)
        log.info("  Found %d settled markets.", len(markets))

        if args.limit_markets:
            markets = markets[: args.limit_markets]

        series_name = series.get("title", sticker)

        # Keep only binary yes/no results
        markets = [m for m in markets if m.get("result", "").lower() in ("yes", "no")]
        log.info("  %d markets with yes/no result after filtering.", len(markets))

        series_rows: list[dict] = []

        for midx, mkt in enumerate(markets, 1):
            ticker = mkt["ticker"]
            event_ticker = mkt.get("event_ticker", "")
            result = mkt.get("result", "")
            close_time = mkt.get("close_time", "")
            volume_dollars = compute_volume_dollars(mkt)

            log.info("  [%d/%d] %s  result=%s", midx, len(markets), ticker, result)

            yes_prob, snap_time, open_interest = get_yes_prob_before_close(sticker, mkt, args.hours_before)
            if yes_prob is None:
                log.warning("    No candlestick price data at all for %s – skipping.", ticker)
                continue

            no_prob = round(1.0 - yes_prob, 4)

            tags = mkt.get("tags", []) or series.get("tags", [])
            tags_str = ";".join(tags) if tags else ""

            series_rows.append({
                "SeriesTicker": sticker,
                "SeriesName": series_name,
                "EventTicker": event_ticker,
                "MarketTicker": ticker,
                "Time": snap_time,
                "EventEndTime": close_time,
                "VolumeInDollar": volume_dollars,
                "OpenInterest": open_interest,
                "YesProb": yes_prob,
                "NoProb": no_prob,
                "EventResult": result,
                "Tags": tags_str,
                "RulesPrimary": mkt.get("rules_primary", ""),
                "KalshiURL": build_kalshi_url(sticker, series_name, event_ticker),
            })

            time.sleep(REQUEST_DELAY)

        # Append this series' rows to CSV
        if series_rows:
            _append_rows_to_csv(args.output, series_rows, write_header=False)
            total_rows += len(series_rows)
            log.info("  Appended %d rows to %s (total: %d)", len(series_rows), args.output, total_rows)

        # Persist state
        state["completed_series"].append(sticker)
        state["total_rows"] = total_rows
        completed_set.add(sticker)
        _save_state(state_path, state)

    log.info("Done. %d total rows in %s", total_rows, args.output)


def main2():
    markets = get_settled_markets("KXMVENBASINGLEGAME")
    print(markets)

if __name__ == "__main__":
    main()
