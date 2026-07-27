#!/usr/bin/env python3
"""
scorecard.py — per-category profit ranking for the Kalshi agent.

Reuses the dashboard's OWN gather() output, so numbers match the dashboard
exactly. Read-only: never touches the bot, never places or changes anything.

Run:  python3 scorecard.py
"""
import asyncio
import dashboard


def verdict(n, actual, claimed, pnl):
    if n < 10:
        return "too few"
    gap = actual - claimed
    if pnl > 0 and gap >= -3:
        return "WINNING"
    if pnl < 0:
        return "BLEEDING"
    return "flat"


async def main():
    data = await dashboard.gather()
    cats = data.get("calibration", {}).get("by_category", [])

    if not cats:
        print("No settled trades with recorded signals yet.")
        print("The bot records a signal per bet; a category shows up here once")
        print("those bets SETTLE (usually a few days). Check back later.")
        return

    ranked = sorted(cats, key=lambda r: r["pnl"], reverse=True)

    print("=" * 74)
    print("  KALSHI AGENT — CATEGORY SCORECARD")
    print("=" * 74)
    print(f"  {'CATEGORY':<10}{'TRADES':>7}{'WON%':>7}{'CLAIMED%':>10}{'GAP':>7}{'NET P&L':>11}   VERDICT")
    print("  " + "-" * 70)
    total = 0.0
    for r in ranked:
        gap = r["actual"] - r["claimed"]
        total += r["pnl"]
        print(f"  {r['key']:<10}{r['n']:>7}{r['actual']:>7.0f}{r['claimed']:>10.0f}"
              f"{gap:>+7.0f}{r['pnl']:>+11.2f}   {verdict(r['n'], r['actual'], r['claimed'], r['pnl'])}")
    print("  " + "-" * 70)
    print(f"  {'TOTAL':<10}{'':>24}{total:>+24.2f}")
    print("=" * 74)
    print()
    print("  GAP = actual win% minus claimed confidence%. Negative = overconfident.")
    print("  <10 settled trades = 'too few' to trust. Wait for ~30-50 to act.")
    print("  BLEEDING categories are candidates to cap harder or bench.")


if __name__ == "__main__":
    asyncio.run(main())
