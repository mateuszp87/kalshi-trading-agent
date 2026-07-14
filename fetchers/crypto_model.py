"""Probability model for Kalshi BTC/ETH daily strike markets.

Kalshi 'will BTC close above $X today' is a binary option. Its fair value is
computable from live spot, time-to-close, and volatility — the same way a
desk prices a digital. Most Kalshi traders eyeball it. We compute it, and
enter only when our probability diverges from the market by more than the
spread. That gap is the edge.
"""
import math, logging
import aiohttp

log = logging.getLogger(__name__)

import re
from datetime import datetime, timezone

_MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
           "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def parse_crypto_ticker(ticker):
    """Parse ONLY daily dated crypto strike markets: KXBTC-26MAR14-100000.

    Deliberately returns None (skip) for 15-minute (KXBTC15M) and hourly
    (KXBTCD) markets. Those settle on a 60-second CF Benchmarks index average
    and reprice tens of cents per minute; a lognormal realized-vol model is NOT
    valid for them (extrapolating vol to 15 min is inventing a number, not
    measuring one). We only trade the frequency our model can actually price.
    Returns {asset, strike, hours_left} or None."""
    t = ticker.upper().strip()
    if t.startswith(("KXBTC15M", "KXETH15M", "KXBTCD", "KXETHD")):
        return None
    m = re.match(r"^KX(BTC|ETH)-(\d{2})([A-Z]{3})(\d{2})-(\d+)$", t)
    if not m:
        return None
    asset, yy, mon, dd, strike = m.groups()
    if mon not in _MONTHS:
        return None
    try:
        expiry = datetime(2000 + int(yy), _MONTHS[mon], int(dd), 21, 0, tzinfo=timezone.utc)
    except ValueError:
        return None
    hours_left = (expiry - datetime.now(timezone.utc)).total_seconds() / 3600.0
    return {"asset": asset, "strike": float(strike), "hours_left": hours_left}

SPOT = {
    "BTC": "https://api.coinbase.com/v2/prices/BTC-USD/spot",
    "ETH": "https://api.coinbase.com/v2/prices/ETH-USD/spot",
}
# Annualized vol estimates; refine from realized vol later.
ANNUAL_VOL = {"BTC": 0.55, "ETH": 0.70}


async def spot_price(session, asset):
    try:
        async with session.get(SPOT[asset], timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json()
            return float(d["data"]["amount"])
    except Exception as e:
        log.warning(f"spot {asset}: {e}")
        return None


def prob_above(spot, strike, hours_left, annual_vol):
    """P(price > strike at expiry) under lognormal diffusion."""
    if hours_left <= 0 or spot <= 0:
        return 1.0 if spot > strike else 0.0
    t = hours_left / (365.0 * 24.0)
    sigma = annual_vol * math.sqrt(t)
    if sigma <= 0:
        return 1.0 if spot > strike else 0.0
    # drift ~0 for a daily horizon; d2 of Black-Scholes
    d2 = (math.log(spot / strike) - 0.5 * sigma**2) / sigma
    return 0.5 * (1 + math.erf(d2 / math.sqrt(2)))


async def realized_vol(session, asset, days=30):
    """Annualized realized volatility from daily closes (keyless public endpoint).
    Replaces the hardcoded ANNUAL_VOL guess, which was materially wrong
    (measured BTC ~33% vs guessed 55%). Returns float or None on failure."""
    pair = f"{asset}-USD"
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=86400"
    try:
        async with session.get(url, headers={"User-Agent": "kalshi-bot"},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            candles = await r.json()
        closes = [c[4] for c in candles[:days + 1]]   # newest first
        closes.reverse()
        if len(closes) < 5:
            return None
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        return round(math.sqrt(var) * math.sqrt(365), 4)
    except Exception as e:
        log.warning(f"realized_vol {asset}: {e}")
        return None


async def evaluate(session, asset, strike, hours_left):
    """Returns (model_prob, spot, note) or None."""
    spot = await spot_price(session, asset)
    if spot is None:
        return None
    # Live realized vol is the correct input; hardcoded value is emergency fallback only.
    vol = await realized_vol(session, asset)
    vol_src = "realized"
    if vol is None or vol <= 0:
        vol = ANNUAL_VOL.get(asset, 0.6)
        vol_src = "fallback"
    p = prob_above(spot, strike, hours_left, vol)
    note = (f"spot=${spot:,.0f} strike=${strike:,.0f} {hours_left:.1f}h "
            f"vol={vol:.0%}({vol_src}) -> P(above)={p:.2%}")
    return round(p, 4), spot, note
