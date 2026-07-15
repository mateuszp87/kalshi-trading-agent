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

import asyncio, json, logging, random, re, os
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

from config import AgentConfig
from kalshi_client import KalshiClient, KalshiMarket
from reasoner import ClaudeReasoner, TradeSignal
SIGNAL_LOG_PATH = os.environ.get("SIGNAL_LOG_PATH", "signal_log.json")
from fetchers import (
    fetch_sports_signals, fetch_politics_signals, fetch_econ_signals,
    fetch_entertainment_signals, fetch_crypto_signals, fetch_weather_signals,
)

log = logging.getLogger(__name__)

TAKE_PROFIT = 0.20   # let winners run to +20c
STOP_LOSS   = 0.10   # cut losers fast at -10c

# ── Ordered by profitability (volume × liquidity) ─────────────
PRIORITY_SERIES = [
    # ═══ YEAR-ROUND, MODEL-PRICEABLE MARKETS — the real edge ═══
    # Weather (physics ensemble prices these; resolves today)
    "KXHIGHNY", "KXHIGHDEN", "KXHIGHCHI", "KXHIGHLAX",
    "KXHIGHTSEA", "KXHIGHTHOU", "KXHIGHTPHX", "KXHIGHTATL",
    # Crypto daily strikes (digital-pricing model prices these; resolves today)
    "KXBTCD", "KXETHD",
    # Commodities daily/weekly (Ornstein-Uhlenbeck target; year-round)
    "KXWTI", "KXWTIW", "KXBRENTD",
    "KXGOLDD", "KXGOLDW",
    "KXSILVERD", "KXSILVERW",
    "KXCOPPERD", "KXCOPPERW",
    "KXNATGASD", "KXNATGASW",
    # Financials / macro catalysts (skew-normal target)
    "KXFED", "KXCPI", "KXPAYROLLS", "KXUSNFP", "KXGDP",
    "KXNASDAQ100", "KXINX",
]

DAILY_SERIES = [
    # Secondary commodities / crypto — lighter tier
    "KXHOILW", "KXCORNW", "KXWHEATW",
    "KXGOLDMON", "KXWTIMONTHLY",
]

FALLBACK_SERIES = [
    # ═══ SPORTS — demoted: unproven, seasonal, -14pp overconfidence gap ═══
    "KXNBAGAME", "KXNHLGAME", "KXMLBGAME", "KXMLBF5",
    "KXMLBTOTAL", "KXMLBSPREAD",
    "KXWNBAGAME", "KXWNBASPREAD", "KXWNBATOTAL",
    "KXEPLGAME", "KXUCLGAME", "KXUELGAME",
    "KXLALIGAGAME", "KXSERIEAGAME", "KXBUNDESLIGAGAME",
    "KXLIGUE1GAME", "KXMLSGAME", "KXATPMATCH",
    "KXWCGAME", "KXWCTOTAL", "KXWCSPREAD",
]

CATEGORY_CONFIG = {
    "sports":   {"fetcher": fetch_sports_signals,   "key_arg": "api_key",          "cfg": "espn_api_key"},
    "crypto":   {"fetcher": fetch_crypto_signals,   "key_arg": "coingecko_api_key","cfg": "coingecko_api_key"},
    "commodities": {"fetcher": fetch_econ_signals, "key_arg": "fred_api_key", "cfg": "fred_api_key"},
    "finance":     {"fetcher": fetch_econ_signals, "key_arg": "fred_api_key", "cfg": "fred_api_key"},
    "weather":  {"fetcher": fetch_weather_signals,  "key_arg": "noaa_token",       "cfg": "noaa_token"},
    "econ":     {"fetcher": fetch_econ_signals,     "key_arg": "fred_api_key",     "cfg": "fred_api_key"},
    "politics": {"fetcher": fetch_politics_signals, "key_arg": "newsapi_key",      "cfg": "newsapi_key"},
    "entertainment": {"fetcher": fetch_entertainment_signals, "key_arg": "newsapi_key", "cfg": "newsapi_key"},
}


# Kalshi multivariate/parlay series — penny combos, 1c spreads, huge profit_score.
# These dominate any spread-weighted ranking and are not what this agent trades.
EXCLUDED_SERIES_PREFIXES = ("KXMVE",)


