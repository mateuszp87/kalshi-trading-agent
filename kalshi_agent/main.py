#!/usr/bin/env python3
"""
Kalshi Multi-Category Trading Agent
Powered by Claude AI reasoning + real external data sources
"""

import asyncio
import argparse
import logging
from config import AgentConfig
from agent import KalshiTradingAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

BANNER = """
╔═══════════════════════════════════════════════════════╗
║         Kalshi Multi-Category Trading Agent           ║
║         Powered by Claude AI + Real Data APIs         ║
╚═══════════════════════════════════════════════════════╝
"""


def parse_args():
    parser = argparse.ArgumentParser(description="Kalshi AI Trading Agent")
    parser.add_argument(
        "--category",
        choices=["sports", "politics", "econ", "entertainment", "crypto", "weather", "all"],
        default="all",
        help="Market category to trade",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate trades without executing")
    parser.add_argument("--interval-min", type=int, default=900, help="Min scan interval seconds (default 900 = 15 min)")
    parser.add_argument("--interval-max", type=int, default=1800, help="Max scan interval seconds (default 1800 = 30 min)")
    parser.add_argument("--max-bet", type=float, default=10.0, help="Max bet size in dollars (default $10)")
    parser.add_argument("--max-positions", type=int, default=10, help="Max simultaneous open positions (default 10)")
    parser.add_argument("--buy-threshold", type=float, default=0.72, help="Signal threshold to buy YES")
    parser.add_argument("--sell-threshold", type=float, default=0.28, help="Signal threshold to buy NO")
    parser.add_argument("--max-daily-loss", type=float, default=100.0, help="Stop trading if daily loss exceeds this")
    return parser.parse_args()


async def main():
    print(BANNER)
    args = parse_args()

    config = AgentConfig(
        category=args.category,
        dry_run=args.dry_run,
        scan_interval_min=args.interval_min,
        scan_interval_max=args.interval_max,
        max_bet_size=args.max_bet,
        max_open_positions=args.max_positions,
        buy_threshold=args.buy_threshold,
        sell_threshold=args.sell_threshold,
        max_daily_loss=args.max_daily_loss,
    )

    print(f"  Category      : {config.category.upper()}")
    print(f"  Mode          : {'DRY RUN (no real trades)' if config.dry_run else 'LIVE TRADING'}")
    print(f"  Scan interval : {config.scan_interval_min//60}–{config.scan_interval_max//60} min (randomized)")
    print(f"  Max bet       : ${config.max_bet_size}")
    print(f"  Max positions : {config.max_open_positions}")
    print(f"  Buy @ ≥       : {int(config.buy_threshold * 100)}%  |  Sell @ ≤ {int(config.sell_threshold * 100)}%")
    print()

    agent = KalshiTradingAgent(config)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
