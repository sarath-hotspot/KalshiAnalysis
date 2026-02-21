"""
Kalshi Live Trading Bot — Basketball NO Strategy

Monitors open basketball markets on Kalshi and places NO bets on markets
where volume is between $100–$500 and YES probability > 65%.

Usage:
    python live_trading.py                     # Run with defaults ($1 bet)
    python live_trading.py --bet-amount 2.00   # Bet $2 per market
    python live_trading.py --dry-run           # Preview without placing orders

Credentials:
    env/.env            → API_KEY_ID=<your-key>
    env/api_privatekey  → RSA private key (PEM)
"""

import argparse
import base64
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
ENV_DIR = Path(__file__).with_name("env")

# Strategy defaults (overridable via CLI)
BET_AMOUNT_DOLLARS = 1.0        # Total dollars to spend per market
BET_CONTRACTS = 1               # Number of contracts to buy per market (takes priority over bet_amount)
VOLUME_MIN = 100_000                # Minimum market volume ($)
VOLUME_MAX = 500_000                # Maximum market volume ($)
YES_PROB_MIN = 0.60             # Bet NO when YES prob exceeds this
YES_PROB_MAX = 0.90             # Skip markets where YES prob exceeds this
HOURS_AHEAD = 6                 # Look at games closing within N hours
BASKETBALL_TAG = "Basketball"
SPORTS_CATEGORY = "Sports"
REQUEST_DELAY = 0.25            # Seconds between API calls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------