def profit_score(m: KalshiMarket) -> float:
    """Score by FAST-CLOSE profit potential. Heavily favor same-day markets."""
    if m.ticker.upper().startswith(EXCLUDED_SERIES_PREFIXES):
        return 0.0
    # Penny longshots: never worth it regardless of stated confidence
    if m.yes_ask <= 0.15 and m.yes_bid <= 0.15:
        return 0.0
    if m.yes_ask >= 0.85 and m.yes_bid >= 0.85:
        return 0.0

    vol    = m.volume or 0
    spread = max((m.yes_ask - m.yes_bid) if m.yes_bid and m.yes_ask else 0.5, 0.001)
    h      = m.hours_until_close or 9999
    
    # HARD GATE: markets beyond 168h (1 week) get zero score
    if h > 168:
        return 0.0
    
    # Urgency tiers — still favor same-day, but allow up to a week
    if h < 3:       urgency = 5000    # resolves in hours
    elif h < 12:    urgency = 2000    # resolves today
    elif h < 24:    urgency = 800     # resolves within 24h
    elif h < 48:    urgency = 200     # tomorrow
    elif h < 96:    urgency = 60      # this week
    elif h < 168:   urgency = 20      # within a week
    else:           urgency = 0
    
    vol_score    = min(vol / 10000, 1000)
    spread_score = min(1.0 / spread, 25)   # cap: 4c spread and tighter score alike
    return vol_score * spread_score * urgency


def event_root(ticker: str) -> str:
    """Extract event ticker (the game itself), stripping the outcome suffix.
    KXNBAGAME-26APR17GSWPHX-GSW → KXNBAGAME-26APR17GSWPHX
    KXNBAGAME-26APR17GSWPHX-PHX → KXNBAGAME-26APR17GSWPHX (same event!)
    """
    parts = ticker.rsplit("-", 1)
    return parts[0] if len(parts) > 1 else ticker


MAX_PER_CORRELATION_GROUP = 2


