"""
Kalshi Trading Agent — game-first with auto exit.

Sports priority:
  1. NBA/NHL/MLB/UCL individual game winners     (same day)
  2. NBA half/quarter winners & player props     (same day)
  3. NHL/NBA series winners & spreads            (this week)
  4. MLB game totals, soccer league games        (same day/week)
  5. Daily crypto + weather                      (same day)
  6. Long-term econ/politics as fallback only

Exit rules (every scan cycle):
  TAKE PROFIT  — close position if +15 cents in your favor
  STOP LOSS    — close position if -20 cents against you
"""

import asyncio, json, logging, random
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from config import AgentConfig
from kalshi_client import KalshiClient, KalshiMarket
from reasoner import ClaudeReasoner, TradeSignal
from fetchers import (
    fetch_sports_signals, fetch_politics_signals, fetch_econ_signals,
    fetch_entertainment_signals, fetch_crypto_signals, fetch_weather_signals,
)

log = logging.getLogger(__name__)

# ── Exit thresholds ───────────────────────────────────────────
TAKE_PROFIT = 0.15   # +15 cents = close and lock profit
STOP_LOSS   = 0.20   # -20 cents = cut the loss

# ── Series tickers — ordered by priority ─────────────────────
GAME_SERIES = {
    "sports": [
        # Individual game winners — resolve same day
        "KXNBAGAME",        # NBA game winner        5.8M vol
        "KXNHLGAME",        # NHL game winner         14k vol
        "KXUCLGAME",        # Champions League        3.7M vol
        "KXMLBGAME",        # MLB game winner         daily
        "KXEPLGAME",        # Premier League          1.4k vol
        "KXSERIEAGAME",     # Serie A                 1.8k vol
        "KXLALIGAGAME",     # La Liga                 356 vol
        "KXBUNDESLIGAGAME", # Bundesliga              daily
        "KXMLSGAME",        # MLS                     daily
        # In-game markets — same day
        "KXNBA1HWINNER",    # NBA 1st half            82k vol
        "KXNBA2HWINNER",    # NBA 2nd half            479 vol
        "KXNBA1QWINNER",    # NBA quarters            daily
        "KXNBA2QWINNER",
        "KXNBA3QWINNER",
        "KXNBA4QWINNER",
        "KXMLBF5",          # MLB first 5 innings     daily
        # Player props
        "KXNBAPLAYOFFPTS",  # NBA playoff player pts
        # Series (week-long)
        "KXNHLSERIES",      # NHL playoff series      6.4k vol
        "KXNBASERIESSPREAD",# NBA series spread       265 vol
        "KXNBASERIESGAMES", # NBA series games        301 vol
    ],
    "crypto": [
        "KXBTCD",           # Bitcoin daily           TODAY
        "KXETHD",           # Ethereum daily          TODAY
    ],
    "weather": [
        "KXHIGHNY",         # NYC high temp           TODAY
        "RAINNY",           # NYC rain                TODAY
    ],
    "econ": [
        "INXZ",             # S&P 500 daily           TODAY
        "KXCPIYOY",         # CPI YoY
        "KXCPI",            # CPI monthly
    ],
    "politics": [
        "KXTRUMPMENTION",   # Trump interview         daily
        "KXSCOTUSRESIGN",   # SCOTUS resign
    ],
    "entertainment": [
        "KXPGAR1LEAD",      # PGA round 1             weekly
    ],
}

# Season win totals — fallback only (skip if games available)
SEASON_SERIES = [
    "KXNBA", "KXNBAEAST", "KXNBAWEST",
    "KXMLB", "KXMLBWINS-LAD", "KXMLBWINS-NYY", "KXMLBWINS-BOS",
]

