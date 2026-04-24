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
    # ═══ SAME-DAY MARKETS ONLY — resolve within 24h ═══
    # NBA individual games (resolve tonight)
    "KXNBAGAME",
    "KXNBA1HWINNER",    # 1st half — resolves in ~1h
    "KXNBA2HWINNER",    # 2nd half — resolves in ~2h
    "KXNBA1QWINNER",    # Q1 — resolves in ~30 min
    "KXNBA2QWINNER",
    "KXNBA3QWINNER",
    "KXNBA4QWINNER",
    "KXNBAPLAYERPTS",   # Player points — same game
    "KXNBAPLAYOFFPTS",
    # NHL games (resolve tonight)
    "KXNHLGAME",
    # MLB games (resolve today/tonight)
    "KXMLBGAME",
    "KXMLBF5",          # First 5 innings — resolves in ~1.5h
    "KXMLBRUNS",        # Total runs — same game
    # Soccer matches (resolve in ~2h)
    "KXEPLGAME",
    "KXUCLGAME",
    "KXUELGAME",
    "KXLALIGAGAME",
    "KXSERIEAGAME",
    "KXBUNDESLIGAGAME",
    "KXLIGUE1GAME",
    "KXMLSGAME",
    # Tennis (resolves in ~2-4h)
    "KXATPMATCH",
    "KXWTAMATCH",
    # Weather (resolves today)
    "KXHIGHNY",
    "KXHIGHDEN",
    "KXHIGHCHI",
    "KXHIGHLAX",
    "KXRAINNY",
    # Crypto daily (resolves today)
    "KXBTCD",
    "KXETHD",
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
    """Score by FAST-CLOSE profit potential. Heavily favor same-day markets."""
    vol    = m.volume or 0
    spread = max((m.yes_ask - m.yes_bid) if m.yes_bid and m.yes_ask else 0.5, 0.001)
    h      = m.hours_until_close or 9999
    
    # HARD GATE: markets beyond 48h get zero score (won't be picked)
    if h > 48:
        return 0.0
    
    # Urgency tiers — heavily favor same-day
    if h < 3:       urgency = 5000    # resolves in hours
    elif h < 12:    urgency = 2000    # resolves today
    elif h < 24:    urgency = 800     # resolves within 24h
    elif h < 48:    urgency = 200     # tomorrow
    else:           urgency = 0       # blocked above
    
    vol_score    = min(vol / 10000, 1000)
    spread_score = 1.0 / spread       # tighter = better
    return vol_score * spread_score * urgency


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