def correlation_group(ticker: str) -> str:
    """Map a ticker to a shared risk factor. Positions in the same group move
    together, so we cap how many we hold. Catches correlation event_root misses:
    BTC bets on different dates, or multiple same-city temperature bets."""
    t = ticker.upper()
    m = re.match(r"^KX(BTC|ETH|SOL|DOGE|XRP)", t)
    if m:
        return f"CRYPTO_{m.group(1)}"
    m = re.match(r"^(KXHIGH[A-Z]+|KXLOW[A-Z]+)", t)
    if m:
        return f"WX_{m.group(1)}"
    return t.split("-")[0]


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
    
    # Series name may contain digits (KXMLBF5, KXNBA1H...), so match up to the dash.
    # Date block is YYMONDD, optionally followed by HHMM start time (UTC).
    m = re.match(r"KX[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{4})?", ticker)
    if not m:
        return None

    try:
        yy = int(m.group(1)) + 2000
        mon_str = m.group(2)
        dd = int(m.group(3))
        hhmm = m.group(4)
        months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        mon = months.get(mon_str)
        if not mon:
            return None
        if hhmm:
            hh, mi = int(hhmm[:2]), int(hhmm[2:])
            if hh > 23 or mi > 59:
                hh, mi = 0, 0
        else:
            hh, mi = 0, 0
        game_start = datetime(yy, mon, dd, hh, mi, 0, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (game_start - now).total_seconds() / 3600
    except Exception:
        return None




SCAN_HOURS_UTC = [15, 0]  # 8am PT and 5pm PT (PDT = UTC-7); 0 is next-day 00:00 UTC


def seconds_until_next_scan() -> int:
    """Seconds until the next 8am/5pm Pacific scan slot."""
    now = datetime.now(timezone.utc)
    candidates = []
    for day_offset in (0, 1):
        for h in SCAN_HOURS_UTC:
            t = (now + timedelta(days=day_offset)).replace(hour=h, minute=0, second=0, microsecond=0)
            if t > now:
                candidates.append(t)
    return int((min(candidates) - now).total_seconds())


# Confidence required, by how much real signal we actually have.
# Sports/weather have live scores + forecast ensembles feeding the reasoner.
# Econ/politics/crypto have thin signal — demand more before risking money.
BASE_CONFIDENCE = 0.63
THIN_SIGNAL_CONFIDENCE = 0.70
LONG_HORIZON_CONFIDENCE = 0.75

RICH_SIGNAL_CATEGORIES = {"sports", "weather"}


def required_confidence(category: str, hours_until_close, ticker: str = "") -> tuple:
    """Returns (threshold, reason) — higher bar for thin signal and distant resolution.

    For sports, Kalshi's close_time sits days past the game (settlement buffer),
    so a game tonight reads as 80h out and wrongly demands the long-horizon bar.
    Use the ticker's encoded game time instead, as the hard filter already does.
    """
    h = hours_until_close if hours_until_close is not None else 9999

    # Only sports tickers encode a game time. Weather/econ tickers carry a
    # date that the parser will happily read as a "game", producing nonsense.
    is_sports = ticker and any(x in ticker.upper() for x in (
        "KXNBA", "KXNHL", "KXMLB", "KXNFL", "KXWNBA", "KXCFB", "KXCBB",
        "KXUCL", "KXUEL", "KXEPL", "KXLALIGA", "KXSERIEA", "KXBUNDESLIGA",
        "KXLIGUE1", "KXMLS", "KXATP", "KXWTA", "KXWC", "KXPGA",
        "KXUFC", "KXBOXING"))
    if is_sports:
        game_h = hours_until_game_from_ticker(ticker)
        if game_h is not None:
            h = game_h

    if h > 72:
        return LONG_HORIZON_CONFIDENCE, f"long-horizon ({h:.0f}h out)"
    if category not in RICH_SIGNAL_CATEGORIES:
        return THIN_SIGNAL_CONFIDENCE, f"thin-signal category ({category})"
    return BASE_CONFIDENCE, "same-week, rich signal"


# How long after tipoff/first-pitch is a market still genuinely uncertain?
# MLB first-five-innings resolves ~90 min in — betting it 4h later is betting
# a settled outcome. NBA in the 3rd quarter is a real bet. One number can't
# cover both.
IN_PROGRESS_WINDOW_HOURS = {
    "KXMLBF5":     -1.5,   # F5 done after ~5 innings
    "KXMLBSPREAD": -3.0,   # full game
    "KXMLBTOTAL":  -3.0,
    "KXMLBGAME":   -3.0,
    "KXNBAGAME":   -2.0,   # in-progress NBA is the original strategy
    "KXNHLGAME":   -2.0,
    "KXWNBAGAME":  -2.0,
    "KXATPMATCH":  -1.0,   # match length varies wildly
    "KXWTAMATCH":  -1.0,
}
DEFAULT_IN_PROGRESS_WINDOW = -1.0


def in_progress_window(ticker: str) -> float:
    """Most-negative game_h we'll still accept for this series."""
    prefix = ticker.split("-")[0].upper()
    return IN_PROGRESS_WINDOW_HOURS.get(prefix, DEFAULT_IN_PROGRESS_WINDOW)


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
    pred_prob: float = 0.5      # model's estimated YES prob at entry
    pred_conf: float = 0.5      # model's confidence at entry


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
            log.info(f"Cash: ${bal:.2f}")
            session_start_equity = await self._equity(client)
            log.info(f"Equity: ${session_start_equity:.2f} (cash + open positions at bid)")

            # Sync real positions from Kalshi on startup
            await self._sync_positions(client)

            # Calibration scorecard: which categories actually make money.
            # Logging only — does not change trading behavior.
            try:
                from calibration import report as _calib_report
                _rep = _calib_report()
                if _rep:
                    log.info("  ── Calibration scorecard ──")
                    for _cat, _s in _rep.items():
                        _flag = "LOSING" if _s["realized_pnl"] < 0 else "ok"
                        log.info(f"    {_cat:<14} n={_s['n']:<3} "
                                 f"pnl=${_s['realized_pnl']:+.0f} "
                                 f"hit={_s['hit_rate']} brier={_s['brier']} [{_flag}]")
                        if _s["n"] >= 20 and _s["realized_pnl"] < 0:
                            log.warning(f"    ^ {_cat} has lost money over {_s['n']} bets "
                                        f"— consider raising its confidence bar.")
            except Exception as _e:
                log.warning(f"  calibration scorecard skipped: {_e}")

            while True:
                try:
                    await self._process_settlements(client)
                    await self._manage_exits(client)
                    await self._scan(client)
                except KeyboardInterrupt: break
                except Exception as e: log.error(f"Cycle error: {e}", exc_info=True)

                self._print_stats()
                # Circuit breaker off Kalshi's balance, not a local tally.
                # Note: this includes unrealized moves in open positions, so it
                # trips on drawdown, not just realized losses. That's stricter
                # than the old behaviour and intentionally so.
                try:
                    eq = await self._equity(client)
                    drawdown = session_start_equity - eq
                    log.info(f"  Equity ${eq:.2f} (session {eq - session_start_equity:+.2f})")
                    if drawdown >= self.config.max_daily_loss:
                        log.warning(f"Equity drawdown ${drawdown:.2f} >= limit "
                                    f"${self.config.max_daily_loss:.2f} — stopping.")
                        break
                except Exception as e:
                    log.warning(f"Equity check failed, not tripping breaker: {e}")

                wait = seconds_until_next_scan()
                nxt = datetime.now(timezone.utc) + timedelta(seconds=wait)
                log.info(f"Next scan in {wait//3600}h {(wait%3600)//60}m (at {nxt.strftime('%Y-%m-%d %H:%M UTC')}) | positions {self.stats.count}/{100}")
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

    async def _equity(self, client) -> float:
        """Cash + open positions marked at current bid.

        get_balance() alone is cash only — it drops every time we buy,
        which is not a loss. Equity is what actually falls when we're wrong.
        """
        cash = await client.get_balance()
        mark = 0.0
        for p in await client.get_positions():
            pos = float(p.get("position_fp", 0) or 0)
            if pos == 0:
                continue
            t = p.get("ticker", "")
            try:
                m = await client.get_market(t)
                if not m:
                    raise ValueError("no market data")
                bid = float(m.get("yes_bid", 0) or 0) if pos > 0 \
                      else float(m.get("no_bid", 0) or 0)
                mark += bid * abs(pos)
            except Exception:
                # Can't price it — fall back to cost basis rather than zero,
                # which would fake a catastrophic loss.
                mark += float(p.get("market_exposure_dollars", 0) or 0)
        return round(cash + mark, 2)

    async def _process_settlements(self, client):
        """Drop settled tickers from local state so slots free up, AND record
        calibration data (prediction vs outcome) for each settled position.

        Slot P&L is still reconstructed by the dashboard from fills; the
        number logged here is only used for per-category calibration.
        """
        try:
            from calibration import record_settled
            raw = await client.get_settlements()
            settled_map = {s.get("ticker", ""): s for s in raw}
            gone = [t for t in self.stats.positions if t in settled_map]
            for t in gone:
                pos = self.stats.positions.get(t)
                s = settled_map[t]
                res = (s.get("market_result") or s.get("result") or "").lower()
                if pos and res in ("yes", "no") and getattr(pos, "pred_conf", 0.5) > 0.5:
                    won = (res == pos.side)
                    outcome = 1 if res == "yes" else 0
                    if won:
                        pnl = round((1.0 - pos.entry_price) * pos.contracts, 2)
                    else:
                        pnl = round(-pos.entry_price * pos.contracts, 2)
                    try:
                        record_settled(
                            category=pos.category,
                            source=f"reasoner conf={pos.pred_conf:.2f}",
                            ref_prob=pos.pred_prob,
                            market_prob=pos.entry_price,
                            outcome=outcome,
                            pnl=pnl,
                        )
                    except Exception as ce:
                        log.warning(f"  calibration log failed for {t}: {ce}")
                log.info(f"  SETTLED: {t} — freeing slot")
                del self.stats.positions[t]
            if gone:
                self._save_trade_log()
        except Exception as e:
            log.warning(f"Settlements: {e}")

    async def _manage_exits_OFF(self, client):
        """Auto-exit DISABLED — let positions ride to resolution."""
        if not self.stats.positions:
            return
        # Still log positions for visibility but never auto-sell
        import logging
        log = logging.getLogger(__name__)
        log.info(f"[POSITIONS] {len(self.stats.positions)} open, letting them resolve naturally")
        return

    async def _manage_exits(self, client):
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

        # Eviction DISABLED — force-selling losers to chase new bets realizes
        # your worst positions at market. A stop-loss cuts a loser on its own
        # merits; eviction dumps it just to free a slot. Never do that.
        if False and self.stats.count >= 100:
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
        # Sports/weather first (rich signal), then daily + fallback (thin signal, higher conf bar)
        sports_mkts = await client.get_series_markets(PRIORITY_SERIES, limit=30)
        other_mkts = await client.get_series_markets(DAILY_SERIES + FALLBACK_SERIES, limit=20)
        log.info(f"  {len(sports_mkts)} priority + {len(other_mkts)} daily/fallback markets")
        sports_mkts = sports_mkts + other_mkts

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
                min_h = in_progress_window(t)
                if game_h is None or game_h > 168 or game_h < min_h:
                    continue
            else:
                if h is None or h > 168:
                    continue
            # STRICT: max 1 position per event (no correlated duplicate bets)
            event = event_root(t)
            if any(event_root(tk) == event for tk in self.stats.positions):
                continue
            # STRICT: never re-bet exact same ticker while holding it
            if t in self.stats.positions:
                continue
            candidates.append(market)

        MAX_EVALS = 40  # ceiling on Claude calls per scan (~$0.40)
        candidates.sort(key=profit_score, reverse=True)
        if len(candidates) > MAX_EVALS:
            log.info(f"  {len(candidates)} passed hard filters — evaluating top {MAX_EVALS} by profit_score")
            candidates = candidates[:MAX_EVALS]
        else:
            log.info(f"  {len(candidates)} passed hard filters — evaluating with Claude...")

        # ═══ PHASE 2: Claude evaluation — collect all BUY recommendations ═══
        opportunities = []

        for market in candidates:
            self.stats.scanned += 1
            tu = market.ticker.upper()
            if any(x in tu for x in ["KXNBA","KXNHL","KXMLB","KXNFL","KXUCL","KXUEL","KXEPL",
                                      "KXLALIGA","KXSERIEA","KXBUNDESLIGA","KXLIGUE1","KXMLS",
                                      "KXWNBA","KXCFB","KXCBB","KXATP","KXWTA","KXPGA",
                                      "KXUFC","KXBOXING"]):
                category = "sports"
            elif any(x in tu for x in ["KXWTI","KXBRENT","KXOIL","KXHOIL","KXGOLD","KXSILVER","KXCOPPER","KXNATGAS","KXCORN","KXWHEAT"]):
                category = "commodities"
            elif any(x in tu for x in ["KXNASDAQ","KXINX","KXGOLDPRICE","NASDAQ100"]):
                category = "finance"
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
            min_conf, conf_reason = required_confidence(
                category, market.hours_until_close, market.ticker)
            if signal.confidence < min_conf:
                log.info(f"    ⊘ conf {signal.confidence:.0%} < {min_conf:.0%} required — {conf_reason}")
                self.stats.skipped += 1
                continue

            thresh = edge_threshold(market)
            # Fee drag: Kalshi's per-contract fee peaks at mid-price. Subtracting
            # it before the gate stops "wash trades" where fees eat the raw edge.
            fee = 0.07 * market.mid_price * (1.0 - market.mid_price)
            eff_thresh = thresh + fee
            if signal.action == "buy_yes" and signal.edge >= eff_thresh:
                side = "yes"
            elif signal.action == "buy_no" and signal.edge <= -eff_thresh:
                side = "no"
            else:
                self.stats.skipped += 1
                if signal.action in ("buy_yes", "buy_no"):
                    log.info(f"  fee-gate SKIP {market.ticker}: edge={signal.edge:+.3f} "
                             f"thresh={thresh:.3f} fee={fee:.3f} need={eff_thresh:.3f}")
                continue

            # Guarantee score = confidence × edge magnitude (higher = more guaranteed)
            opportunities.append({
                "market": market,
                "signal": signal,
                "side": side,
                "category": category,
                "score": signal.confidence * abs(signal.edge),
                "conf_gate": min_conf,
                "conf_reason": conf_reason,
            })
            log.info(f"  ✓ OPP: {market.ticker} {side.upper()} conf={signal.confidence:.0%} edge={signal.edge:+.2f} score={signal.confidence * abs(signal.edge):.3f}")

        log.info("")
        log.info(f"  {len(opportunities)} BUY opportunities identified")

        # ═══ PHASE 3: Deliberate — if more than slots available, take most guaranteed ═══
        if len(opportunities) > slots:
            log.info(f"  More opportunities than slots ({len(opportunities)} > {slots}) — ranking by confidence × edge...")
            # Capital-velocity ranking (IRR tiebreaker): among opportunities,
            # prefer faster-resolving trades so capital recycles and compounds.
            # This ONLY reorders ranking; it never blocks a trade. Edge quality
            # still dominates — a strong slow trade still outranks a weak fast one.
            def _velocity_score(o):
                hrs = getattr(o["market"], "hours_until_close", None) or 9999.0
                hrs = max(float(hrs), 1.0)
                velocity = 1.0 + (24.0 / hrs) * 0.5
                return o["score"] * velocity
            opportunities.sort(key=_velocity_score, reverse=True)
            opportunities = opportunities[:slots]
            log.info(f"  → Taking top {len(opportunities)} most guaranteed trades")

        # ═══ PHASE 4: Place trades ═══
        placed = 0
        for opp in opportunities:
            if self.stats.count >= 100:
                log.info("  Max positions reached — stopping")
                break
            if await self._place(client, opp["market"], opp["side"], opp["signal"], opp["category"]):
                placed += 1

        log.info("")
        log.info(f"  Scan complete: {placed}/{len(opportunities)} orders filled")

    async def _place(self, client, market: KalshiMarket, side: str,
                     signal: TradeSignal, category: str):
        # DEFENSE IN DEPTH: block duplicate event/ticker BEFORE placing any order
        _event = event_root(market.ticker)
        if any(event_root(tk) == _event for tk in self.stats.positions):
            log.info(f"  ⊘ SKIP {market.ticker} — already hold position on this event")
            return False
        if market.ticker in self.stats.positions:
            log.info(f"  ⊘ SKIP {market.ticker} — already hold this ticker")
            return False
        # Correlation cap: limit positions sharing one underlying risk factor.
        _cg = correlation_group(market.ticker)
        _cg_count = sum(1 for tk in self.stats.positions if correlation_group(tk) == _cg)
        if _cg_count >= MAX_PER_CORRELATION_GROUP:
            log.info(f"  ⊘ SKIP {market.ticker} — correlation group {_cg} "
                     f"at cap ({_cg_count}/{MAX_PER_CORRELATION_GROUP})")
            return False
        c = signal.confidence
        edge = abs(signal.edge)
        # Combine confidence + edge for sizing
        # Higher of either boosts the bet; both high = max bet
        if c >= 0.88 and edge >= 0.15:  frac, tier = 1.00, "ELITE"
        elif c >= 0.85:                 frac, tier = 0.85, "HIGH"
        elif c >= 0.80:                 frac, tier = 0.70, "SOLID"
        elif c >= 0.75:                 frac, tier = 0.55, "GOOD"
        else:                           frac, tier = 0.40, "MODERATE"

        # ═══ WEATHER ENSEMBLE OVERRIDE ═══
        # For weather, ignore Claude's signal and use multi-source forecast ensemble
        weather_cap = None
        if category == "weather":
            from fetchers.weather_ensemble import get_ensemble_forecast
            try:
                ens = await get_ensemble_forecast(market.ticker)
            except Exception as e:
                log.warning(f"  Weather ensemble failed for {market.ticker}: {e}")
                return False
            
            if not ens:
                log.info(f"  ⊘ WEATHER UNPARSEABLE {market.ticker}")
                return False
            
            rec = ens["recommendation"]
            if rec == "SKIP":
                log.info(f"  ⊘ WEATHER SKIP {market.ticker} — {ens['reason']}")
                return False
            
            # Override side based on ensemble (ignore Claude's opinion)
            ensemble_side = "yes" if rec == "BUY_YES" else "no"
            if ensemble_side != side:
                log.info(f"  ↻ WEATHER SIDE FLIP {market.ticker}: Claude said {side.upper()}, ensemble says {ensemble_side.upper()}")
                side = ensemble_side
            
            # Cap weather bets: $10 HIGH confidence, $5 MEDIUM
            weather_cap = 15.0 if ens["confidence"] == "HIGH" else 8.0  # caps tuned from settled data
            log.info(f"  🌡 WEATHER ENSEMBLE: {market.ticker} → {rec} ({ens['confidence']})")
            log.info(f"     Sources: {ens['source_results']}")
            log.info(f"     {ens['reason']}  cap=${weather_cap:.0f}")

        # ═══ CRYPTO MODEL OVERRIDE ═══
        # For daily BTC/ETH strike markets, price the digital from live spot +
        # realized vol instead of trusting Claude's guess. Skip 15-min/hourly
        # markets entirely (model invalid AND a twice-daily bot can't catch them).
        crypto_cap = None
        if category == "crypto":
            from fetchers.crypto_model import parse_crypto_ticker, evaluate as crypto_eval
            parsed = parse_crypto_ticker(market.ticker)
            if not parsed:
                log.info(f"  ⊘ CRYPTO SKIP {market.ticker} — not a daily strike market (model invalid)")
                return False
            try:
                import aiohttp as _aioh
                async with _aioh.ClientSession() as _cs:
                    cres = await crypto_eval(_cs, parsed["asset"], parsed["strike"], parsed["hours_left"])
            except Exception as e:
                log.warning(f"  Crypto model failed for {market.ticker}: {e}")
                return False
            if not cres:
                log.info(f"  ⊘ CRYPTO NO-DATA {market.ticker}")
                return False
            model_prob, spot, note = cres
            divergence = model_prob - market.mid_price
            # Enter only when model diverges from market by more than the gate.
            cthresh = edge_threshold(market)
            cfee = 0.07 * market.mid_price * (1.0 - market.mid_price)
            if abs(divergence) < (cthresh + cfee):
                log.info(f"  ⊘ CRYPTO SKIP {market.ticker}: {note} | "
                         f"div={divergence:+.3f} < need={cthresh + cfee:.3f}")
                return False
            crypto_side = "yes" if divergence > 0 else "no"
            if crypto_side != side:
                log.info(f"  ↻ CRYPTO SIDE FLIP {market.ticker}: Claude said {side.upper()}, model says {crypto_side.upper()}")
                side = crypto_side
            crypto_cap = 15.0
            log.info(f"  ₿ CRYPTO MODEL: {market.ticker} → {crypto_side.upper()} | {note} div={divergence:+.3f}")

        # ---- Kelly sizing -------------------------------------------------
        # Edge and price define the Kelly-optimal fraction of bankroll:
        #   f* = edge / odds, where odds = (1-price)/price for a YES-style bet.
        # We use quarter-Kelly and cap hard. A coin-flip (tiny edge) now gets
        # a tiny bet; only a real mispricing gets size. This is the fix for
        # the flat-$40-on-everything problem that produced the -$124 ATP loss.
        # price computed before sizing (fix UnboundLocalError)
        price = (market.yes_ask if side == "yes" else market.no_ask)
        if price <= 0:
            price = market.mid_price if side == "yes" else round(1 - market.mid_price, 4)
        if price <= 0:
            log.warning(f"  No valid price for {market.ticker}"); return False

        try:
            bankroll = await client.get_balance()
        except Exception:
            bankroll = 100.0

        edge_abs = abs(signal.edge)              # how mispriced, in probability
        p = max(0.01, min(0.99, price))          # entry price for the side we take
        odds = (1.0 - p) / p                      # payout per $1 risked
        kelly_f = (edge_abs / odds) if odds > 0 else 0.0
        kelly_f = max(0.0, min(kelly_f, 0.25))    # never risk >25% even at full Kelly
        QUARTER = 0.25
        bet = round(bankroll * QUARTER * kelly_f, 2)

        # Confidence gate on top: below 0.60 conviction, size down further
        if signal.confidence < 0.70:
            bet *= 0.5

        # Absolute guardrails
        bet = max(0.0, min(bet, 25.0))            # hard ceiling per position
        if weather_cap is not None:
            bet = min(bet, weather_cap)
        if crypto_cap is not None:
            bet = min(bet, crypto_cap)
        if category in ("econ", "politics", "crypto"):
            bet = min(bet, 10.0)
        if category == "sports":
            bet = min(bet, 5.0)                    # sports is loss center: cap tight
        if category in ("commodities", "finance"):
            bet = min(bet, 5.0)                    # no pricing model yet: cap tight
        if bet < 1.0:
            log.info(f"  ⊘ SKIP {market.ticker} — Kelly bet ${bet:.2f} below $1 floor "
                     f"(edge={edge_abs:.3f} conf={signal.confidence:.0%})")
            return False
        log.info(f"  SIZING {market.ticker}: edge={edge_abs:.3f} kelly_f={kelly_f:.3f} "
                 f"bankroll=${bankroll:.0f} → bet ${bet:.2f}")
        # WEATHER DISCIPLINE: only high-probability outcomes, cap at $10
        # Block "10x longshots" — anything priced ≤ 15c on weather is too speculative
        if category == "weather":
            _check_price = market.yes_ask if side == "yes" else market.no_ask
            if _check_price <= 0.15:
                log.info(f"  ⊘ WEATHER LONGSHOT BLOCKED {market.ticker} @ ${_check_price:.2f} (need >15c for weather)")
                return False
            bet = min(bet, 15.0)
            log.info(f"  Weather discipline: bet capped at ${bet:.2f}")

        count = max(1, int(bet / price))
        cost  = round(price * count, 2)
        game_flag = is_game(market.title)

        log.info(f"  Confidence {c:.0%} ({tier}) → ${bet:.2f}")

        # ── PROBATION GATE ──────────────────────────────────────────────
        # commodities/finance have no pricing model yet and the reasoner
        # shows a systematic all-NO bias. Log the signal so the scorecard
        # grades it on settlement, but do NOT risk real money until the
        # data proves the category. Same discipline weather went through.
        if category in ("commodities", "finance"):
            log.info(f"  ⊘ PROBATION (log-only) {market.ticker} — {category} not yet live; signal recorded")
            self._log_signal(market, side, signal, category, price, count, cost)
            return False

        if self.config.dry_run:
            log.info(f"  [DRY RUN] BUY {side.upper()} {market.ticker} | {count}x@{price:.0%} | ${cost:.2f}")
        else:
            result = await client.place_order(market.ticker, side, price, count)
            if not result: log.error("  Order failed"); return False
            log.info(f"  ✓ BUY {side.upper()} {market.ticker} | {count}x@{price:.0%} | ${cost:.2f} | id={result.order_id}")

        self.stats.positions[market.ticker] = Position(
            ticker=market.ticker, title=market.title[:80], side=side,
            entry_price=price, contracts=count, cost=cost,
            entry_time=datetime.now(timezone.utc).isoformat(),
            category=category, timeframe=market.timeframe_label,
            is_game=game_flag,
            pred_prob=getattr(signal, "estimated_prob", signal.confidence),
            pred_conf=signal.confidence,
        )
        self.stats.placed += 1
        self._save_trade_log()
        self._log_signal(market, side, signal, category, price, count, cost)
        return True

    def _log_signal(self, market, side, signal, category, price, count, cost):
        """Record what Claude believed at entry.

        Kalshi remembers the trade. Nothing remembers the reasoning behind
        it, so it has to be written down now or it's gone. The dashboard
        joins this against settlements to answer: does 78% confidence
        actually resolve yes 78% of the time?
        """
        try:
            try:
                with open(SIGNAL_LOG_PATH) as f:
                    rows = json.load(f)
            except Exception:
                rows = []
            def _safe_round(v, n=4):
                # Weather/crypto overrides can leave signal fields as None.
                # Record what we have rather than losing the whole signal.
                try:
                    return round(float(v), n)
                except (TypeError, ValueError):
                    return None
            rows.append({
                "ticker": market.ticker,
                "title": market.title[:100],
                "category": category,
                "side": side,
                "confidence": _safe_round(getattr(signal, "confidence", None)),
                "estimated_prob": _safe_round(getattr(signal, "estimated_prob", None)),
                "kalshi_mid": _safe_round(getattr(signal, "kalshi_mid", None)),
                "edge": _safe_round(getattr(signal, "edge", None)),
                "entry_price": _safe_round(price),
                "contracts": int(count),
                "cost": round(cost, 2),
                "entry_time": datetime.now(timezone.utc).isoformat(),
            })
            with open(SIGNAL_LOG_PATH, "w") as f:
                json.dump(rows, f, indent=2)
            # Also emit to stdout. Render's disk is ephemeral; its logs are not.
            # Copy these lines into the dashboard's import box.
            log.info("SIGNAL " + json.dumps(rows[-1], separators=(",", ":")))
        except Exception as e:
            log.warning(f"signal_log: {e}")

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
        log.info(f"  P&L tracked in dashboard (localhost:8080), not here")
        log.info(f"────────────────────────────────────────────────────\n")
