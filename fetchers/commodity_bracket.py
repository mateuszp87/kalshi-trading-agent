"""Coherent bracket pricing for commodity strike ladders.

Prices an ENTIRE ladder of strikes on one underlying off a single lognormal
distribution (spot + realized vol + time-to-settle). Because every strike is
read off the same curve, P(above strike) is monotonic by construction: higher
strike -> lower probability, always. This eliminates the incoherent all-NO
ladder the per-strike LLM produced (it priced twelve strikes independently and
even non-monotonically).

Reuses prob_above from crypto_model (the lognormal digital), which is CORRECT
for commodities because they settle on a point-in-time price at a fixed hour,
unlike crypto KXBTCD's 60-second average.
"""
import re, logging
from fetchers.crypto_model import prob_above
from fetchers.commodity_spot import root_from_ticker

log = logging.getLogger(__name__)

# strike lives in the ticker suffix: KXWTI-26JUL1614-T79.49 -> 79.49
_STRIKE_RE = re.compile(r"-T(\d+(?:\.\d+)?)$")


def parse_strike(ticker: str):
    m = _STRIKE_RE.search(ticker.upper())
    return float(m.group(1)) if m else None


def ladder_key(ticker: str):
    """Group key = everything before the -T strike suffix.
    KXWTI-26JUL1614-T79.49 -> KXWTI-26JUL1614  (one underlying, one settle time)"""
    return _STRIKE_RE.sub("", ticker.upper())


def price_strike(spot, strike, hours_left, annual_vol):
    """P(settle > strike) for one strike off the shared lognormal curve."""
    return prob_above(spot, strike, hours_left, annual_vol)