def load_credentials() -> tuple:
    """Load API key ID and RSA private key from the env/ folder."""
    # --- API key ID from .env ---
    env_file = ENV_DIR / ".env"
    api_key_id = None
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("API_KEY_ID="):
                api_key_id = line.split("=", 1)[1].strip()

    if not api_key_id:
        raise ValueError("API_KEY_ID not found in env/.env")

    # --- RSA private key ---
    key_file = ENV_DIR / "api_privatekey.pem"
    with open(key_file, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    log.info("Credentials loaded (key ID: %s…)", api_key_id[:12])
    return api_key_id, private_key


def _sign(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """RSA-PSS-SHA256 signature of  timestamp + METHOD + path (no query string)."""
    # Strip query string from path for signing
    clean_path = path.split("?")[0]
    message = f"{timestamp_ms}{method.upper()}{clean_path}".encode("utf-8")
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


# ---------------------------------------------------------------------------
# Authenticated Kalshi client
# ---------------------------------------------------------------------------

class KalshiClient:
    """Thin wrapper around the Kalshi Trade API v2 with RSA auth."""

    def __init__(self):
        self.api_key_id, self.private_key = load_credentials()
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        # Extract base path once (e.g. "/trade-api/v2")
        self._base_path = urlparse(BASE_URL).path  # /trade-api/v2

    # --- low-level helpers ---

    def _auth_headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        sig = _sign(self.private_key, ts, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """Authenticated GET.  `endpoint` is relative, e.g. '/markets'."""
        url = f"{BASE_URL}{endpoint}"
        sign_path = f"{self._base_path}{endpoint}"
        for attempt in range(1, 4):
            try:
                resp = self.session.get(
                    url, params=params,
                    headers=self._auth_headers("GET", sign_path),
                    timeout=30,
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 5))
                    log.warning("Rate-limited – waiting %ds", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                log.warning("GET %s failed (attempt %d): %s", endpoint, attempt, exc)
                time.sleep(2 ** attempt)
        log.error("Giving up on GET %s", endpoint)
        return {}

    def _post(self, endpoint: str, body: dict) -> dict:
        """Authenticated POST."""
        url = f"{BASE_URL}{endpoint}"
        sign_path = f"{self._base_path}{endpoint}"
        for attempt in range(1, 4):
            try:
                resp = self.session.post(
                    url, json=body,
                    headers=self._auth_headers("POST", sign_path),
                    timeout=30,
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 5))
                    log.warning("Rate-limited – waiting %ds", wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    log.error("POST %s → %d: %s", endpoint, resp.status_code, resp.text)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                log.warning("POST %s failed (attempt %d): %s", endpoint, attempt, exc)
                time.sleep(2 ** attempt)
        log.error("Giving up on POST %s", endpoint)
        return {}

    def _paginate(self, endpoint: str, key: str,
                  params: dict | None = None, limit: int = 200) -> list:
        params = dict(params or {})
        params["limit"] = limit
        cursor = ""
        items: list = []
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get(endpoint, params)
            if not data:
                break
            batch = data.get(key, [])
            items.extend(batch)
            cursor = data.get("cursor", "")
            if not cursor or not batch:
                break
            time.sleep(REQUEST_DELAY)
        return items

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    def get_basketball_series(self) -> list[dict]:
        """Return sports series tagged with 'Basketball' (pro + college)."""
        all_series = self._paginate("/series", "series")
        basketball = [
            s for s in all_series
            if s.get("category", "").lower() == SPORTS_CATEGORY.lower()
            and BASKETBALL_TAG.lower() in [t.lower() for t in (s.get("tags") or [])]
            and s.get("ticker").lower().endswith("game")
        ]
        log.info("Found %d basketball series out of %d total.", len(basketball), len(all_series))
        return basketball

    def get_basketball_markets(self, hours: float) -> list[dict]:
        """Return open basketball markets whose close time is within *hours*."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)

        # Step 1 — discover basketball series
        series_list = self.get_basketball_series()
        if not series_list:
            log.warning("No basketball series found.")
            return []
        
        log.info(f"Found {len(series_list)} basketball series. Fetching open markets for each…")
        # Step 2 — fetch open markets per series
        results = []
        for series in series_list:
            sticker = series["ticker"]
            markets = self._paginate(
                "/markets", "markets",
                params={"series_ticker": sticker, "status": "open"},
            )
            for m in markets:
                close_str = m.get("expected_expiration_time") or m.get("close_time") or m.get("expiration_time") or ""
                if not close_str:
                    continue
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                if not (now <= close_dt <= cutoff):
                    continue
                results.append(m)
            time.sleep(REQUEST_DELAY)

        return results

    # ------------------------------------------------------------------
    # Portfolio helpers
    # ------------------------------------------------------------------

    def get_no_position(self, ticker: str) -> int:
        """Return total NO contracts we currently hold for *ticker*.
        position_fp is negative for NO positions, positive for YES."""
        data = self._get("/portfolio/positions", params={"ticker": ticker})
        positions = data.get("market_positions", [])
        for pos in positions:
            if pos.get("ticker") == ticker:
                position_fp = float(pos.get("position_fp", 0) or 0)
                # Negative means NO contracts held
                if position_fp < 0:
                    return int(abs(position_fp))
                return 0
        return 0

    def get_resting_no_orders(self, ticker: str) -> int:
        """Return NO contracts in resting (open) buy orders for *ticker*."""
        data = self._get("/portfolio/orders", params={"ticker": ticker, "status": "resting"})
        orders = data.get("orders", [])
        total = 0
        for o in orders:
            if (o.get("ticker") == ticker
                    and o.get("side", "").lower() == "no"
                    and o.get("action", "").lower() == "buy"):
                total += o.get("remaining_count", 0) or 0
        return total

    def existing_no_contracts(self, ticker: str) -> int:
        """Held + resting-order NO contracts."""
        return self.get_no_position(ticker) + self.get_resting_no_orders(ticker)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_market(self, ticker: str) -> dict:
        """Fetch the latest market data for a single ticker."""
        data = self._get(f"/markets/{ticker}")
        return data.get("market", data)

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_no_order(self, ticker: str, count: int, no_price_cents: int) -> dict:
        """Place a market order to BUY *count* NO contracts at *no_price_cents*."""
        body = {
            "action": "buy",
            "side": "no",
            "count": count,
            "type": "market",
            "ticker": ticker,
            "no_price": no_price_cents,
        }
        log.info("  >> Placing order: BUY %d NO on %s @ %d¢", count, ticker, no_price_cents)
        return self._post("/portfolio/orders", body)


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------

def _volume_dollars(market: dict) -> float:
    """Market lifetime volume in USD."""
    vol_fp = market.get("volume_fp")
    if vol_fp:
        return float(vol_fp)
    vol = market.get("volume", 0)
    notional_cents = market.get("notional_value", 100)
    return vol * notional_cents / 100.0


def _yes_prob(market: dict) -> float:
    """Current YES probability (0-1) from the last traded price."""
    price = market.get("last_price") or market.get("yes_bid") or 0
    return price / 100.0


def _no_price_cents(market: dict) -> int:
    """Best estimate of the NO price in cents."""
    no = market.get("no_bid") or market.get("no_ask")
    if no:
        return no
    yes = market.get("last_price") or market.get("yes_bid") or 50
    return 100 - yes


# ---------------------------------------------------------------------------
# Main strategy
# ---------------------------------------------------------------------------

def run(client: KalshiClient, *,
        bet_amount: float,
        bet_contracts: int,
        volume_min: float,
        volume_max: float,
        yes_min_threshold: float,
        yes_max_threshold: float,
        hours: float,
        dry_run: bool):
    log.info("=" * 60)
    log.info("Kalshi Live Trading Bot — Basketball NO Strategy")
    log.info("  Volume range  : $%.0f – $%.0f", volume_min, volume_max)
    log.info("  YES threshold : %.0f%%–%.0f%%", yes_min_threshold * 100, yes_max_threshold * 100)
    log.info("  Bet contracts : %d", bet_contracts)
    log.info("  Bet amount    : $%.2f (fallback if --bet-contracts not set)", bet_amount)
    log.info("  Time window   : next %.1f hours", hours)
    log.info("  Mode          : %s", "DRY RUN" if dry_run else "LIVE")
    log.info("=" * 60)

    # 1 — Discover markets
    markets = client.get_basketball_markets(hours)
    log.info("Found %d open basketball markets in window.", len(markets))
    if not markets:
        log.info("Nothing to do.")
        return

    candidates = bets_placed = bets_skipped = 0

    for mkt in markets:
        ticker = mkt["ticker"]
        title = mkt.get("title", ticker)
        volume = _volume_dollars(mkt)
        yes_p = _yes_prob(mkt)
        no_p_cents = _no_price_cents(mkt)

        log.info("  ── %s", title)
        log.info("     Ticker: %s | Vol: $%.0f | YES: %.1f%% | NO price: %d¢",
                 ticker, volume, yes_p * 100, no_p_cents)

        # 2 — Filter: volume $100-$500
        if not (volume_min <= volume <= volume_max):
            log.info("     SKIP  volume $%.0f outside $%.0f–$%.0f", volume, volume_min, volume_max)
            continue

        # 3 — Filter: YES prob within threshold range
        if yes_p <= yes_min_threshold:
            log.info("     SKIP  YES %.1f%% <= %.0f%% min threshold", yes_p * 100, yes_min_threshold * 100)
            continue
        if yes_p > yes_max_threshold:
            log.info("     SKIP  YES %.1f%% > %.0f%% max threshold", yes_p * 100, yes_max_threshold * 100)
            continue

        candidates += 1

        # Calculate desired NO contract count
        # bet_contracts takes priority; fall back to bet_amount / price
        if no_p_cents <= 0:
            log.warning("     SKIP  NO price is 0")
            continue
        desired = bet_contracts if bet_contracts > 0 else max(int(bet_amount * 100) // no_p_cents, 1)

        # 4 — Check existing position / orders
        existing = client.existing_no_contracts(ticker)
        log.info("     Currently hold %d NO contracts (including resting orders)", existing)
        if existing >= desired:
            log.info("     SKIP  already hold %d NO (need %d)", existing, desired)
            bets_skipped += 1
            continue

        missing = desired - existing
        if existing > 0:
            log.info("     Have %d NO, placing %d more", existing, missing)

        # 5 — Refresh market data for latest prices before ordering
        fresh = client.get_market(ticker)
        fresh_yes_p = _yes_prob(fresh)

        # Use the NO ask so the order fills immediately
        fresh_no_bid = fresh.get("no_bid") or 0
        fresh_no_ask = fresh.get("no_ask") or 0
        log.info("     Refreshed: NO bid=%d¢  ask=%d¢  YES=%.1f%%",
                 fresh_no_bid, fresh_no_ask, fresh_yes_p * 100)

        if fresh_no_ask <= 0:
            log.warning("     SKIP  NO ask is 0 (no liquidity)")
            continue

        # Guard: only cross the spread if bid-ask spread <= 2¢
        spread = fresh_no_ask - fresh_no_bid
        if fresh_no_bid > 0 and spread > 2:
            log.info("     SKIP  bid-ask spread %d¢ > 2¢ limit (bid=%d¢ ask=%d¢)",
                     spread, fresh_no_bid, fresh_no_ask)
            continue

        order_price = fresh_no_ask

        # Recalculate count with fresh price
        desired = bet_contracts if bet_contracts > 0 else max(int(bet_amount * 100) // order_price, 1)
        missing = max(desired - existing, 1)

        # 6 — Place order at the ask for immediate fill
        if dry_run:
            log.info("     [DRY RUN] would BUY %d NO on %s @ %d¢ (ask)", missing, ticker, order_price)
            bets_placed += 1
        else:
            result = client.place_no_order(ticker, missing, order_price)
            if result:
                log.info("     OK  order submitted (%d NO @ %d¢ ask)", missing, order_price)
                bets_placed += 1
            else:
                log.error("     FAIL  order rejected for %s", ticker)

        time.sleep(REQUEST_DELAY)

    # Summary
    log.info("=" * 60)
    log.info("Summary")
    log.info("  Markets scanned        : %d", len(markets))
    log.info("  Met strategy criteria   : %d", candidates)
    log.info("  Orders placed           : %d", bets_placed)
    log.info("  Skipped (already filled): %d", bets_skipped)
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Kalshi Basketball NO-bet trading bot",
    )
    parser.add_argument("--bet-amount", type=float, default=BET_AMOUNT_DOLLARS,
                        help="Dollars to spend per market, used when --bet-contracts is 0 (default: $%(default).2f)")
    parser.add_argument("--bet-contracts", type=int, default=BET_CONTRACTS,
                        help="Number of contracts per market; takes priority over --bet-amount (default: %(default)s)")
    parser.add_argument("--volume-min", type=float, default=VOLUME_MIN,
                        help="Min volume in $ (default: %(default)s)")
    parser.add_argument("--volume-max", type=float, default=VOLUME_MAX,
                        help="Max volume in $ (default: %(default)s)")
    parser.add_argument("--yes-min", type=float, default=YES_PROB_MIN * 100,
                        help="Min YES probability threshold %% (default: %(default)s)")
    parser.add_argument("--yes-max", type=float, default=YES_PROB_MAX * 100,
                        help="Max YES probability threshold %% (default: %(default)s)")
    parser.add_argument("--hours", type=float, default=HOURS_AHEAD,
                        help="Look-ahead window in hours (default: %(default)s)")
    parser.add_argument("--dry-run", default=False, action="store_true",
                        help="Preview actions without placing real orders")
    args = parser.parse_args()

    client = KalshiClient()

    run(
        client,
        bet_amount=args.bet_amount,
        bet_contracts=args.bet_contracts,
        volume_min=args.volume_min,
        volume_max=args.volume_max,
        yes_min_threshold=args.yes_min / 100.0,
        yes_max_threshold=args.yes_max / 100.0,
        hours=args.hours,
        dry_run=args.dry_run,
    )


def test_endpoints():
    client = KalshiClient()
    market_ticker = "KXNBLGAME-26FEB190130CARNZB-NZB"    
    # client.place_no_order(market_ticker, 1, 2)
    # print(client.get_resting_no_orders(market_ticker))
    print(client.get_no_position(market_ticker))
    
if __name__ == "__main__":
    main()
