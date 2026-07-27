#!/usr/bin/env bash
#
# install_upgrade.sh — writes the reference-edge upgrade into your repo and
# pushes to GitHub (which triggers your Render deploy).
#
# WHAT IT DOES NOT DO: touch your .env, keys, Render settings, or agent.py.
# Until the 3 agent.py edits in INTEGRATION.md are done, these modules sit in
# the repo UNUSED and change nothing at runtime. Pushing this alone is safe.
#
# USAGE:
#   cd ~/Desktop/kalshi_agent
#   bash install_upgrade.sh
#
set -euo pipefail

if [ ! -d .git ]; then
  echo "ERROR: run this from your kalshi_agent repo root (no .git found here)."
  exit 1
fi

echo "Writing modules into $(pwd) ..."

cat > edge_engine.py <<'PYEOF'
"""
edge_engine.py — reference-based edge scoring for the Kalshi agent.

CORE PRINCIPLE (read this before touching thresholds):
    An edge only exists when the market price disagrees with a NAMED, EXTERNAL,
    VERIFIABLE reference. "Claude thinks it's higher" is NOT a reference and must
    never produce a tradable edge. This is the fix for the discretionary-override
    problem: the settlement log shows systematic bets win and confident overrides
    lose. So we only bet mechanical, sourced gaps.

Each category has a reference function that returns an estimated TRUE probability
in [0,1], plus a confidence weight. Edge = ref_prob - market_prob. We bet only
when |edge| clears the category threshold AND confidence is high enough AND the
market resolves inside the horizon window.

Wire-in: call score_market_reference(market) where the old Claude-scoring call
was. If it returns None, the market is a PASS (no reference => no bet).
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import logging

log = logging.getLogger("edge_engine")


# ---------------------------------------------------------------------------
# The trader mandate. This is the "identity". It is a rule set, not a vibe.
# Every one of these is enforced in code below or in agent gating.
# ---------------------------------------------------------------------------
MANDATE = {
    "no_reference_no_trade": True,      # unsourced conviction is banned
    "max_bet_usd": 40,                  # matches the sizing that actually wins
    "no_override_multiplier": True,     # bot cannot scale up on "high confidence"
    "resolution_horizon_hours": 30,     # only near-term, data-informed markets
    "min_edge_by_category": {           # gap required between ref and market
        "weather": 0.12,
        "crypto":  0.15,
        "econ":    0.14,
        "politics":0.18,                # highest bar; least real edge
        "entertainment": 0.99,          # effectively disabled
    },
    "min_ref_confidence": 0.55,         # reject weak references
    "max_positions": 50,
    "max_new_trades_per_scan": 4,       # forces selectivity
}


@dataclass
class RefResult:
    ref_prob: float          # estimated true probability of YES
    confidence: float        # 0..1, how much we trust the reference
    source: str              # human-readable provenance (REQUIRED, for logging)
    category: str

    def edge_vs(self, market_yes_price: float) -> float:
        """Positive => YES underpriced (buy YES). Negative => buy NO."""
        return self.ref_prob - market_yes_price


# ---------------------------------------------------------------------------
# Reference sources. These are stubs wired to real endpoints you already have
# HTTP access to. Fill the fetch bodies with your existing http client.
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


async def crypto_reference(market, http) -> RefResult | None:
    """
    For same-day 'BTC/ETH above $X at close' style markets.
    Pure distance-to-strike vs realized vol. No opinion.
    """
    info = parse_crypto_market(market)
    if not info:
        return None
    spot = await fetch_spot(http, info["asset"])
    if spot is None:
        return None
    hours_left = info["hours_to_resolve"]
    if hours_left <= 0:
        return None
    # annualized vol -> horizon sigma. Use a rolling realized vol you already track,
    # fallback to a conservative constant.
    ann_vol = info.get("realized_vol", 0.6)
    sigma = ann_vol * math.sqrt(hours_left / (365 * 24))
    if sigma <= 0:
        return None
    # P(spot_T > strike) under lognormal-ish approx
    z = (math.log(spot / info["strike"])) / sigma
    prob_yes = _norm_cdf(z)
    # confidence scales down as horizon lengthens (vol estimate gets shakier)
    conf = max(0.4, 0.85 - 0.02 * hours_left)
    return RefResult(prob_yes, conf, f"spot={spot} strike={info['strike']} vol={ann_vol}", "crypto")


async def weather_reference(market, http) -> RefResult | None:
    """
    For KXHIGH* daily-high-temperature markets. Highest-quality edge you have:
    NWS forecast often updates before the Kalshi price moves. But note the crowd
    is already well-calibrated at the extremes, so real edge lives mid-range and
    in the late repricing window -- the thresholds reflect that.
    """
    info = parse_weather_market(market)
    if not info:
        return None
    fc = await fetch_nws_forecast(http, info["station"], info["date"])
    if not fc:
        return None
    # Treat forecast high as mean, spread as ~1 sigma of forecast error.
    sigma = max(1.5, fc["spread_F"])
    z = (fc["high_F"] - info["threshold_F"]) / sigma
    prob_yes = _norm_cdf(z)
    # NWS same-day forecasts are tight => higher confidence for short horizon
    conf = 0.7 if info["hours_to_resolve"] < 18 else 0.55
    return RefResult(prob_yes, conf, f"NWS high={fc['high_F']}F thr={info['threshold_F']}F", "weather")


async def econ_reference(market, http) -> RefResult | None:
    """
    Only fires inside a release window: the resolving data is published but the
    market may not have fully converged. Outside the window -> None (pass).
    You maintain a small calendar dict of {series: release_datetime, value_fetcher}.
    """
    info = parse_econ_market(market)
    if not info or not info.get("data_released"):
        return None
    published_value = info["published_value"]
    threshold = info["threshold"]
    prob_yes = 1.0 if published_value > threshold else 0.0
    # Data is published => near-certain. This is the cleanest edge type: the
    # market literally hasn't updated to a known number yet.
    return RefResult(prob_yes, 0.9, f"released={published_value} thr={threshold}", "econ")


async def politics_reference(market, http) -> RefResult | None:
    """
    Poll-aggregate gap only. If you don't have an aggregate, return None.
    Politics defaults to PASS. This category is a trap for LLM confidence.
    """
    info = parse_politics_market(market)
    if not info or "poll_prob" not in info:
        return None
    conf = min(0.65, 0.3 + info.get("n_polls", 0) * 0.05)
    return RefResult(info["poll_prob"], conf, f"pollavg={info['poll_prob']:.2f}", "politics")


REFERENCES = {
    "crypto": crypto_reference,
    "weather": weather_reference,
    "econ": econ_reference,
    "politics": politics_reference,
}


async def score_market_reference(market, http) -> RefResult | None:
    """Drop-in replacement for the old Claude fair-value scorer (now async)."""
    cat = _field(market, "category")
    fn = REFERENCES.get(cat)
    if not fn:
        return None                      # unsupported category => pass
    try:
        return await fn(market, http)
    except Exception as e:
        log.warning("reference failed for %s: %s", _ticker(market), e)
        return None


# ---------------------------------------------------------------------------
# The gate. This decides buy/sell/pass. This is where the identity is enforced.
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    action: str              # "buy_yes" | "buy_no" | "pass"
    size_usd: float
    edge: float
    reason: str


def decide(market, ref: RefResult | None, open_positions: int) -> Decision:
    yes_price = market["yes_price"]      # 0..1
    hrs = market.get("hours_to_resolve", 999)

    if ref is None:
        return Decision("pass", 0, 0.0, "no reference (unsourced => banned)")

    if hrs > MANDATE["resolution_horizon_hours"]:
        return Decision("pass", 0, 0.0, f"horizon {hrs:.0f}h > {MANDATE['resolution_horizon_hours']}h")

    if ref.confidence < MANDATE["min_ref_confidence"]:
        return Decision("pass", 0, 0.0, f"ref conf {ref.confidence:.2f} too low")

    edge = ref.edge_vs(yes_price)
    thr = MANDATE["min_edge_by_category"].get(ref.category, 0.99)
    if abs(edge) < thr:
        return Decision("pass", 0, edge, f"edge {edge:+.2f} < thr {thr:.2f}")

    if open_positions >= MANDATE["max_positions"]:
        return Decision("pass", 0, edge, "position cap reached")

    # Fixed size. No confidence multiplier. This is the whole point.
    size = MANDATE["max_bet_usd"]
    action = "buy_yes" if edge > 0 else "buy_no"
    return Decision(action, size, edge,
                    f"{ref.source} | edge {edge:+.2f} vs thr {thr:.2f}")


# ---------------------------------------------------------------------------
# Parsers + fetchers -- written against confirmed Kalshi ticker formats.
#
# Weather:  KXHIGHNY-26APR22-B59.5   series=city, date, bracket
# Crypto:   KXBTC-26MAR14-100000     series=asset, date, strike($)
# Econ:     KXFED-26MAR19            series, date  (threshold from title)
#
# NOTE: `m` here is your KalshiMarket dataclass (or a dict from it). Access is
# written to work with either: getattr first, then dict fallback.
# ---------------------------------------------------------------------------
import re

_MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
           "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

# City series prefix -> NWS settlement station (from Kalshi's settlement rules).
# Extend this as you trade more cities; unmapped city => weather ref returns None.
CITY_STATION = {
    "KXHIGHNY":  "KNYC",   # Central Park
    "KXHIGHCHI": "KMDW",   # Midway
    "KXHIGHLAX": "KLAX",
    "KXHIGHMIA": "KMIA",
    "KXHIGHDEN": "KDEN",
    "KXHIGHAUS": "KAUS",
    "KXHIGHPHIL":"KPHL",
    # KXHIGH<DAL> Dallas -> KDFW, Houston -> KHOU  (add exact prefixes from your logs)
}


def _field(m, name, default=None):
    if hasattr(m, name):
        return getattr(m, name)
    if isinstance(m, dict):
        return m.get(name, default)
    return default


def _ticker(m):
    return _field(m, "ticker", "") or ""


def _parse_date(datestr):
    """'26APR22' -> datetime date at 23:59 UTC (Kalshi daily close-ish)."""
    mobj = re.match(r"(\d{2})([A-Z]{3})(\d{2})", datestr)
    if not mobj:
        return None
    yy, mon, dd = mobj.groups()
    if mon not in _MONTHS:
        return None
    return datetime(2000 + int(yy), _MONTHS[mon], int(dd), 23, 59, tzinfo=timezone.utc)


def _hours_to_resolve(m):
    """Prefer the market's close_time; fall back to date parsed from ticker."""
    ct = _field(m, "close_time")
    dt = None
    if ct:
        try:
            dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        except Exception:
            dt = None
    if dt is None:
        parts = _ticker(m).split("-")
        if len(parts) >= 2:
            dt = _parse_date(parts[1])
    if dt is None:
        return 999.0
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0


