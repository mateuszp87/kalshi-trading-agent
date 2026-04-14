"""
Main agent orchestrator — ties together Kalshi API, signal fetchers, and Claude reasoner.
Runs scan loops, enforces risk limits, executes or simulates trades.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from dataclasses import dataclass, field

from config import AgentConfig
from kalshi_client import KalshiClient, KalshiMarket
from reasoner import ClaudeReasoner, TradeSignal
from fetchers import (
    fetch_sports_signals,
    fetch_politics_signals,
    fetch_econ_signals,
    fetch_entertainment_signals,
    fetch_crypto_signals,
    fetch_weather_signals,
)

log = logging.getLogger(__name__)


# Liquid Kalshi series tickers with real volume
LIQUID_SERIES = {
    "sports": [
        "KXNBA",        # NBA Finals — 13M volume
        "KXNBAEAST",    # NBA Eastern Conference — 4M volume
        "KXNBAWEST",    # NBA Western Conference — 4M volume
        "KXMLB",        # MLB Championship — 900k volume
        "KXMLBWINS-NYY", # Yankees wins — 10k volume
        "KXMLBWINS-LAD", # Dodgers wins — 21k volume
        "KXMLBWINS-BOS", # Red Sox wins — 18k volume
        "KXMLBWINS-HOU", # Astros wins
        "KXPGAR1LEAD",  # PGA Round 1 leader
        "KXGOLFH2H",    # Golf head to head
        "KXNHL",        # NHL
        "KXNHLSC",      # Stanley Cup
    ],
    "politics": [
        "KXTRUMPMENTION",      # Trump mentions
        "KXLEADERCOMBOOUT",    # Leader out this month
        "KXIMPEACHBOASBERG",   # Impeachment
        "KXAUSTRALIA",         # Australian election
        "KXSCOTUSRESIGN",      # SCOTUS resign
    ],
    "econ": [
        "KXCPIYOY",      # CPI YoY — 410 volume
        "KXCPI",         # CPI — 10k volume
        "KXHIGHNY",      # NYC temp — 3k volume
        "INXZ",          # S&P 500 up
        "LAYOFFSY",      # Layoffs
        "KXUSTYLD",      # Treasury yield
    ],
    "entertainment": [
        "KXPGAR1LEAD",   # PGA
        "KXTRUMPMENTION", # Mentions
        "BILLBOARDPEAKUS", # Billboard
        "KXLEADERMLBWINS", # MLB wins leader
    ],
    "crypto": [
        "KXBTCD",        # Bitcoin daily
        "KXETHD",        # Ethereum daily
        "BITCOINMAXY",   # Bitcoin min/max
    ],
    "weather": [
        "KXHIGHNY",      # NYC high temp — 3k volume
        "RAINNY",        # NYC rain
    ],
}

CATEGORY_CONFIG = {
    "sports": {
        "keywords": ["will", "Fed rate", "Bitcoin price", "recession", "unemployment", "election", "hurricane", "temperature"],
        "fetcher": fetch_sports_signals,
        "fetcher_key_arg": "api_key",
        "config_key": "espn_api_key",
    },
    "politics": {
        "keywords": ["Trump approve", "Senate majority", "government shutdown", "Supreme Court", "tariff", "executive order", "Congress pass", "veto", "recession 2026", "rate cut"],
        "fetcher": fetch_politics_signals,
        "fetcher_key_arg": "newsapi_key",
        "config_key": "newsapi_key",
    },
    "econ": {
        "keywords": ["Fed rate", "rate cut", "rate hike", "CPI above", "inflation above", "unemployment above", "GDP growth", "recession 2026", "FOMC decision", "interest rate"],
        "fetcher": fetch_econ_signals,
        "fetcher_key_arg": "fred_api_key",
        "config_key": "fred_api_key",
    },
    "entertainment": {
        "keywords": ["Oscar", "Grammy", "Emmy", "movie", "film", "album", "box office", "Netflix", "Taylor Swift", "award"],
        "fetcher": fetch_entertainment_signals,
        "fetcher_key_arg": "newsapi_key",
        "config_key": "newsapi_key",
    },
    "crypto": {
        "keywords": ["Bitcoin above", "Bitcoin below", "BTC price", "Ethereum above", "ETH price", "crypto ETF", "Bitcoin ETF", "Solana above", "crypto market cap", "altcoin season"],
        "fetcher": fetch_crypto_signals,
        "fetcher_key_arg": "coingecko_api_key",
        "config_key": "coingecko_api_key",
    },
    "weather": {
        "keywords": ["hurricane", "tornado", "storm", "snow", "blizzard", "flood", "temperature", "heat", "rainfall", "El Niño"],
        "fetcher": fetch_weather_signals,
        "fetcher_key_arg": "noaa_token",
        "config_key": "noaa_token",
    },
}


@dataclass
class SessionStats:
    trades_placed: int = 0
    trades_skipped: int = 0
    markets_scanned: int = 0
    daily_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    open_positions: int = 0          # live count of unresolved positions
    open_tickers: set = field(default_factory=set)  # prevent double-entry same market
    trade_log: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0


class KalshiTradingAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.stats = SessionStats()
        self.reasoner = ClaudeReasoner(config.anthropic_api_key, config.claude_model)
        self._running = True

    async def run(self):
        config = self.config
        try:
            config.validate()
        except EnvironmentError as e:
            log.error(str(e))
            return

        log.info("Starting Kalshi Trading Agent...")
        log.info(f"Categories: {config.active_categories}")

        async with KalshiClient(config.kalshi_api_key, config.kalshi_base_url, config.kalshi_private_key_path) as client:
            balance = await client.get_balance()
            log.info(f"Account balance: ${balance:.2f}")

            while self._running:
                try:
                    await self._scan_cycle(client)
                except KeyboardInterrupt:
                    log.info("Interrupted by user.")
                    break
                except Exception as e:
                    log.error(f"Scan cycle error: {e}", exc_info=True)

                self._print_session_stats()

                if abs(self.stats.daily_pnl) >= config.max_daily_loss:
                    log.warning(f"Daily loss limit hit (${self.stats.daily_pnl:.2f}). Stopping.")
                    break

                # Randomize interval between scan_interval_min and scan_interval_max
                wait = random.randint(config.scan_interval_min, config.scan_interval_max)
                log.info(f"Next scan in {wait//60}m {wait%60}s  (open positions: {self.stats.open_positions}/{config.max_open_positions})")
                await asyncio.sleep(wait)

    async def _scan_cycle(self, client: KalshiClient):
        log.info("=" * 60)
        log.info(f"SCAN CYCLE — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        log.info("=" * 60)

        for category in self.config.active_categories:
            cat_cfg = CATEGORY_CONFIG[category]
            log.info(f"\n[{category.upper()}] Fetching markets...")

            # Rotate through keywords for this category
            # Use liquid series tickers directly instead of keyword search
            series_list = LIQUID_SERIES.get(category, [])
            if series_list:
                markets = await client.get_series_markets(series_list, limit=10)
            else:
                keyword = cat_cfg["keywords"][self.stats.markets_scanned % len(cat_cfg["keywords"])]
                markets = await client.get_markets(keyword=keyword, limit=25)

            if not markets:
                log.info(f"  No markets found for keyword='{keyword}'")
                continue

            log.info(f"  Found {len(markets)} markets. Scoring top {min(3, len(markets))}...")

            for market in markets[:5]:
                self.stats.markets_scanned += 1
                await self._evaluate_market(client, market, category, cat_cfg)
                await asyncio.sleep(1)  # rate limit Claude calls

    async def _evaluate_market(
        self,
        client: KalshiClient,
        market: KalshiMarket,
        category: str,
        cat_cfg: dict,
    ):
        # ── Risk gate: max 10 open positions ──────────────────────────────────
        if self.stats.open_positions >= self.config.max_open_positions:
            log.info(f"  → SKIP — at position limit ({self.config.max_open_positions} open). Waiting for resolution.")
            self.stats.trades_skipped += 1
            return

        # ── Prevent re-entering same market ───────────────────────────────────
        if market.ticker in self.stats.open_tickers:
            log.info(f"  → SKIP — already holding position in {market.ticker}")
            return
        log.info(f"\n  Market: {market.title[:70]}")
        log.info(f"  Ticker: {market.ticker} | Mid: {market.mid_price:.2f} | Vol: {market.volume:,}")

        # Fetch signals from external APIs
        api_key_val = getattr(self.config, cat_cfg["config_key"], "")
        try:
            signals = await cat_cfg["fetcher"](market.title, **{cat_cfg["fetcher_key_arg"]: api_key_val})
        except Exception as e:
            log.warning(f"  Signal fetch error: {e}")
            signals = {}

        if signals:
            log.info(f"  Signals gathered: {list(signals.keys())}")
        else:
            log.warning("  No signals gathered — Claude will reason from market title only.")

        # Claude scores the market
        signal: TradeSignal = self.reasoner.score_market(market, signals, category)

        if signal is None:
            log.warning("  Claude scoring returned None.")
            return

        log.info(f"  Claude: estimated_prob={signal.estimated_prob:.2f} | edge={signal.edge:+.2f} | action={signal.action}")
        log.info(f"  Reasoning: {signal.reasoning}")

        # Enforce thresholds
        if signal.action == "skip":
            log.info("  → SKIP (Claude recommends no trade)")
            self.stats.trades_skipped += 1
            return

        if signal.action == "buy_yes" and market.mid_price >= self.config.buy_threshold:
            await self._execute_trade(client, market, "yes", signal)
        elif signal.action == "buy_no" and market.mid_price <= self.config.sell_threshold:
            await self._execute_trade(client, market, "no", signal)
        else:
            log.info(f"  → SKIP (market price {market.mid_price:.2f} doesn't meet threshold)")
            self.stats.trades_skipped += 1

    async def _execute_trade(
        self,
        client: KalshiClient,
        market: KalshiMarket,
        side: str,
        signal: TradeSignal,
    ):
        # Size contracts: each Kalshi contract = $0.01 max payout
        # We'll use limit orders priced at the current ask
        price_cents = int(market.yes_ask * 100) if side == "yes" else int(market.no_ask * 100)
        # # contracts = max_bet / cost_per_contract_in_dollars
        cost_per_contract = price_cents / 100
        count = max(1, int(self.config.max_bet_size / cost_per_contract))

        total_cost = round(cost_per_contract * count, 2)

        if self.config.dry_run:
            log.info(f"  [DRY RUN] Would BUY {side.upper()} | {market.ticker} | {count} contracts @ {price_cents}¢ | Total: ${total_cost:.2f}")
            self._log_trade(market, side, price_cents, count, total_cost, signal, "dry_run")
            self.stats.trades_placed += 1
            self.stats.open_positions += 1
            self.stats.open_tickers.add(market.ticker)
            return

        log.info(f"  → EXECUTING: BUY {side.upper()} | {market.ticker} | {count} contracts @ {price_cents}¢ | Total: ${total_cost:.2f}")
        result = await client.place_order(market.ticker, side, price_cents, count)

        if result:
            log.info(f"  ✓ Order placed: {result.order_id} | status={result.status}")
            self._log_trade(market, side, price_cents, count, total_cost, signal, result.status)
            self.stats.trades_placed += 1
            self.stats.open_positions += 1
            self.stats.open_tickers.add(market.ticker)
            self.stats.daily_pnl -= total_cost  # debit upfront; credit on resolution
        else:
            log.error("  ✗ Order failed.")

    def _log_trade(self, market, side, price_cents, count, cost, signal, status):
        self.stats.trade_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "ticker": market.ticker,
            "title": market.title[:60],
            "side": side,
            "price_cents": price_cents,
            "contracts": count,
            "cost_usd": cost,
            "estimated_prob": signal.estimated_prob,
            "edge": signal.edge,
            "confidence": signal.confidence,
            "reasoning": signal.reasoning[:120],
            "status": status,
        })

    def _print_session_stats(self):
        s = self.stats
        log.info("\n── Session Stats ──────────────────────────────")
        log.info(f"  Markets scanned : {s.markets_scanned}")
        log.info(f"  Trades placed   : {s.trades_placed}")
        log.info(f"  Trades skipped  : {s.trades_skipped}")
        log.info(f"  Open positions  : {s.open_positions}/{self.config.max_open_positions}")
        log.info(f"  Daily P&L       : ${s.daily_pnl:+.2f}")
        log.info("───────────────────────────────────────────────\n")

    def save_results(self):
        import json
        from datetime import date
        filename = f"results_{date.today()}.json"
        with open(filename, 'w') as f:
            json.dump(self.stats.trade_log, f, indent=2)
        log.info(f"Results saved to {filename}")
