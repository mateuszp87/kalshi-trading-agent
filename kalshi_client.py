import base64
import datetime
import logging
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
        return round((self.yes_bid + self.yes_ask) / 2, 4)

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
    if val is None:
        return 0.5
    if isinstance(val, str):
        return float(val)
    if isinstance(val, float):
        return val if val <= 1.0 else val / 100
    if isinstance(val, int):
        return val / 100 if val > 1 else float(val)
    return 0.5

class KalshiClient:
    def __init__(self, api_key: str, base_url: str, private_key_path: str = ""):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._private_key = None
        self._session: Optional[aiohttp.ClientSession] = None

        if private_key_path and HAS_CRYPTO:
            try:
                with open(private_key_path, "rb") as f:
                    self._private_key = serialization.load_pem_private_key(
                        f.read(), password=None, backend=default_backend()
                    )
                log.info(f"Loaded RSA private key from {private_key_path}")
            except Exception as e:
                log.warning(f"Could not load private key: {e}")

    def _make_headers(self, method: str, path: str) -> dict:
        ts = str(int(datetime.datetime.now().timestamp() * 1000))
        # Use urlparse exactly as Kalshi docs show
        sign_path = urlparse(self.base_url + path).path.split('?')[0]

        if self._private_key and HAS_CRYPTO:
            try:
                message = f"{ts}{method.upper()}{sign_path}".encode('utf-8')
                sig_bytes = self._private_key.sign(
                    message,
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH,
                    ),
                    hashes.SHA256(),
                )
                return {
                    "KALSHI-ACCESS-KEY": self.api_key,
                    "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig_bytes).decode(),
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                    "Content-Type": "application/json",
                }
            except Exception as e:
                log.warning(f"RSA sign failed: {e}")

        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._make_headers("GET", path)
        async with self._session.get(url, headers=headers, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._make_headers("POST", path)
        async with self._session.post(url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_markets(self, keyword: str = "", limit: int = 20, status: str = "open") -> list[KalshiMarket]:
        params = {"limit": limit, "status": status}
        if keyword:
            params["search"] = keyword
        try:
            data = await self._get("/markets", params=params)
            markets = []
            for m in data.get("markets", []):
                yes_bid = _parse_price(m.get("yes_bid") or m.get("yes_price") or 0.5)
                yes_ask = _parse_price(m.get("yes_ask") or m.get("yes_ask_price") or 0.5)
                markets.append(KalshiMarket(
                    ticker=m.get("ticker", ""),
                    title=m.get("title", m.get("subtitle", m.get("question", ""))),
                    category=m.get("category", ""),
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=round(1 - yes_ask, 4),
                    no_ask=round(1 - yes_bid, 4),
                    volume=int(m.get("volume", 0) or 0),
                    open_interest=int(m.get("open_interest", 0) or 0),
                    close_time=m.get("close_time", m.get("expiration_time", "")),
                    status=m.get("status", "open"),
                ))
            log.info(f"Fetched {len(markets)} markets (keyword='{keyword}')")
            return markets
        except Exception as e:
            log.error(f"Error fetching markets: {e}")
            return []

    async def get_balance(self) -> float:
        try:
            data = await self._get("/portfolio/balance")
            bal = data.get("balance", data.get("available_balance", 0))
            
            return round(float(bal) / 100, 2)
        except Exception as e:
            log.error(f"Error fetching balance: {e}")
            return 0.0

    async def place_order(self, ticker: str, side: str, price_dollars: float, count: int, order_type: str = "limit") -> Optional[OrderResult]:
        yes_price = price_dollars if side == "yes" else round(1 - price_dollars, 4)
        payload = {
            "ticker": ticker,
            "side": side,
            "type": order_type,
            "count": count,
            "yes_price": str(round(yes_price, 4)),
            "no_price": str(round(1 - yes_price, 4)),
        }
        try:
            data = await self._post("/portfolio/orders", payload)
            order = data.get("order", {})
            return OrderResult(
                order_id=order.get("id", order.get("order_id", "")),
                ticker=ticker, side=side, price=yes_price, count=count,
                status=order.get("status", "unknown"),
                filled=int(order.get("filled_count", 0)),
            )
        except Exception as e:
            log.error(f"Order failed ({ticker} {side} @{yes_price}): {e}")
            return None

    async def get_events(self, keyword: str = "", limit: int = 25) -> list[KalshiMarket]:
        """Fetch events endpoint — returns clean single-outcome markets."""
        try:
            params = {"limit": limit}
            if keyword:
                params["search"] = keyword
            data = await self._get("/events", params=params)
            markets = []
            for event in data.get("events", []):
                # Each event has one or more markets
                event_markets = event.get("markets", [])
                event_title = event.get("title", "")
                event_category = event.get("category", "")
                if not event_markets:
                    # Event itself is the market
                    yes_bid = _parse_price(event.get("yes_bid") or 0.5)
                    yes_ask = _parse_price(event.get("yes_ask") or 0.5)
                    markets.append(KalshiMarket(
                        ticker=event.get("event_ticker", event.get("ticker", "")),
                        title=event_title,
                        category=event_category,
                        yes_bid=yes_bid,
                        yes_ask=yes_ask,
                        no_bid=round(1 - yes_ask, 4),
                        no_ask=round(1 - yes_bid, 4),
                        volume=int(event.get("volume", 0) or 0),
                        open_interest=int(event.get("open_interest", 0) or 0),
                        close_time=event.get("close_time", event.get("end_date", "")),
                        status=event.get("status", "open"),
                    ))
                for market in event_markets:
                    yes_bid = _parse_price(market.get("yes_bid") or 0.5)
                    yes_ask = _parse_price(market.get("yes_ask") or 0.5)
                    title = market.get("title", "") or event_title
                    markets.append(KalshiMarket(
                        ticker=market.get("ticker", ""),
                        title=title,
                        category=event_category,
                        yes_bid=yes_bid,
                        yes_ask=yes_ask,
                        no_bid=round(1 - yes_ask, 4),
                        no_ask=round(1 - yes_bid, 4),
                        volume=int(market.get("volume", 0) or 0),
                        open_interest=int(market.get("open_interest", 0) or 0),
                        close_time=market.get("close_time", event.get("end_date", "")),
                        status=market.get("status", "open"),
                    ))
            log.info(f"Fetched {len(markets)} event markets (keyword='{keyword}')")
            return markets
        except Exception as e:
            log.error(f"Error fetching events: {e}")
            return []

    async def get_series_markets(self, series_tickers: list, limit: int = 10) -> list[KalshiMarket]:
        """Fetch markets from specific series tickers — guaranteed liquid markets."""
        import random
        markets = []
        # Rotate through series randomly for variety
        random.shuffle(series_tickers)
        for ticker in series_tickers[:6]:
            try:
                data = await self._get("/markets", params={"series_ticker": ticker, "status": "open", "limit": limit})
                for m in data.get("markets", []):
                    yes_bid = _parse_price(m.get("yes_bid_dollars") or m.get("yes_bid") or 0.5)
                    yes_ask = _parse_price(m.get("yes_ask_dollars") or m.get("yes_ask") or 0.5)
                    if yes_bid == 0.0 and yes_ask == 0.0:
                        continue  # skip markets with no pricing
                    markets.append(KalshiMarket(
                        ticker=m.get("ticker", ""),
                        title=m.get("title", ""),
                        category=m.get("category", ticker),
                        yes_bid=yes_bid,
                        yes_ask=yes_ask,
                        no_bid=round(1 - yes_ask, 4),
                        no_ask=round(1 - yes_bid, 4),
                        volume=int(float(m.get("volume", m.get("volume_fp", 0)) or 0)),
                        open_interest=int(float(m.get("open_interest", 0) or 0)),
                        close_time=m.get("close_time", m.get("expiration_time", "")),
                        status=m.get("status", "open"),
                    ))
            except Exception as e:
                log.warning(f"Series {ticker} fetch failed: {e}")
        log.info(f"Fetched {len(markets)} markets from {len(series_tickers)} series")
        return markets

    async def get_positions(self) -> list[dict]:
        try:
            data = await self._get("/portfolio/positions")
            return data.get("market_positions", data.get("positions", []))
        except Exception as e:
            log.error(f"Error fetching positions: {e}")
            return []