def hours_until_game_from_ticker(ticker: str) -> float:
    """Parse game date from Kalshi ticker like KXMLBGAME-26APR26...
    Returns hours until the game date's start-of-day UTC, or None if unparseable.
    This is used INSTEAD of close_time for sports because Kalshi keeps sports
    markets open for days after the game (settlement buffer)."""
    import re
    from datetime import datetime, timezone
    
    m = re.match(r"KX[A-Z]+-?(\d{2})([A-Z]{3})(\d{2})", ticker)
    if not m:
        return None
    
    try:
        yy = int(m.group(1)) + 2000
        mon_str = m.group(2)
        dd = int(m.group(3))
        months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        mon = months.get(mon_str)
        if not mon:
            return None
        game_start = datetime(yy, mon, dd, 0, 0, 0, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (game_start - now).total_seconds() / 3600
    except Exception:
        return None




def edge_threshold(m: KalshiMarket) -> float:
    """Loosened to find more opportunities. Still requires real edge."""
    spread = (m.yes_ask - m.yes_bid) if m.yes_bid and m.yes_ask else 0.5
    vol    = m.volume or 0
    mid    = m.mid_price
    is_spread_market = "SPREAD" in m.ticker.upper()
    
    # Base threshold by price range
    if mid >= 0.90 or mid <= 0.10:
        base = 0.08   # cheap / expensive markets — was 0.20
    elif mid >= 0.75 or mid <= 0.25:
        base = 0.06   # favorite / underdog — was 0.20
    elif spread <= 0.02 and vol > 100000:
        base = 0.02   # thick liquid books — unchanged
    elif spread <= 0.05 and vol > 10000:
        base = 0.05   # good liquidity — was 0.15
    elif spread <= 0.10 and vol > 1000:
        base = 0.07   # moderate — was 0.18
    else:
        base = 0.12   # illiquid — was 0.25
    
    # SPREAD markets historically best — 2c discount
    if is_spread_market:
        base = max(0.02, base - 0.02)
    
    return base


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
    realized_pnl_dollars: float = 0.0
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
                if self.stats.realized_pnl_dollars <= -self.config.max_daily_loss:
                    log.warning("Daily loss limit — stopping."); break

                wait = random.randint(self.config.scan_interval_min, self.config.scan_interval_max)
                log.info(f"Next scan {wait//60}m {wait%60}s | positions {self.stats.count}/{100}")
                await asyncio.sleep(wait)

    async def _sync_positions(self, client):
        """Load real open positions from Kalshi on startup."""
        await self._sync_positions_from_kalshi(client)

    async def _sync_positions_from_kalshi(self, client):
        """Pull fresh positions from Kalshi and OVERWRITE internal state.
        Called at startup AND at the start of every scan cycle to prevent drift.
        Preserves existing Position metadata (title/category/timeframe) when the
        ticker is already known; otherwise creates a synced placeholder."""
        try:
            real = await client.get_positions()
            fresh_positions = {}
            
            for p in real:
                ticker = p.get("market_ticker", p.get("ticker", ""))
                pos_fp = float(p.get("position_fp", 0) or 0)
                if not ticker or pos_fp == 0:
                    continue
                
                # Correctly determine side and count from signed position_fp
                side = "yes" if pos_fp > 0 else "no"
                count = int(abs(pos_fp))
                
                # Preserve existing metadata if we already know this ticker
                existing = self.stats.positions.get(ticker)
                if existing:
                    # Update contracts but keep original entry_price/title/category
                    existing.contracts = count
                    existing.side = side
                    fresh_positions[ticker] = existing
                else:
                    # New ticker (e.g. placed outside the agent) — create placeholder
                    try:
                        mkt = await client.get_market(ticker)
                        bid = mkt.get("yes_bid", 0)
                        ask = mkt.get("yes_ask", 0)
                        mid = round((bid + ask) / 2, 4) if bid and ask else 0.5
                    except Exception:
                        mid = 0.5
                    entry = mid if side == "yes" else round(1 - mid, 4)
                    fresh_positions[ticker] = Position(
                        ticker=ticker, title=ticker, side=side,
                        entry_price=entry, contracts=count,
                        cost=round(entry * count, 2),
                        entry_time=datetime.now(timezone.utc).isoformat(),
                        category="synced", timeframe="synced", is_game=False,
                    )
            
            removed = set(self.stats.positions.keys()) - set(fresh_positions.keys())
            added = set(fresh_positions.keys()) - set(self.stats.positions.keys())
            
            self.stats.positions = fresh_positions
            
            if removed or added:
                log.info(f"  Position sync: {len(fresh_positions)} open "
                         f"(+{len(added)} new, -{len(removed)} closed)")
            else:
                log.info(f"  Position sync: {len(fresh_positions)} open (no changes)")
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
                self.stats.realized_pnl_dollars += pnl
                if won: self.stats.wins += 1
                else:   self.stats.losses += 1
                log.info(f"  {'✅ WIN' if won else '❌ LOSS'} SETTLED: {ticker} | ${pnl:+.2f}")
                if ticker in self.stats.positions: del self.stats.positions[ticker]
                new += 1
            self._pnl["known_ids"] = list(known)
            if new: self._save_pnl(); log.info(f"  {new} settled. All-time: ${self._pnl['all_time_pnl']:+.2f}")
        except Exception as e: log.warning(f"Settlements: {e}")

    async def _manage_exits(self, client):
        """Auto-exit DISABLED — let positions ride to resolution."""
        if not self.stats.positions:
            return
        # Still log positions for visibility but never auto-sell
        import logging
        log = logging.getLogger(__name__)
        log.info(f"[POSITIONS] {len(self.stats.positions)} open, letting them resolve naturally")
        return

    async def _manage_exits_DISABLED(self, client):
        """Check every position_fp — take profit, stop loss, or evict worst to free slot."""
        if not self.stats.positions: return
        tickers = list(self.stats.positions.keys())
        log.info(f"\n[EXIT CHECK] {len(tickers)} positions | {100 - self.stats.count} slots free")

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

        # If at limit, evict the worst-performing non-game position_fp to free a slot
        if self.stats.count >= 100:
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
                log.info(f"  🔄 EVICTING worst position_fp to free slot: {worst['ticker']} ({worst['move']:.2f})")
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
                result = await client.sell_position_fp(ticker, pos.side, pos.contracts, current)
                if not result: log.warning(f"  Sell failed {ticker}"); continue
            else:
                log.info(f"  [DRY RUN] SELL {pos.side.upper()} {pos.contracts}x {ticker} @ {current:.2f}")
            self.stats.realized_pnl_dollars += pnl
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
        # CRITICAL: re-sync positions from Kalshi every scan to prevent stale count drift
        await self._sync_positions_from_kalshi(client)
        
        slots = 100 - self.stats.count
        if slots <= 0:
            log.info(f"  No slots — {self.stats.count}/{100} positions held")
            return

        log.info("")
        log.info("=" * 60)
        log.info(f"SCAN — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | {slots} slots open")
        log.info("=" * 60)

        log.info("")
        log.info("[SPORTS] Fetching best markets...")
        sports_mkts = await client.get_series_markets(PRIORITY_SERIES, limit=15)

        tradeable = [
            m for m in sports_mkts
            if (m.yes_bid > 0 or m.yes_ask > 0)
            and m.volume > 500
            and (m.yes_ask - m.yes_bid) <= 0.10
        ]
        tradeable.sort(key=profit_score, reverse=True)
        log.info(f"  {len(tradeable)} tradeable markets after initial filters")

        # ═══ PHASE 1: Cheap hard filters — no API calls ═══
        candidates = []
        for market in tradeable:
            h = market.hours_until_close
            t = market.ticker

            if "KXBTCD" in t or "KXETHD" in t:
                if market.volume < 100:
                    continue
                if market.yes_ask <= 0.02 and market.volume < 1000:
                    continue
            if "KXHIGHNY" in t or "RAINNY" in t:
                now_hour = datetime.now(timezone.utc).hour
                if h is not None and h < 6 and now_hour >= 18:
                    continue
            if "TRUMPMENTION" in t:
                continue
            if market.yes_bid >= 0.97 or market.yes_ask >= 0.99:
                continue
            if market.yes_ask <= 0.03 and market.yes_bid <= 0.01:
                continue
            if h is not None and h < 0.5:
                continue
            # HARD CUTOFF: only bet on markets closing within 36 hours (today + tomorrow)
            # Rejects future playoff/championship markets dated days or weeks out
            # For sports tickers, Kalshi's close_time is days AFTER the game
            # (settlement buffer). Use the ticker's game date instead.
            is_sports = any(x in t.upper() for x in [
                "KXNBA","KXNHL","KXMLB","KXNFL","KXWNBA","KXCFB","KXCBB",
                "KXUCL","KXUEL","KXEPL","KXLALIGA","KXSERIEA","KXBUNDESLIGA",
                "KXLIGUE1","KXMLS","KXATP","KXWTA","KXPGA","KXUFC","KXBOXING"
            ])
            if is_sports:
                game_h = hours_until_game_from_ticker(t)
                if game_h is None or game_h > 72 or game_h < -18:  # allow in-progress + late-night games  # -6 allows in-progress games
                    continue
            else:
                if h is None or h > 72:
                    continue
            # Max 3 positions per game event
            event = event_root(t)
            if sum(1 for tk in self.stats.positions if event_root(tk) == event) >= 3:
                continue
            candidates.append(market)

        log.info(f"  {len(candidates)} passed hard filters — evaluating with Claude...")

        # ═══ PHASE 2: Claude evaluation — collect all BUY recommendations ═══
        MIN_CONFIDENCE = 0.63  # loosened from 0.68
        opportunities = []

        for market in candidates:
            self.stats.scanned += 1
            tu = market.ticker.upper()
            if any(x in tu for x in ["KXNBA","KXNHL","KXMLB","KXNFL","KXUCL","KXUEL","KXEPL",
                                      "KXLALIGA","KXSERIEA","KXBUNDESLIGA","KXLIGUE1","KXMLS",
                                      "KXWNBA","KXCFB","KXCBB","KXATP","KXWTA","KXPGA",
                                      "KXUFC","KXBOXING"]):
                category = "sports"
            elif any(x in tu for x in ["KXBTC","KXETH","BTCUSD","ETHUSD"]):
                category = "crypto"
            elif any(x in tu for x in ["KXHIGH","RAINNY","SNOW"]):
                category = "weather"
            elif any(x in tu for x in ["CPI","FED","GDP","UNEMPLOYMENT","JOBS","PCE"]):
                category = "econ"
            elif any(x in tu for x in ["TRUMP","BIDEN","ELECTION","SENATE","HOUSE","SCOTUS"]):
                category = "politics"
            else:
                category = "sports"

            cat_cfg = CATEGORY_CONFIG.get(category, CATEGORY_CONFIG["sports"])
            api_val = getattr(self.config, cat_cfg.get("cfg", ""), "")
            try:
                signals = await cat_cfg["fetcher"](market.title, **{cat_cfg["key_arg"]: api_val})
            except Exception as e:
                log.warning(f"  Signals error {market.ticker}: {e}")
                signals = {}

            # Enrich signals with orderbook intelligence (volume imbalance)
            try:
                orderbook = await client.get_orderbook(market.ticker)
                if orderbook:
                    signals["orderbook"] = orderbook
            except Exception:
                pass

            # Enrich with LIVE GAME STATE (ESPN scoreboard)
            if category == "sports":
                try:
                    from fetchers.live_scores import get_live_game_for_ticker
                    # Reuse the session from the client
                    live_game = await get_live_game_for_ticker(client._session, market.ticker)
                    if live_game:
                        signals["live_game"] = live_game
                except Exception as e:
                    log.debug(f"Live score fetch failed {market.ticker}: {e}")

            signal = self.reasoner.score_market(market, signals, category)
            
            # Log every evaluation so we see what's being considered
            ob = signals.get("orderbook", {})
            imb = ob.get("imbalance", 0) if ob else 0
            conv = ob.get("conviction_score", 0) if ob else 0
            lg = signals.get("live_game", {})
            live_str = ""
            if lg:
                if lg.get("is_live"):
                    live_str = f" LIVE:{lg['away']}{lg['away_score']}-{lg['home_score']}{lg['home']}({lg.get('detail','')[:15]})"
                elif lg.get("is_final"):
                    live_str = f" FINAL:{lg['away']}{lg['away_score']}-{lg['home_score']}{lg['home']}"
                else:
                    live_str = f" PRE:{lg['away']}@{lg['home']}"
            sig_str = f"{signal.action}@{signal.confidence:.0%} edge={signal.edge:+.2f}" if signal else "skipped"
            log.info(f"  EVAL {market.ticker[:42]:<42} | ${market.yes_ask:.2f} | imb={imb:+.2f} conv={conv:.2f}{live_str} | {sig_str}")
            
            if not signal:
                self.stats.skipped += 1
                continue
            if signal.action == "skip":
                self.stats.skipped += 1
                continue
            if signal.confidence < MIN_CONFIDENCE:
                self.stats.skipped += 1
                continue

            thresh = edge_threshold(market)
            if signal.action == "buy_yes" and signal.edge >= thresh:
                side = "yes"
            elif signal.action == "buy_no" and signal.edge <= -thresh:
                side = "no"
            else:
                self.stats.skipped += 1
                continue

            # Guarantee score = confidence × edge magnitude (higher = more guaranteed)
            opportunities.append({
                "market": market,
                "signal": signal,
                "side": side,
                "category": category,
                "score": signal.confidence * abs(signal.edge),
            })
            log.info(f"  ✓ OPP: {market.ticker} {side.upper()} conf={signal.confidence:.0%} edge={signal.edge:+.2f} score={signal.confidence * abs(signal.edge):.3f}")

        log.info("")
        log.info(f"  {len(opportunities)} BUY opportunities identified")

        # ═══ PHASE 3: Deliberate — if more than slots available, take most guaranteed ═══
        if len(opportunities) > slots:
            log.info(f"  More opportunities than slots ({len(opportunities)} > {slots}) — ranking by confidence × edge...")
            opportunities.sort(key=lambda o: o["score"], reverse=True)
            opportunities = opportunities[:slots]
            log.info(f"  → Taking top {len(opportunities)} most guaranteed trades")

        # ═══ PHASE 4: Place trades ═══
        placed = 0
        for opp in opportunities:
            if self.stats.count >= 100:
                log.info("  Max positions reached — stopping")
                break
            await self._place(client, opp["market"], opp["side"], opp["signal"], opp["category"])
            placed += 1

        log.info("")
        log.info(f"  Scan complete: {placed}/{len(opportunities)} trades placed")

    async def _place(self, client, market: KalshiMarket, side: str,
                     signal: TradeSignal, category: str):
        c = signal.confidence
        edge = abs(signal.edge)
        # Combine confidence + edge for sizing
        # Higher of either boosts the bet; both high = max bet
        if c >= 0.88 and edge >= 0.15:  frac, tier = 1.00, "ELITE"
        elif c >= 0.85:                 frac, tier = 0.85, "HIGH"
        elif c >= 0.80:                 frac, tier = 0.70, "SOLID"
        elif c >= 0.75:                 frac, tier = 0.55, "GOOD"
        else:                           frac, tier = 0.40, "MODERATE"

        bet   = round(40.0 * frac, 2)  # HARDCODED: was self.config.max_bet_size
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
                                "realized_pnl_dollars": round(self.stats.realized_pnl_dollars, 2),
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
        log.info(f"  Positions {s.count}/{100} ({games} games, {s.count-games} other)")
        log.info(f"  Session P&L ${s.realized_pnl_dollars:+.2f} | All-time ${self._pnl.get('all_time_pnl',0):+.2f}")
        log.info(f"  Win rate {s.win_rate:.0%} ({s.wins}W/{s.losses}L)")
        log.info(f"────────────────────────────────────────────────────\n")
