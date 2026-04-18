"""
Kalshi Trading Agent — profit-first.

Targets markets in order of expected profit:
1. NBA playoff series (9M+ volume, 1c spreads) — best liquidity
2. UCL Champions League semis (1.8M volume, 1c spreads)
3. NHL playoff series (tight spreads, daily updates)
4. NBA in-game halves/quarters (same-day resolution)
5. MLB game winners (daily)
6. Daily crypto/weather (same-day, knowable outcomes)
7. Econ/politics as fallback only

Exit rules (checked every scan):
  TAKE PROFIT +12c  — lock gains quickly, free slot for next bet
  STOP LOSS   -15c  — cut fast, preserve capital
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

TAKE_PROFIT = 0.12   # tighter — lock gains fast
STOP_LOSS   = 0.15   # tighter — cut losses fast

# ── Ordered by profitability (volume × liquidity) ─────────────
PRIORITY_SERIES = [
    # NBA playoff series — 9M+ volume, 1c spreads, series ongoing
    "KXNBAGAME",        # NBA series game winner    (highest volume)
    "KXNBA1HWINNER",    # NBA half winner           (82k vol, same-day)
    "KXNBA2HWINNER",    # NBA 2nd half winner
    "KXNBA1QWINNER",    # NBA quarter winners
    "KXNBA2QWINNER",
    "KXNBA3QWINNER",
    "KXNBA4QWINNER",
    "KXNBASERIESSPREAD",# NBA series game spread
    "KXNBASERIESGAMES", # NBA series total games
    "KXNBAPLAYOFFPTS",  # NBA player points props
    # Champions League semis — 1.8M volume, 1c spreads
    "KXUCLGAME",
    # NHL playoff series — good volume, daily updates
    "KXNHLGAME",
    "KXNHLSERIES",
    # MLB game winners
    "KXMLBGAME",
    "KXMLBF5",          # MLB first 5 innings
    # Soccer leagues
    "KXEPLGAME",
    "KXSERIEAGAME",
    "KXLALIGAGAME",
    "KXBUNDESLIGAGAME",
    "KXMLSGAME",
]

DAILY_SERIES = [
    "KXBTCD", "KXETHD",   # crypto — resolves today
    "KXHIGHNY", "RAINNY", # weather — resolves today
    "INXZ",               # S&P 500 — resolves today
]

FALLBACK_SERIES = [
    "KXCPIYOY", "KXCPI", "KXTRUMPMENTION",
    "KXSCOTUSRESIGN", "KXPGAR1LEAD",
]

CATEGORY_CONFIG = {
    "sports":   {"fetcher": fetch_sports_signals,   "key_arg": "api_key",          "cfg": "espn_api_key"},
    "crypto":   {"fetcher": fetch_crypto_signals,   "key_arg": "coingecko_api_key","cfg": "coingecko_api_key"},
    "weather":  {"fetcher": fetch_weather_signals,  "key_arg": "noaa_token",       "cfg": "noaa_token"},
    "econ":     {"fetcher": fetch_econ_signals,     "key_arg": "fred_api_key",     "cfg": "fred_api_key"},
    "politics": {"fetcher": fetch_politics_signals, "key_arg": "newsapi_key",      "cfg": "newsapi_key"},
    "entertainment": {"fetcher": fetch_entertainment_signals, "key_arg": "newsapi_key", "cfg": "newsapi_key"},
}


def profit_score(m: KalshiMarket) -> float:
    """Score each market by expected profit potential.
    Higher volume + tighter spread + sooner close = better score."""
    vol    = m.volume or 0
    spread = max((m.yes_ask - m.yes_bid) if m.yes_bid and m.yes_ask else 0.5, 0.001)
    h      = m.hours_until_close or 9999
    # Reward: volume (liquidity), tight spread (real market), urgency
    vol_score    = min(vol / 10000, 1000)
    spread_score = 1.0 / spread          # tighter = better
    time_score   = max(1000 / h, 1)     # sooner = better, but don't over-penalize long ones
    return vol_score * spread_score * time_score


def event_root(ticker: str) -> str:
    """Extract event ticker (the game itself), stripping the outcome suffix.
    KXNBAGAME-26APR17GSWPHX-GSW → KXNBAGAME-26APR17GSWPHX
    KXNBAGAME-26APR17GSWPHX-PHX → KXNBAGAME-26APR17GSWPHX (same event!)
    """
    parts = ticker.rsplit("-", 1)
    return parts[0] if len(parts) > 1 else ticker


def is_game(title: str) -> bool:
    t = title.lower()
    game = [" at ", " vs ", "winner?", "first half", "second half",
            "quarter winner", "first 5 innings", "series winner",
            "series spread", "total games"]
    season = ["win at least", "win the 2026", "conference champion",
              "division winner", "lead pro baseball", "lead pro basketball"]
    return any(w in t for w in game) and not any(w in t for w in season)


def edge_threshold(m: KalshiMarket) -> float:
    """Minimum edge to trade. Strict — aiming for 70%+ win rate.
    HEAVY FAVORITES (75c+) need extra-large edge because sharp money already hunted them."""
    spread = (m.yes_ask - m.yes_bid) if m.yes_bid and m.yes_ask else 0.5
    vol    = m.volume or 0
    mid    = m.mid_price

    # Heavy favorite/underdog territory: market is usually right
    # Only trade if edge is HUGE (15c+)
    if mid >= 0.75 or mid <= 0.25:
        if vol > 100000: return 0.15  # liquid heavy fave — need huge edge
        return 0.12

    # Mid-range markets (25-75c): normal thresholds
    if spread <= 0.02 and vol > 100000: return 0.08
    if spread <= 0.05 and vol > 10000:  return 0.10
    if spread <= 0.10 and vol > 1000:   return 0.13
    return 0.18


@dataclass
class Position:
    ticker: str
    title: str
    side: str
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
    positions: dict = field(default_factory=dict)

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
            with open("pnl_log.json") as f: return json.load(f)
        except:
            return {"all_time_pnl": 0.0, "wins": 0, "losses": 0,
                    "trades": [], "known_ids": []}

    def _save_pnl(self):
        try:
            with open("pnl_log.json", "w") as f: json.dump(self._pnl, f, indent=2)
        except Exception as e: log.warning(f"pnl save: {e}")

    async def run(self):
        try: self.config.validate()
        except EnvironmentError as e: log.error(str(e)); return

        mode = "DRY RUN" if self.config.dry_run else "LIVE TRADING"
        log.info(f"Kalshi Agent | {mode} | profit-first | exit +{int(TAKE_PROFIT*100)}c/-{int(STOP_LOSS*100)}c")

        async with KalshiClient(
            self.config.kalshi_api_key,
            self.config.kalshi_base_url,
            self.config.kalshi_private_key_path,
        ) as client:
            bal = await client.get_balance()
            log.info(f"Balance: ${bal:.2f}")

            # Sync real positions from Kalshi on startup
            await self._sync_positions(client)

            while True:
                try:
                    await self._process_settlements(client)
                    await self._manage_exits(client)
                    await self._scan(client)
                except KeyboardInterrupt: break
                except Exception as e: log.error(f"Cycle error: {e}", exc_info=True)

                self._print_stats()
                if self.stats.realized_pnl <= -self.config.max_daily_loss:
                    log.warning("Daily loss limit — stopping."); break

                wait = random.randint(self.config.scan_interval_min, self.config.scan_interval_max)
                log.info(f"Next scan {wait//60}m {wait%60}s | positions {self.stats.count}/{self.config.max_open_positions}")
                await asyncio.sleep(wait)

    async def _sync_positions(self, client):
        """Load real open positions from Kalshi on startup."""
        try:
            real = await client.get_positions()
            for p in real:
                ticker = p.get("market_ticker", p.get("ticker", ""))
                yes_ct = int(p.get("position", p.get("yes_count", 0)) or 0)
                no_ct  = int(p.get("no_count", 0) or 0)
                if not ticker or (yes_ct == 0 and no_ct == 0): continue
                side  = "yes" if yes_ct > 0 else "no"
                count = yes_ct if yes_ct > 0 else no_ct
                mkt   = await client.get_market(ticker)
                bid   = mkt.get("yes_bid", 0)
                ask   = mkt.get("yes_ask", 0)
                mid   = round((bid + ask) / 2, 4) if bid and ask else 0.5
                entry = mid if side == "yes" else round(1 - mid, 4)
                self.stats.positions[ticker] = Position(
                    ticker=ticker, title=ticker, side=side,
                    entry_price=entry, contracts=count,
                    cost=round(entry * count, 2),
                    entry_time=datetime.now(timezone.utc).isoformat(),
                    category="synced", timeframe="synced", is_game=False,
                )
            log.info(f"Synced {len(self.stats.positions)} real positions from Kalshi")
        except Exception as e:
            log.warning(f"Position sync failed: {e}")

    async def _process_settlements(self, client):
        try:
            known = set(self._pnl.get("known_ids", []))
            new = 0
            for s in await client.get_settlements():
                sid = s.get("id") or s.get("market_ticker") or ""
                if not sid or sid in known: continue
                rev  = float(s.get("revenue", 0)) / 100
                cost = float(s.get("cost", 0)) / 100
                pnl  = round(rev - cost, 2)
                won  = pnl > 0
                ticker = s.get("market_ticker", "")
                self._pnl["all_time_pnl"] = round(self._pnl["all_time_pnl"] + pnl, 2)
                self._pnl["wins" if won else "losses"] += 1
                self._pnl["trades"].append({"id": sid, "ticker": ticker,
                    "type": "settlement", "pnl": pnl, "won": won,
                    "time": s.get("created_time", datetime.now(timezone.utc).isoformat())})
                known.add(sid)
                self.stats.realized_pnl += pnl
                if won: self.stats.wins += 1
                else:   self.stats.losses += 1
                log.info(f"  {'✅ WIN' if won else '❌ LOSS'} SETTLED: {ticker} | ${pnl:+.2f}")
                if ticker in self.stats.positions: del self.stats.positions[ticker]
                new += 1
            self._pnl["known_ids"] = list(known)
            if new: self._save_pnl(); log.info(f"  {new} settled. All-time: ${self._pnl['all_time_pnl']:+.2f}")
        except Exception as e: log.warning(f"Settlements: {e}")

    async def _manage_exits(self, client):
        """Check every position — take profit, stop loss, or evict worst to free slot."""
        if not self.stats.positions: return
        tickers = list(self.stats.positions.keys())
        log.info(f"\n[EXIT CHECK] {len(tickers)} positions | {self.config.max_open_positions - self.stats.count} slots free")

        statuses = []
        for ticker in tickers:
            pos = self.stats.positions.get(ticker)
            if not pos: continue
            mkt = await client.get_market(ticker)
            if not mkt: continue
            bid = mkt.get("yes_bid", 0)
            ask = mkt.get("yes_ask", 0)
            if pos.side == "yes":
                current = bid
                move    = current - pos.entry_price
            else:
                current = round(1 - ask, 4) if ask else 0
                move    = current - pos.entry_price
            unreal = round(move * pos.contracts, 2)
            log.info(f"  {ticker[:38]} {pos.side.upper()} {pos.contracts}x | entry={pos.entry_price:.2f} now={current:.2f} | {move:+.2f} | ${unreal:+.2f}")
            statuses.append({"ticker": ticker, "pos": pos, "current": current,
                             "move": move, "unreal": unreal,
                             "status": mkt.get("status", "open")})

        # Determine exits
        to_exit = []
        for ps in statuses:
            reason = ""
            if ps["move"] >= TAKE_PROFIT:
                reason = f"TAKE PROFIT +{ps['move']:.2f}"
            elif ps["move"] <= -STOP_LOSS:
                reason = f"STOP LOSS {ps['move']:.2f}"
            elif ps["status"] in ("finalized", "closed"):
                reason = "MARKET CLOSED"
            if reason: to_exit.append((ps, reason))

        # If at limit, evict the worst-performing non-game position to free a slot
        if self.stats.count >= self.config.max_open_positions:
            already_exiting = {e[0]["ticker"] for e in to_exit}
            candidates = sorted(
                [ps for ps in statuses
                 if not ps["pos"].is_game
                 and ps["move"] < -0.04
                 and ps["ticker"] not in already_exiting],
                key=lambda x: x["move"]
            )
            if candidates:
                worst = candidates[0]
                log.info(f"  🔄 EVICTING worst position to free slot: {worst['ticker']} ({worst['move']:.2f})")
                to_exit.append((worst, f"EVICT for better opportunity (move={worst['move']:.2f})"))

        # Execute exits
        for ps, reason in to_exit:
            ticker   = ps["ticker"]
            pos      = ps["pos"]
            current  = ps["current"]
            proceeds = round(current * pos.contracts, 2)
            pnl      = round(proceeds - pos.cost, 2)
            log.info(f"  → EXIT [{reason}] | {pos.contracts}x @ {current:.2f} | P&L ${pnl:+.2f}")
            if not self.config.dry_run:
                result = await client.sell_position(ticker, pos.side, pos.contracts, current)
                if not result: log.warning(f"  Sell failed {ticker}"); continue
            else:
                log.info(f"  [DRY RUN] SELL {pos.side.upper()} {pos.contracts}x {ticker} @ {current:.2f}")
            self.stats.realized_pnl += pnl
            self.stats.exited += 1
            if pnl > 0: self.stats.wins += 1
            else:       self.stats.losses += 1
            self._pnl["all_time_pnl"] = round(self._pnl["all_time_pnl"] + pnl, 2)
            self._pnl["wins" if pnl > 0 else "losses"] += 1
            self._pnl["trades"].append({"ticker": ticker, "type": "early_exit",
                "side": pos.side, "contracts": pos.contracts,
                "entry": pos.entry_price, "exit": current,
                "pnl": pnl, "reason": reason,
                "time": datetime.now(timezone.utc).isoformat()})
            self._save_pnl()
            if ticker in self.stats.positions: del self.stats.positions[ticker]

    async def _scan(self, client):
        slots = self.config.max_open_positions - self.stats.count
        if slots <= 0:
            log.info("  No slots — exits only"); return

        log.info(f"\n{'='*60}")
        log.info(f"SCAN — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | {slots} slots open")
        log.info(f"{'='*60}")

        # ── Step 1: Fetch ALL priority sports markets ──────────────
        log.info("\n[SPORTS] Fetching best markets...")
        sports_mkts = await client.get_series_markets(PRIORITY_SERIES, limit=15)

        # Only keep markets with real pricing and decent liquidity
        tradeable = [
            m for m in sports_mkts
            if (m.yes_bid > 0 or m.yes_ask > 0)
            and m.volume > 500              # some real trading happened
            and (m.yes_ask - m.yes_bid) <= 0.10  # not a junk spread
        ]

        # Sort by profit potential score (volume × liquidity × urgency)
        tradeable.sort(key=profit_score, reverse=True)

        log.info(f"  {len(tradeable)} tradeable | top 3 by profit score:")
        for m in tradeable[:3]:
            spread = round(m.yes_ask - m.yes_bid, 3) if m.yes_bid and m.yes_ask else '?'
            h = f"{m.hours_until_close:.0f}h" if m.hours_until_close else '?'
            log.info(f"    vol={m.volume:,} spread={spread} {h} | {m.title[:50]}")

        placed = 0
        # Track event tickers to avoid betting both sides of same game
        event_tickers_seen = set()
        for market in tradeable[:8]:
            if self.stats.count >= self.config.max_open_positions: break
            if market.ticker in self.stats.positions: continue
            # Skip if we already have a bet on this game (same event, different outcome)
            event_root = market.ticker.rsplit("-", 1)[0]
            if event_root in event_tickers_seen:
                log.info(f"  → SKIP already have position in this game ({event_root})")
                continue
            event_tickers_seen.add(event_root)
            self.stats.scanned += 1
            await self._evaluate(client, market, "sports")
            await asyncio.sleep(1)
            placed += 1

        # ── Step 2: Daily crypto if slots remain ─────────────────
        if self.stats.count < self.config.max_open_positions:
            log.info("\n[CRYPTO] Daily markets...")
            crypto_mkts = await client.get_series_markets(["KXBTCD","KXETHD"], limit=10)
            daily_crypto = [m for m in crypto_mkts
                           if (m.yes_bid > 0 or m.yes_ask > 0)
                           and m.hours_until_close is not None
                           and m.hours_until_close <= 24]
            daily_crypto.sort(key=profit_score, reverse=True)
            for m in daily_crypto[:3]:
                if self.stats.count >= self.config.max_open_positions: break
                if m.ticker in self.stats.positions: continue
                self.stats.scanned += 1
                await self._evaluate(client, m, "crypto")
                await asyncio.sleep(1)

        # ── Step 3: Daily weather if slots remain ─────────────────
        if self.stats.count < self.config.max_open_positions:
            log.info("\n[WEATHER] Daily markets...")
            wx_mkts = await client.get_series_markets(["KXHIGHNY","RAINNY"], limit=10)
            daily_wx = [m for m in wx_mkts
                       if (m.yes_bid > 0 or m.yes_ask > 0)
                       and m.hours_until_close is not None
                       and m.hours_until_close <= 24]
            daily_wx.sort(key=profit_score, reverse=True)
            for m in daily_wx[:3]:
                if self.stats.count >= self.config.max_open_positions: break
                if m.ticker in self.stats.positions: continue
                self.stats.scanned += 1
                await self._evaluate(client, m, "weather")
                await asyncio.sleep(1)

        # ── Step 4: Fallback econ/politics if zero sports found ────
        if placed == 0 and self.stats.count < self.config.max_open_positions:
            log.info("\n[FALLBACK] No sports trades found — checking econ/politics...")
            fb_mkts = await client.get_series_markets(FALLBACK_SERIES, limit=8)
            fb_tradeable = [m for m in fb_mkts if m.yes_bid > 0 or m.yes_ask > 0]
            fb_tradeable.sort(key=profit_score, reverse=True)
            for m in fb_tradeable[:3]:
                if self.stats.count >= self.config.max_open_positions: break
                if m.ticker in self.stats.positions: continue
                cat = "econ" if any(x in m.ticker for x in ["CPI","FED","GDP","INXZ"]) else "politics"
                self.stats.scanned += 1
                await self._evaluate(client, m, cat)
                await asyncio.sleep(1)

    async def _evaluate(self, client, market: KalshiMarket, category: str):
        cat_cfg = CATEGORY_CONFIG.get(category, CATEGORY_CONFIG["sports"])
        # CRITICAL: Check if we already have ANY position on this event (game)
        # e.g. if we have GSW YES, don't bet PHX YES on the same game
        event = event_root(market.ticker)
        for existing_ticker in self.stats.positions:
            if event_root(existing_ticker) == event:
                log.info(f"  → SKIP already have position in this game ({event})")
                self.stats.skipped += 1
                return
        # Also check already-settled trades — no re-entering the same game
        # Skip markets already resolved or near-resolved
        if market.yes_bid >= 0.97 or market.yes_ask >= 0.99:
            log.info(f"  → SKIP market already near-resolved (bid={market.yes_bid:.2f} ask={market.yes_ask:.2f})")
            self.stats.skipped += 1
            return
        if market.yes_ask <= 0.03 and market.yes_bid <= 0.01:
            log.info(f"  → SKIP market near-zero (too resolved)")
            self.stats.skipped += 1
            return
        # Skip markets closing in less than 30 minutes
        if market.hours_until_close is not None and market.hours_until_close < 0.5:
            log.info(f"  → SKIP closing too soon ({market.hours_until_close:.1f}h left)")
            self.stats.skipped += 1
            return
        game_flag = is_game(market.title)
        spread = round(market.yes_ask - market.yes_bid, 3) if market.yes_bid and market.yes_ask else '?'
        log.info(f"\n  {'🏀' if game_flag else '📊'} {market.title[:68]}")
        log.info(f"  {market.ticker} | mid={market.mid_price:.2f} spread={spread} vol={market.volume:,} | [{market.timeframe_label}]")

        api_val = getattr(self.config, cat_cfg["cfg"], "")
        try:
            signals = await cat_cfg["fetcher"](market.title, **{cat_cfg["key_arg"]: api_val})
        except Exception as e:
            log.warning(f"  Signals error: {e}"); signals = {}

        log.info(f"  Signals: {list(signals.keys()) if signals else 'none'}")
        signal = self.reasoner.score_market(market, signals, category)
        if not signal: log.warning("  Claude returned None"); return

        log.info(f"  Claude: prob={signal.estimated_prob:.2f} edge={signal.edge:+.2f} action={signal.action} conf={signal.confidence:.0%}")
        log.info(f"  {signal.reasoning}")

        if signal.action == "skip":
            log.info("  → SKIP"); self.stats.skipped += 1; return

        thresh = edge_threshold(market)
        MIN_CONFIDENCE = 0.70  # require 70%+ confidence — aiming at 70% win rate
        if signal.confidence < MIN_CONFIDENCE:
            log.info(f"  → SKIP confidence {signal.confidence:.0%} < 70% minimum")
            self.stats.skipped += 1
            return
        if signal.action == "buy_yes" and signal.edge >= thresh:
            await self._place(client, market, "yes", signal, category)
        elif signal.action == "buy_no" and signal.edge <= -thresh:
            await self._place(client, market, "no", signal, category)
        else:
            log.info(f"  → SKIP (edge {signal.edge:+.2f} below {thresh:.2f} threshold)")
            self.stats.skipped += 1

    async def _place(self, client, market: KalshiMarket, side: str,
                     signal: TradeSignal, category: str):
        c = signal.confidence
        if c >= 0.85:   frac, tier = 1.00, "HIGH"
        elif c >= 0.70: frac, tier = 0.70, "MEDIUM"
        elif c >= 0.55: frac, tier = 0.50, "LOW"
        else:           frac, tier = 0.30, "VERY LOW"

        bet   = round(self.config.max_bet_size * frac, 2)
        price = (market.yes_ask if side == "yes" else market.no_ask)
        if price <= 0: price = market.mid_price if side == "yes" else round(1 - market.mid_price, 4)
        if price <= 0: log.warning("  No valid price"); return

        count = max(1, int(bet / price))
        cost  = round(price * count, 2)
        game_flag = is_game(market.title)

        log.info(f"  Confidence {c:.0%} ({tier}) → ${bet:.2f}")
        if self.config.dry_run:
            log.info(f"  [DRY RUN] BUY {side.upper()} {market.ticker} | {count}x@{price:.0%} | ${cost:.2f}")
        else:
            result = await client.place_order(market.ticker, side, price, count)
            if not result: log.error("  Order failed"); return
            log.info(f"  ✓ BUY {side.upper()} {market.ticker} | {count}x@{price:.0%} | ${cost:.2f} | id={result.order_id}")

        self.stats.positions[market.ticker] = Position(
            ticker=market.ticker, title=market.title[:80], side=side,
            entry_price=price, contracts=count, cost=cost,
            entry_time=datetime.now(timezone.utc).isoformat(),
            category=category, timeframe=market.timeframe_label,
            is_game=game_flag,
        )
        self.stats.placed += 1
        self._save_trade_log()

    def _save_trade_log(self):
        try:
            with open("trade_log.json", "w") as f:
                json.dump({
                    "updated": datetime.now(timezone.utc).isoformat(),
                    "session": {"scanned": self.stats.scanned, "placed": self.stats.placed,
                                "exited": self.stats.exited, "skipped": self.stats.skipped,
                                "positions": self.stats.count,
                                "realized_pnl": round(self.stats.realized_pnl, 2),
                                "win_rate": round(self.stats.win_rate, 3)},
                    "open_positions": {
                        t: {"title": p.title, "side": p.side, "entry": p.entry_price,
                            "contracts": p.contracts, "cost": p.cost,
                            "timeframe": p.timeframe, "is_game": p.is_game}
                        for t, p in self.stats.positions.items()},
                    "all_time_pnl": self._pnl.get("all_time_pnl", 0),
                }, f, indent=2)
        except Exception as e: log.warning(f"trade_log: {e}")

    def _print_stats(self):
        s = self.stats
        games = sum(1 for p in s.positions.values() if p.is_game)
        log.info(f"\n── Stats ───────────────────────────────────────────")
        log.info(f"  Scanned {s.scanned} | Placed {s.placed} | Exited {s.exited} | Skipped {s.skipped}")
        log.info(f"  Positions {s.count}/{self.config.max_open_positions} ({games} games, {s.count-games} other)")
        log.info(f"  Session P&L ${s.realized_pnl:+.2f} | All-time ${self._pnl.get('all_time_pnl',0):+.2f}")
        log.info(f"  Win rate {s.win_rate:.0%} ({s.wins}W/{s.losses}L)")
        log.info(f"────────────────────────────────────────────────────\n")
