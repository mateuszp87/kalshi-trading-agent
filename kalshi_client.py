"""
Kalshi REST API client — April 2026
- Uses yes_bid_dollars / yes_ask_dollars (new API format)
- Uses volume_fp for real volume
- Includes sell_position_fp and get_market for exit management
"""

import base64, datetime, logging
import aiohttp
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    log.warning("cryptography not installed")


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    category: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    volume: int
    open_interest: int
    close_time: str
    status: str

    @property
    def mid_price(self) -> float:
        if self.yes_bid > 0 and self.yes_ask > 0:
            return round((self.yes_bid + self.yes_ask) / 2, 4)
        return self.yes_ask or self.yes_bid or 0.5

    @property
    def hours_until_close(self):
        if not self.close_time:
            return None
        try:
            dt = datetime.datetime.fromisoformat(self.close_time.replace("Z", "+00:00"))
            return max(0, (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600)
        except:
            return None

    @property
    def timeframe_label(self) -> str:
        h = self.hours_until_close
        if h is None:  return "unknown"
        if h <= 12:    return "TODAY"
        if h <= 48:    return "TOMORROW"
        if h <= 168:   return "THIS WEEK"
        if h <= 720:   return "THIS MONTH"
        return "LONG-TERM"


@dataclass
class OrderResult:
    order_id: str
    ticker: str
    side: str
    price: float
    count: int
    status: str
    filled: int = 0


def _parse_price(val) -> float:
    if val is None: return 0.0
    if isinstance(val, str):
        try: return float(val)
        except: return 0.0
    if isinstance(val, float):
        return val if val <= 1.0 else val / 100
    if isinstance(val, int):
        return val / 100 if val > 1 else float(val)
    return 0.0


def _parse_volume(m: dict) -> int:
    for key in ("volume_fp", "volume_24h_fp", "volume"):
        val = m.get(key)
        if val is not None:
            try: return int(float(val))
            except: pass
    return 0


class KalshiClient:
    def __init__(self, api_key: str, base_url: str, private_key_path: str = ""):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._private_key = None
        self._session = None
        if private_key_path and HAS_CRYPTO:
            try:
                with open(private_key_path, "rb") as f:
                    self._private_key = serialization.load_pem_private_key(
                        f.read(), password=None, backend=default_backend()
                    )
                log.info(f"Loaded RSA key from {private_key_path}")
            except Exception as e:
                log.error(f"Key load failed: {e}")

    def _make_headers(self, method: str, path: str) -> dict:
        if not self._private_key:
            return {"Content-Type": "application/json"}
        ts = str(int(datetime.datetime.now().timestamp() * 1000))
        sign_path = urlparse(self.base_url + path).path.split("?")[0]
        sig = self._private_key.sign(
            f"{ts}{method}{sign_path}".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *a):
        if self._session:
            await self._session.close()

    async def _get(self, path, params=None):
        url = f"{self.base_url}{path}"
        async with self._session.get(url, headers=self._make_headers("GET", path), params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def _post(self, path, payload):
        url = f"{self.base_url}{path}"
        async with self._session.post(url, headers=self._make_headers("POST", path), json=payload) as r:
            if r.status >= 400:
                err_body = await r.text()
                raise Exception(f"{r.status} {r.reason} | body={err_body[:400]} | sent={payload}")
            return await r.json()

    def _parse_market(self, m: dict, category: str = "") -> KalshiMarket:
        yes_bid = _parse_price(m.get("yes_bid_dollars") or m.get("yes_bid") or m.get("yes_price_dollars"))
        yes_ask = _parse_price(m.get("yes_ask_dollars") or m.get("yes_ask") or m.get("yes_ask_price"))
        return KalshiMarket(
            ticker=m.get("ticker", ""),
            title=m.get("title", m.get("subtitle", "")),
            category=category or m.get("category", ""),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=round(1 - yes_ask, 4) if yes_ask > 0 else 0.0,
            no_ask=round(1 - yes_bid, 4) if yes_bid > 0 else 0.0,
            volume=_parse_volume(m),
            open_interest=int(float(m.get("open_interest_fp") or m.get("open_interest") or 0)),
            close_time=m.get("close_time", m.get("expiration_time", "")),
            status=m.get("status", "open"),
        )

    async def get_series_markets(self, series_tickers: list, limit: int = 10) -> list:
        import random
        all_markets = []
        shuffled = series_tickers[:]
        random.shuffle(shuffled)
        for ticker in shuffled:
            try:
                data = await self._get("/markets", params={"series_ticker": ticker, "status": "open", "limit": limit})
                for m in data.get("markets", []):
                    parsed = self._parse_market(m, category=ticker)
                    if parsed and (parsed.yes_bid > 0 or parsed.yes_ask > 0):
                        all_markets.append(parsed)
            except Exception as e:
                log.warning(f"Series {ticker} failed: {e}")
        log.info(f"Fetched {len(all_markets)} markets from {len(series_tickers)} series")
        return all_markets

    async def get_all_open_markets(self, max_pages: int = 20) -> list:
        """Sweep every open market on Kalshi (paginated)."""
        out, cursor, page = [], None, 0
        while page < max_pages:
            page += 1
            params = {"status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await self._get("/markets", params=params)
            except Exception as e:
                log.warning(f"Market sweep page {page} failed: {e}")
                break
            batch = data.get("markets", [])
            for m in batch:
                p = self._parse_market(m, category=m.get("ticker", "").split("-")[0])
                if p and (p.yes_bid > 0 or p.yes_ask > 0):
                    out.append(p)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        log.info(f"Swept {len(out)} open markets across {page} pages")
        return out

    async def get_market(self, ticker: str) -> dict:
        """Get current price for a single ticker (used for exit checks)."""
        try:
            data = await self._get(f"/markets/{ticker}")
            m = data.get("market", data)
            return {
                "ticker": ticker,
                "yes_bid": _parse_price(m.get("yes_bid_dollars") or m.get("yes_bid")),
                "yes_ask": _parse_price(m.get("yes_ask_dollars") or m.get("yes_ask")),
                "no_bid": _parse_price(m.get("no_bid_dollars") or m.get("no_bid")),
                "no_ask": _parse_price(m.get("no_ask_dollars") or m.get("no_ask")),
                "volume": _parse_volume(m),
                "status": m.get("status", "open"),
                "close_time": m.get("close_time", ""),
            }
        except Exception as e:
            log.warning(f"get_market {ticker}: {e}")
            return {}


    async def get_orderbook(self, ticker: str) -> dict:
        """Fetch orderbook and compute imbalance signal for a market.
        Returns dict with:
          - yes_dollars_total: $ stacked on YES bids (buying pressure for YES)
          - no_dollars_total: $ stacked on NO bids (buying pressure for NO)
          - imbalance: -1 to +1 (positive = YES-heavy, negative = NO-heavy)
          - top_yes_bid: best YES bid price
          - top_no_bid: best NO bid price
          - yes_bid_depth_3c: $ available within 3c of top YES bid (liquidity)
          - no_bid_depth_3c: $ available within 3c of top NO bid
          - conviction_score: 0-1, weighted by proximity to mid price
        """
        try:
            data = await self._get(f"/markets/{ticker}/orderbook")
            ob = data.get("orderbook_fp", data.get("orderbook", {}))
            yes_book = ob.get("yes_dollars") or ob.get("yes") or []
            no_book = ob.get("no_dollars") or ob.get("no") or []
            if not yes_book and not no_book:
                return {}

            def parse_side(book):
                # Kalshi returns sorted by price asc. Top bid = highest price.
                # Each entry: [price_str_dollars, count_str]
                parsed = []
                for entry in book:
                    try:
                        p = float(entry[0])
                        c = float(entry[1])
                        if p > 0 and c > 0:
                            parsed.append((p, c))
                    except (ValueError, IndexError, TypeError):
                        continue
                return parsed

            yes_parsed = parse_side(yes_book)
            no_parsed = parse_side(no_book)

            # Top-of-book prices (highest bid on each side)
            top_yes = max((p for p, _ in yes_parsed), default=0)
            top_no = max((p for p, _ in no_parsed), default=0)

            # Total dollar volume on each side (count * price, the actual $ at risk)
            yes_dollars = round(sum(p * c for p, c in yes_parsed), 2)
            no_dollars = round(sum(p * c for p, c in no_parsed), 2)

            # Liquidity within 3c of top bid
            yes_depth_3c = round(sum(p * c for p, c in yes_parsed if p >= top_yes - 0.03), 2)
            no_depth_3c = round(sum(p * c for p, c in no_parsed if p >= top_no - 0.03), 2)

            # Imbalance: -1 (all NO) to +1 (all YES)
            total = yes_dollars + no_dollars
            imbalance = round((yes_dollars - no_dollars) / total, 3) if total > 0 else 0

            # Conviction: favor money stacked near the action (within 10c of top)
            yes_near = sum(p * c for p, c in yes_parsed if p >= top_yes - 0.10)
            no_near = sum(p * c for p, c in no_parsed if p >= top_no - 0.10)
            total_near = yes_near + no_near
            conviction = round(total_near / total, 3) if total > 0 else 0

            return {
                "yes_dollars_total": yes_dollars,
                "no_dollars_total": no_dollars,
                "imbalance": imbalance,
                "top_yes_bid": round(top_yes, 4),
                "top_no_bid": round(top_no, 4),
                "yes_depth_3c": yes_depth_3c,
                "no_depth_3c": no_depth_3c,
                "conviction_score": conviction,
            }
        except Exception as e:
            return {}

    async def get_balance(self) -> float:
        try:
            data = await self._get("/portfolio/balance")
            return round(float(data.get("balance", 0)) / 100, 2)
        except Exception as e:
            log.error(f"Balance error: {e}")
            return 0.0

    async def get_positions(self) -> list:
        try:
            data = await self._get("/portfolio/positions")
            return data.get("market_positions", data.get("positions", []))
        except Exception as e:
            log.error(f"Positions error: {e}")
            return []

    async def get_settlements(self) -> list:
        try:
            data = await self._get("/portfolio/settlements")
            return data.get("settlements", [])
        except Exception as e:
            log.error(f"Settlements error: {e}")
            return []

    async def place_order(self, ticker, side, price_dollars, count):
        """V2 endpoint (migrated 2026-07-08).
        V2 quotes only from YES side: side='bid' = buy YES, side='ask' = sell YES (= buy NO).
        Price is decimal string, count is decimal string, needs client_order_id + self_trade_prevention_type.
        """
        import uuid
        # Clamp yes-side price between $0.01 and $0.99
        yes_price = round(max(0.01, min(0.99, price_dollars if side == "yes" else 1 - price_dollars)), 4)
        # V2 side mapping: yes buy → bid, no buy → ask (sells YES at 1-no_price = yes_price economic equivalent)
        # BUT V2 always quotes YES side, so no-buy price = yes-side ask price (which equals 1 - no_ask target)
        # We already computed yes_price such that: yes-side buy at yes_price = same economics as no-side buy at 1-yes_price
        v2_side = "bid" if side == "yes" else "ask"
        v2_price = f"{yes_price:.4f}"
        v2_count = f"{int(count)}.00"
        payload = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": v2_side,
            "count": v2_count,
            "price": v2_price,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }
        try:
            data = await self._post("/portfolio/events/orders", payload)
            o = data if "order_id" in data else data.get("order", {})
            return OrderResult(
                order_id=o.get("order_id", ""), ticker=ticker, side=side,
                price=yes_price, count=count,
                status=o.get("status", "unknown"),
                filled=int(o.get("filled_count", 0)),
            )
        except Exception as e:
            log.error(f"Order failed ({ticker} {side} x{count}): {e}")
            return None

    async def sell_position_fp(self, ticker, side, count, price_dollars):
        yes_price = round(max(0.01, min(0.99, price_dollars if side == "yes" else 1 - price_dollars)), 2)
        no_price = round(1 - yes_price, 2)
        if side == "yes":
            payload = {
                "ticker": ticker, "side": "yes", "type": "limit",
                "count": int(count), "action": "sell",
                "yes_price": int(round(yes_price * 100)),
            }
        else:
            payload = {
                "ticker": ticker, "side": "no", "type": "limit",
                "count": int(count), "action": "sell",
                "no_price": int(round(no_price * 100)),
            }
        try:
            data = await self._post("/portfolio/events/orders", payload)
            o = data if "order_id" in data else data.get("order", {})
            return OrderResult(
                order_id=o.get("order_id", ""), ticker=ticker, side=side,
                price=yes_price, count=count,
                status=o.get("status", "unknown"),
                filled=int(o.get("filled_count", 0)),
            )
        except Exception as e:
            log.error(f"Sell failed ({ticker} {side}): {e}")
            return None