def parse_weather_market(m):
    # KXHIGHNY-26APR22-B59.5   (bracket forms: B59.5 = above 59.5, T58 = the 58 bracket)
    t = _ticker(m)
    parts = t.split("-")
    if len(parts) < 3:
        return None
    series = parts[0]
    station = CITY_STATION.get(series)
    if not station:
        return None
    mnum = re.search(r"[BT]?(\d+(?:\.\d+)?)", parts[2])
    if not mnum:
        return None
    return {
        "station": station,
        "date": _parse_date(parts[1]),
        "threshold_F": float(mnum.group(1)),
        "hours_to_resolve": _hours_to_resolve(m),
    }


def parse_crypto_market(m):
    # KXBTC-26MAR14-100000   asset=BTC, strike=$100000
    t = _ticker(m)
    parts = t.split("-")
    if len(parts) < 3:
        return None
    asset = parts[0].replace("KX", "")           # BTC / ETH
    try:
        strike = float(parts[2])
    except ValueError:
        return None
    hrs = _hours_to_resolve(m)
    return {
        "asset": asset,
        "strike": strike,
        "hours_to_resolve": hrs,
        # realized_vol: plug in a rolling estimate you track; conservative default.
        "realized_vol": _field(m, "realized_vol", 0.6),
    }


