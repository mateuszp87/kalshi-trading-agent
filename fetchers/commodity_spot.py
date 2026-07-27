"""Live commodity spot + realized-vol source for Kalshi commodity ladders.

Kalshi commodity contracts (KXWTI, KXGOLD, KXSILVER, ...) settle on a
POINT-IN-TIME price at a fixed time (e.g. 5:00 PM EDT). That is exactly the
settlement type a lognormal digital model can price correctly, unlike crypto
KXBTCD which settles on a 60-second average and is therefore benched.

This module supplies the two inputs pricing needs and the agent currently
lacks: current spot, and an annualized realized volatility from recent daily
closes. Source is Yahoo Finance's free chart endpoint (no API key).
"""
import math, logging
import aiohttp

log = logging.getLogger(__name__)

# Kalshi commodity root -> Yahoo futures symbol
ROOT_TO_YAHOO = {
    "KXWTI":    "CL=F",
    "KXOIL":    "CL=F",
    "KXBRENT":  "BZ=F",
    "KXHOIL":   "HO=F",
    "KXGOLD":   "GC=F",
    "KXSILVER": "SI=F",
    "KXCOPPER": "HG=F",
    "KXNATGAS": "NG=F",
    "KXCORN":   "ZC=F",
    "KXWHEAT":  "ZW=F",
}

_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1mo"


def root_from_ticker(ticker: str) -> str:
    """KXWTI-26JUL1614-T79.49 -> KXWTI (longest matching known root)."""
    t = ticker.upper()
    for root in sorted(ROOT_TO_YAHOO, key=len, reverse=True):
        if t.startswith(root):
            return root
    return ""


def annualized_vol(closes) -> float:
    """Annualized vol from daily log-returns of recent closes."""
    closes = [c for c in closes if c and c > 0]
    if len(closes) < 3:
        return 0.0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / max(1, n - 1)
    daily_sd = math.sqrt(var)
    return daily_sd * math.sqrt(252.0)


async def fetch_spot_and_vol(session, root: str):
    """Returns {spot, annual_vol, symbol, n_closes} or None on failure."""
    sym = ROOT_TO_YAHOO.get(root)
    if not sym:
        return None
    url = _CHART.format(sym=sym)
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log.warning(f"commodity spot {root}/{sym}: HTTP {r.status}")
                return None
            d = await r.json()
        res = d["chart"]["result"][0]
        spot = res["meta"].get("regularMarketPrice")
        closes = res["indicators"]["quote"][0].get("close", [])
        closes = [c for c in closes if c is not None]
        if spot is None and closes:
            spot = closes[-1]
        if not spot or spot <= 0:
            return None
        vol = annualized_vol(closes)
        return {"spot": float(spot), "annual_vol": vol,
                "symbol": sym, "n_closes": len(closes)}
    except Exception as e:
        log.warning(f"commodity spot {root}/{sym}: {e}")
        return None