CATEGORY_CONFIG = {
    "sports":        {"fetcher": fetch_sports_signals,        "key_arg": "api_key",          "cfg": "espn_api_key"},
    "politics":      {"fetcher": fetch_politics_signals,      "key_arg": "newsapi_key",       "cfg": "newsapi_key"},
    "econ":          {"fetcher": fetch_econ_signals,          "key_arg": "fred_api_key",      "cfg": "fred_api_key"},
    "entertainment": {"fetcher": fetch_entertainment_signals, "key_arg": "newsapi_key",       "cfg": "newsapi_key"},
    "crypto":        {"fetcher": fetch_crypto_signals,        "key_arg": "coingecko_api_key", "cfg": "coingecko_api_key"},
    "weather":       {"fetcher": fetch_weather_signals,       "key_arg": "noaa_token",        "cfg": "noaa_token"},
}


def priority_score(m: KalshiMarket) -> float:
    """Higher = scan first. Game markets closing today rank highest."""
    h = m.hours_until_close
    vol = m.volume or 0
    if h is None:   base = 5
    elif h <= 6:    base = 2000
    elif h <= 12:   base = 1600
    elif h <= 24:   base = 1300
    elif h <= 48:   base = 1000
    elif h <= 168:  base = 500
    elif h <= 720:  base = 100
    else:           base = 20
    return base + min(vol / 1000, 400)


def is_game(title: str) -> bool:
    """True if this is an individual game market (not a season future)."""
    t = title.lower()
    game_words = [" at ", " vs ", "winner?", "first half", "second half",
                  "quarter winner", "first 5 innings", "series winner",
                  "series spread", "total games", "points tonight"]
    season_words = ["win at least", "win the 2026", "win the season",
                    "championship?", "conference champion", "division winner",
                    "lead pro baseball", "lead pro basketball"]
    return any(w in t for w in game_words) and not any(w in t for w in season_words)


def edge_threshold(m: KalshiMarket) -> float:
    """Minimum edge required to place a trade based on timeframe."""
    h = m.hours_until_close
    if h is None:       return 0.12
    if h <= 24:         return 0.04   # same-day game: 4 cents
    if h <= 168:        return 0.06   # this week: 6 cents
    if h <= 720:        return 0.08   # this month: 8 cents
    return 0.12                       # long-term: 12 cents


@dataclass
class Position:
    ticker: str
    title: str
    side: str           # "yes" or "no"
    entry_price: float
    contracts: int
    cost: float
    entry_time: str
    category: str
    timeframe: str
    is_game: bool = False


@dataclass
class Stats:
    placed: int = 0
    exited: int = 0
    skipped: int = 0
    scanned: int = 0
    realized_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    positions: dict = field(default_factory=dict)  # ticker → Position

    @property
    def count(self): return len(self.positions)
    @property
    def win_rate(self):
        t = self.wins + self.losses
        return self.wins / t if t else 0.0


class KalshiTradingAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.stats = Stats()
        self.reasoner = ClaudeReasoner(config.anthropic_api_key, config.claude_model)
        self._pnl = self._load_pnl()

    def _load_pnl(self):
        try:
            with open("pnl_log.json") as f:
                return json.load(f)
        except:
            return {"all_time_pnl": 0.0, "wins": 0, "losses": 0,
                    "trades": [], "known_ids": []}

    def _save_pnl(self):
        try:
            with open("pnl_log.json", "w") as f:
                json.dump(self._pnl, f, indent=2)
        except Exception as e:
            log.warning(f"pnl save failed: {e}")

    async def run(self):
        try:
            self.config.validate()
        except EnvironmentError as e:
            log.error(str(e)); return

        mode = "DRY RUN" if self.config.dry_run else "LIVE TRADING"
        log.info(f"Kalshi Agent | {mode} | game markets first | exit ±{int(TAKE_PROFIT*100)}¢/±{int(STOP_LOSS*100)}¢")

        async with KalshiClient(
            self.config.kalshi_api_key,
            self.config.kalshi_base_url,
            self.config.kalshi_private_key_path,
        ) as client:
            bal = await client.get_balance()
            log.info(f"Balance: ${bal:.2f}")

            while True:
                try:
                    await self._process_settlements(client)
                    await self._manage_exits(client)
                    if self.stats.count < self.config.max_open_positions:
                        await self._scan(client)
                    else:
                        log.info(f"  Full ({self.stats.count}/{self.config.max_open_positions}) — exits only")
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    log.error(f"Cycle error: {e}", exc_info=True)

                self._print_stats()
                if self.stats.realized_pnl <= -self.config.max_daily_loss:
                    log.warning("Daily loss limit — stopping."); break

                wait = random.randint(self.config.scan_interval_min, self.config.scan_interval_max)
                log.info(f"Next scan in {wait//60}m {wait%60}s | positions: {self.stats.count}/{self.config.max_open_positions}")
                await asyncio.sleep(wait)

    # ── Settlements ──────────────────────────────────────────
    async def _process_settlements(self, client):
        try:
            known = set(self._pnl.get("known_ids", []))
            new = 0
            for s in await client.get_settlements():
                sid = s.get("id") or s.get("market_ticker") or ""
                if not sid or sid in known:
                    continue
                rev  = float(s.get("revenue", 0)) / 100
                cost = float(s.get("cost", 0)) / 100
                pnl  = round(rev - cost, 2)
                won  = pnl > 0
                ticker = s.get("market_ticker", "")
                self._pnl["all_time_pnl"] = round(self._pnl["all_time_pnl"] + pnl, 2)
                self._pnl["wins" if won else "losses"] += 1
                self._pnl["trades"].append({
                    "id": sid, "ticker": ticker, "type": "settlement",
                    "pnl": pnl, "won": won,
                    "time": s.get("created_time", datetime.now(timezone.utc).isoformat()),
                })
                known.add(sid)
                self.stats.realized_pnl += pnl
                if won: self.stats.wins += 1
                else:   self.stats.losses += 1
                log.info(f"  {'✅ WIN' if won else '❌ LOSS'} SETTLED: {ticker} | P&L ${pnl:+.2f}")
                if ticker in self.stats.positions:
                    del self.stats.positions[ticker]
                new += 1
            self._pnl["known_ids"] = list(known)
            if new:
                self._save_pnl()
                log.info(f"  {new} settled. All-time P&L: ${self._pnl['all_time_pnl']:+.2f}")
        except Exception as e:
            log.warning(f"Settlement check error: {e}")

    # ── Exit management — take profit / stop loss ─────────────
    async def _manage_exits(self, client):
        if not self.stats.positions:
            return
        tickers = list(self.stats.positions.keys())
        log.info(f"\n[EXIT CHECK] {len(tickers)} open positions")

        for ticker in tickers:
            pos = self.stats.positions.get(ticker)
            if not pos:
                continue
            mkt = await client.get_market(ticker)
            if not mkt:
                continue

            yes_bid = mkt.get("yes_bid", 0)
            yes_ask = mkt.get("yes_ask", 0)

            # How much has our position moved?
            if pos.side == "yes":
                current = yes_bid          # sell YES → get bid
                move = current - pos.entry_price
            else:
                current = round(1 - yes_ask, 4) if yes_ask else 0
                move = current - pos.entry_price

            unreal = round(move * pos.contracts, 2)
            log.info(f"  {ticker[:38]} {pos.side.upper()} {pos.contracts}x | entry={pos.entry_price:.2f} now={current:.2f} | move={move:+.2f} | unrealized=${unreal:+.2f}")

            reason = ""
            if move >= TAKE_PROFIT:
                reason = f"TAKE PROFIT +{move:.2f}"
            elif move <= -STOP_LOSS:
                reason = f"STOP LOSS {move:.2f}"

            if not reason:
                continue

            proceeds = round(current * pos.contracts, 2)
            pnl = round(proceeds - pos.cost, 2)
            log.info(f"  → EXIT [{reason}] | {pos.contracts}x @ {current:.2f} | P&L ${pnl:+.2f}")

            if not self.config.dry_run:
                result = await client.sell_position(ticker, pos.side, pos.contracts, current)
                if not result:
                    log.warning(f"  Sell failed for {ticker}")
                    continue
            else:
                log.info(f"  [DRY RUN] Would SELL {pos.side.upper()} {pos.contracts}x {ticker} @ {current:.2f}")

            self.stats.realized_pnl += pnl
            self.stats.exited += 1
            if pnl > 0: self.stats.wins += 1
            else:       self.stats.losses += 1

            self._pnl["all_time_pnl"] = round(self._pnl["all_time_pnl"] + pnl, 2)
            self._pnl["wins" if pnl > 0 else "losses"] += 1
            self._pnl["trades"].append({
                "ticker": ticker, "type": "early_exit",
                "side": pos.side, "contracts": pos.contracts,
                "entry": pos.entry_price, "exit": current,
                "pnl": pnl, "reason": reason,
                "time": datetime.now(timezone.utc).isoformat(),
            })
            self._save_pnl()
            del self.stats.positions[ticker]

    # ── Market scan ──────────────────────────────────────────
    async def _scan(self, client):
        log.info("=" * 60)
        log.info(f"SCAN — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        log.info("=" * 60)

        for category in self.config.active_categories:
            cat_cfg = CATEGORY_CONFIG[category]
            log.info(f"\n[{category.upper()}]")

            series = GAME_SERIES.get(category, [])
            markets = await client.get_series_markets(series, limit=10)

            # Sports: prioritize game markets, append season markets as fallback
            if category == "sports":
                games = [m for m in markets if is_game(m.title)]
                others = [m for m in markets if not is_game(m.title)]
                if not games:
                    log.info("  No priced games — adding season fallback...")
                    fallback = await client.get_series_markets(SEASON_SERIES, limit=5)
                    markets = others + fallback
                else:
                    markets = games + others

            if not markets:
                log.info("  No markets."); continue

            priced = [m for m in markets if m.yes_bid > 0 or m.yes_ask > 0]
            if not priced:
                log.info(f"  {len(markets)} found, none priced yet."); continue

            priced.sort(key=priority_score, reverse=True)
            game_ct = sum(1 for m in priced if is_game(m.title))
            today_ct = sum(1 for m in priced if m.hours_until_close is not None and m.hours_until_close <= 24)
            log.info(f"  {len(priced)} priced | {game_ct} games | {today_ct} today | scoring top 5...")

            for market in priced[:5]:
                if self.stats.count >= self.config.max_open_positions:
                    log.info("  Position limit — skipping rest"); break
                self.stats.scanned += 1
                await self._evaluate(client, market, category, cat_cfg)
                await asyncio.sleep(1)

    async def _evaluate(self, client, market: KalshiMarket, category: str, cat_cfg: dict):
        if market.ticker in self.stats.positions:
            log.info(f"  → SKIP already holding {market.ticker}")
            return

        game_flag = is_game(market.title)
        log.info(f"\n  {'🏀' if game_flag else '📊'} {market.title[:70]}")
        log.info(f"  {market.ticker} | mid={market.mid_price:.2f} | vol={market.volume:,} | [{market.timeframe_label}]")

        api_val = getattr(self.config, cat_cfg["cfg"], "")
        try:
            signals = await cat_cfg["fetcher"](market.title, **{cat_cfg["key_arg"]: api_val})
        except Exception as e:
            log.warning(f"  Signal error: {e}")
            signals = {}

        log.info(f"  Signals: {list(signals.keys()) if signals else 'none — reasoning from title'}")

        signal = self.reasoner.score_market(market, signals, category)
        if not signal:
            log.warning("  Claude returned None"); return

        log.info(f"  Claude: prob={signal.estimated_prob:.2f} edge={signal.edge:+.2f} action={signal.action} conf={signal.confidence:.0%}")
        log.info(f"  {signal.reasoning}")

        if signal.action == "skip":
            log.info("  → SKIP"); self.stats.skipped += 1; return

        thresh = edge_threshold(market)
        if signal.action == "buy_yes" and signal.edge >= thresh:
            await self._place(client, market, "yes", signal, category)
        elif signal.action == "buy_no" and signal.edge <= -thresh:
            await self._place(client, market, "no", signal, category)
        else:
            log.info(f"  → SKIP edge {signal.edge:+.2f} < {thresh:.2f} for {market.timeframe_label}")
            self.stats.skipped += 1

    async def _place(self, client, market: KalshiMarket, side: str, signal: TradeSignal, category: str):
        c = signal.confidence
        if c >= 0.85:   frac, tier = 1.00, "HIGH"
        elif c >= 0.70: frac, tier = 0.70, "MEDIUM"
        elif c >= 0.55: frac, tier = 0.50, "LOW"
        else:           frac, tier = 0.30, "VERY LOW"

        bet = round(self.config.max_bet_size * frac, 2)
        price = (market.yes_ask if side == "yes" else market.no_ask)
        if price <= 0:
            price = (market.mid_price if side == "yes" else round(1 - market.mid_price, 4))
        if price <= 0:
            log.warning("  No valid price"); return

        count = max(1, int(bet / price))
        cost  = round(price * count, 2)
        game_flag = is_game(market.title)

        log.info(f"  Confidence {c:.0%} ({tier}) → ${bet:.2f}")
        tag = "GAME ✓" if game_flag else "FUTURES"

        if self.config.dry_run:
            log.info(f"  [DRY RUN] BUY {side.upper()} {market.ticker} | {count}x @ {price:.0%} | ${cost:.2f} [{tag}]")
        else:
            result = await client.place_order(market.ticker, side, price, count)
            if not result:
                log.error("  Order failed"); return
            log.info(f"  ✓ BUY {side.upper()} {market.ticker} | {count}x @ {price:.0%} | ${cost:.2f} [{tag}] | id={result.order_id}")

        pos = Position(
            ticker=market.ticker, title=market.title[:80],
            side=side, entry_price=price, contracts=count, cost=cost,
            entry_time=datetime.now(timezone.utc).isoformat(),
            category=category, timeframe=market.timeframe_label,
            is_game=game_flag,
        )
        self.stats.positions[market.ticker] = pos
        self.stats.placed += 1
        self._save_trade_log()

    def _save_trade_log(self):
        try:
            with open("trade_log.json", "w") as f:
                json.dump({
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                    "session": {
                        "scanned": self.stats.scanned,
                        "placed": self.stats.placed,
                        "exited": self.stats.exited,
                        "skipped": self.stats.skipped,
                        "positions": self.stats.count,
                        "realized_pnl": round(self.stats.realized_pnl, 2),
                        "win_rate": round(self.stats.win_rate, 3),
                    },
                    "open_positions": {
                        t: {"title": p.title, "side": p.side, "entry": p.entry_price,
                            "contracts": p.contracts, "cost": p.cost,
                            "timeframe": p.timeframe, "is_game": p.is_game}
                        for t, p in self.stats.positions.items()
                    },
                    "all_time_pnl": self._pnl.get("all_time_pnl", 0),
                }, f, indent=2)
        except Exception as e:
            log.warning(f"trade_log save failed: {e}")

    def _print_stats(self):
        s = self.stats
        games = sum(1 for p in s.positions.values() if p.is_game)
        log.info("\n── Stats ──────────────────────────────────────")
        log.info(f"  Scanned {s.scanned} | Placed {s.placed} | Exited {s.exited} | Skipped {s.skipped}")
        log.info(f"  Positions: {s.count}/{self.config.max_open_positions} ({games} games)")
        log.info(f"  Session P&L: ${s.realized_pnl:+.2f} | All-time: ${self._pnl.get('all_time_pnl',0):+.2f}")
        log.info(f"  Win rate: {s.win_rate:.0%} ({s.wins}W / {s.losses}L)")
        log.info("──────────────────────────────────────────────\n")