def parse_econ_market(m):
    # KXFED-26MAR19 and similar. Threshold + released value come from a calendar
    # you maintain, not the ticker. Return None until you populate ECON_CALENDAR.
    t = _ticker(m)
    series = t.split("-")[0]
    cal = ECON_CALENDAR.get(series)
    if not cal:
        return None
    return {
        "data_released": cal.get("data_released", False),
        "published_value": cal.get("published_value"),
        "threshold": cal.get("threshold"),
    }


def parse_politics_market(m):
    # Poll aggregate must come from a source you maintain in POLL_AGGREGATE,
    # keyed by event_root/ticker. No aggregate => None => pass.
    key = _field(m, "event_ticker") or _ticker(m).split("-")[0]
    agg = POLL_AGGREGATE.get(key)
    if not agg:
        return None
    return {"poll_prob": agg["prob"], "n_polls": agg.get("n_polls", 0)}


# You maintain these two small dicts (or load from a file). Empty => those
# categories pass by default, which is the safe behavior.
ECON_CALENDAR: dict = {}
POLL_AGGREGATE: dict = {}


async def fetch_spot(http, asset):
    """Coinbase spot. http is your aiohttp session."""
    pair = {"BTC": "BTC-USD", "ETH": "ETH-USD"}.get(asset.upper())
    if not pair:
        return None
    url = f"https://api.coinbase.com/v2/prices/{pair}/spot"
    async with http.get(url) as r:
        if r.status != 200:
            return None
        data = await r.json()
        return float(data["data"]["amount"])


