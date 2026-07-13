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
