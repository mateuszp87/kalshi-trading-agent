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