async def fetch_nws_forecast(http, station, date):
    """
    NWS forecast high for a station on a given date.
    Two-step: station -> gridpoint -> forecast. Cache the gridpoint per station.
    Returns {high_F, spread_F} or None.
    """
    if date is None:
        return None
    # 1) resolve station lat/lon (you likely already have a station->latlon map;
    #    hardcode the ~20 Kalshi cities to avoid an extra call).
    latlon = STATION_LATLON.get(station)
    if not latlon:
        return None
    lat, lon = latlon
    points_url = f"https://api.weather.gov/points/{lat},{lon}"
    async with http.get(points_url, headers={"User-Agent": "kalshi-agent"}) as r:
        if r.status != 200:
            return None
        grid = (await r.json())["properties"]["forecast"]
    async with http.get(grid, headers={"User-Agent": "kalshi-agent"}) as r:
        if r.status != 200:
            return None
        periods = (await r.json())["properties"]["periods"]
    target = date.date()
    for p in periods:
        start = datetime.fromisoformat(p["startTime"])
        if p["isDaytime"] and start.date() == target:
            return {"high_F": float(p["temperature"]), "spread_F": 3.0}
    return None


# Fill these ~20 entries from Kalshi's city list; only need cities you trade.
STATION_LATLON = {
    "KNYC": (40.78, -73.97),
    "KMDW": (41.79, -87.75),
    "KLAX": (33.94, -118.41),
    "KMIA": (25.79, -80.29),
    "KDEN": (39.85, -104.66),
}
PYEOF

cat > calibration.py <<'PYEOF'
"""
calibration.py — the part that makes it "win more, lose less" honestly.

You asked for higher accuracy (right vs wrong). The only way to actually get
that is to MEASURE calibration and let bad reference sources get demoted. A
quant desk lives or dies on this, not on conviction.

For every settled bet, log: category, reference source, ref_prob at entry,
market_prob at entry, and the realized outcome (0/1). Then:

  1. Per-category hit rate and Brier score.
  2. If a category's realized edge is negative over N>=20 bets, RAISE its
     threshold automatically (or disable it). This is the circuit breaker for
     a bad edge source, mirroring your equity circuit breaker.

This turns "trust me" into "the numbers earned the right to keep betting."
"""

from __future__ import annotations
import json, os, statistics
from collections import defaultdict

STORE = os.path.expanduser("~/kalshi_agent/calibration_log.jsonl")


