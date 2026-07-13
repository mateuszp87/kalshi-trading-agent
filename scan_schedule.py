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