def record_settled(category, source, ref_prob, market_prob, outcome, pnl):
    row = {
        "category": category, "source": source,
        "ref_prob": ref_prob, "market_prob": market_prob,
        "outcome": outcome, "pnl": pnl,
    }
    with open(STORE, "a") as f:
        f.write(json.dumps(row) + "\n")


def _load():
    if not os.path.exists(STORE):
        return []
    with open(STORE) as f:
        return [json.loads(l) for l in f if l.strip()]


def report():
    rows = _load()
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)

    out = {}
    for cat, rs in by_cat.items():
        n = len(rs)
        # Brier: how well-calibrated the REFERENCE was vs reality
        brier = statistics.mean((r["ref_prob"] - r["outcome"]) ** 2 for r in rs)
        # Did betting the ref-vs-market gap actually make money?
        realized_pnl = sum(r["pnl"] for r in rs)
        # Hit rate: did the side we picked resolve our way?
        hits = sum(1 for r in rs
                   if (r["ref_prob"] > r["market_prob"]) == (r["outcome"] == 1))
        out[cat] = {
            "n": n,
            "brier": round(brier, 4),
            "hit_rate": round(hits / n, 3) if n else None,
            "realized_pnl": round(realized_pnl, 2),
        }
    return out


def suggested_threshold_adjustments(mandate, min_n=20):
    """
    Auto-defense: if a category has enough settled bets and is losing money or
    poorly calibrated, tighten it. Returns dict of category -> new threshold.
    Apply these to MANDATE at startup.
    """
    rep = report()
    adj = {}
    for cat, s in rep.items():
        if s["n"] < min_n:
            continue
        cur = mandate["min_edge_by_category"].get(cat, 0.99)
        if s["realized_pnl"] < 0 or s["brier"] > 0.25:
            adj[cat] = min(0.99, round(cur + 0.04, 2))   # make it harder
        elif s["realized_pnl"] > 0 and s["brier"] < 0.18 and s["hit_rate"] > 0.6:
            adj[cat] = max(0.08, round(cur - 0.01, 2))   # earn a little slack
    return adj


if __name__ == "__main__":
    import pprint
    pprint.pprint(report())
PYEOF

cat > scan_schedule.py <<'PYEOF'
"""
scan_schedule.py — 1-2 disciplined scans/day instead of every 15-30 min.

Rationale: more scans != more edge. It means more marginal bets and more fee
drag. Two scans timed to when external data is freshest (post NWS forecast
update, post econ release windows) gives the reference the best chance of
leading the market. Fewer, better trades is the whole strategy.

Replace your randomized 15-30 min loop with this. Times are UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

# 13:00 UTC ~ after morning NWS forecast refresh + pre-US econ prints
# 21:00 UTC ~ late-day forecast + intraday crypto/econ convergence check
SCAN_HOURS_UTC = [13, 21]


def next_scan_time(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    todays = sorted(
        now.replace(hour=h, minute=0, second=0, microsecond=0)
        for h in SCAN_HOURS_UTC
    )
    for t in todays:
        if t > now:
            return t
    # all of today's passed -> first slot tomorrow
    return todays[0] + timedelta(days=1)


def eligible_market(market, horizon_hours: int) -> bool:
    """Only near-term markets where the reference data is actually informative."""
    hrs = market.get("hours_to_resolve", 999)
    return 0 < hrs <= horizon_hours


if __name__ == "__main__":
    print("next scan:", next_scan_time().isoformat())
PYEOF

cat > INTEGRATION.md <<'MDEOF'
# Wiring the upgrade into your agent

Three files, dropped next to `agent.py` and `kalshi_client.py`:
`edge_engine.py`, `calibration.py`, `scan_schedule.py`.

## What changed conceptually
Your old path: fetch market -> Claude scores a fair-value guess -> edge ->
threshold -> maybe trade. That path is where the losing discretionary-style
bets came from. The new path replaces Claude's guess with an EXTERNAL
reference (NWS forecast, crypto spot vs strike, published econ data, poll avg).
No reference => the bot passes. Unsourced conviction is banned in code.

## Step 1 — in your scan loop, replace the scorer

Find where you currently call Claude to score a market (near the
`score_market` / EVAL logging in agent.py). Replace with:

```python
from edge_engine import score_market_reference, decide, MANDATE
from scan_schedule import eligible_market

# inside the per-market loop:
if not eligible_market(market, MANDATE["resolution_horizon_hours"]):
    continue

ref = await score_market_reference(market, self.http)   # self.http = aiohttp session
d = decide(market, ref, open_positions=self.position_count)

log.info("EVAL %s | %s | edge=%+.2f | %s",
         market.ticker, d.action, d.edge, d.reason)

if d.action != "pass":
    # d.size_usd is FIXED at MANDATE['max_bet_usd']; no confidence multiplier
    await self.place_order(market, d.action, d.size_usd)
    new_trades += 1
    if new_trades >= MANDATE["max_new_trades_per_scan"]:
        break
```

## Step 2 — swap the loop cadence

Replace the randomized 15-30 min sleep with:

```python
from scan_schedule import next_scan_time
import asyncio
from datetime import datetime, timezone

while True:
    await self.run_scan()
    nxt = next_scan_time()
    sleep_s = (nxt - datetime.now(timezone.utc)).total_seconds()
    log.info("next scan at %s (%.0f min)", nxt.isoformat(), sleep_s/60)
    await asyncio.sleep(max(60, sleep_s))
```

## Step 3 — record settlements for calibration

When a position settles (in your settlement/exit handler), call:

```python
from calibration import record_settled
record_settled(
    category=pos.category,
    source=pos.ref_source,        # store ref.source at entry on the position
    ref_prob=pos.ref_prob,        # store ref.ref_prob at entry
    market_prob=pos.entry_price,  # yes price at entry
    outcome=1 if settled_yes else 0,
    pnl=realized_pnl,
)
```

You'll need to stash `ref.source` and `ref.ref_prob` on the position object at
entry. That's the only new state to persist.

## Step 4 — let calibration auto-tighten (optional but recommended)

At startup, after you build MANDATE:

```python
from calibration import suggested_threshold_adjustments
adj = suggested_threshold_adjustments(MANDATE, min_n=20)
MANDATE["min_edge_by_category"].update(adj)
log.info("calibration adjustments applied: %s", adj)
```

Any category losing money over 20+ settled bets gets a harder threshold
automatically. This is your edge-source circuit breaker.

## Step 5 — the data you must supply
- `STATION_LATLON` and `CITY_STATION` in edge_engine.py: add the ~20 Kalshi
  weather cities you actually trade. Unmapped city => weather passes (safe).
- `ECON_CALENDAR`: populate around release dates, or econ always passes.
- `POLL_AGGREGATE`: populate if you want politics bets, else it passes.
- `realized_vol` for crypto: wire your rolling vol estimate; default 0.6 is
  conservative and will make the bot bet less, not more.

## The one-line summary of the strategy
Fewer bets, all sourced, fixed size, and a tracker that kills any edge source
that stops working. That's the honest version of "sophisticated quant."
MDEOF

echo "Files written."
echo "Verifying modules import cleanly..."
python3 -c "import edge_engine, calibration, scan_schedule" || {
  echo "ERROR: import failed. NOT pushing. Fix the error above first."
  exit 1
}
echo "Imports OK."

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "Committing on branch: $BRANCH"
git add edge_engine.py calibration.py scan_schedule.py INTEGRATION.md
git commit -m "Add reference edge engine, calibration, scheduler (modules only; agent.py wiring pending)"
echo ""
echo "About to push to origin/$BRANCH — triggers your Render deploy."
echo "agent.py is unchanged, so live bot behavior will NOT change yet."
read -r -p "Push now? [y/N] " ok
if [ "$ok" = "y" ] || [ "$ok" = "Y" ]; then
  git push origin "$BRANCH"
  echo "Pushed. Render should pick it up shortly."
else
  echo "Skipped push. Commit saved locally; run 'git push' when ready."
fi
