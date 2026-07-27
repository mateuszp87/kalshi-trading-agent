"""Claude scoring engine — profit-first game trader."""

import json, logging
import anthropic
from dataclasses import dataclass
from typing import Optional
from kalshi_client import KalshiMarket

log = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    ticker: str
    title: str
    action: str
    confidence: float
    estimated_prob: float
    kalshi_mid: float
    edge: float
    reasoning: str
    factor_scores: dict


SYSTEM_PROMPT = """You are a sharp Kalshi prediction market trader.

GOAL: Find 2-5 high-confidence mispricings per scan. Make money. Not paralyzed by perfection.

═══ LIVE GAME INTELLIGENCE (NEW, HIGHEST PRIORITY) ═══
When LIVE GAME data is provided, it means the game is IN PROGRESS. This is your biggest edge:
- Score + time remaining + current spread tells you if the market is mispriced
- Example: Market says OKC -5.5 at 62c YES, but OKC is up 12 in Q4 with 4 min left → EASY yes bet
- Example: Market says "OKC wins" at 78c YES, but OKC is down 8 at halftime → fade, market is slow to adjust
- SCORE MARGIN + TIME REMAINING > everything else. Pregame tiers/stats are irrelevant once the ball tips.
- When no live data is provided, the game hasn't started or isn't trackable — use pregame analysis.

═══ ORDER BOOK INTELLIGENCE (NEW) ═══
The "ORDER BOOK" signal shows where real money is currently stacked on each side:
- Imbalance > +0.4: Strong money on YES. If you agree → confirming. If you disagree → fade opportunity.
- Imbalance < -0.4: Strong money on NO. Same logic.
- Conviction < 0.3: Stale book, discount the signal (maybe off-hours or low-interest market)
- Conviction > 0.5: Active book, take the signal seriously
- Top of book prices show the true market mid. Use this as a sanity check vs the bid/ask we show.

═══ HOW TO READ SIGNALS ═══
Signals provided may be partially irrelevant (e.g., NBA injury data for MMA fights). IGNORE mismatched signals and focus on:
- Market price (the anchor)
- Time until close (short = more info baked in)
- Volume (high = efficient, low = potentially exploitable)
- Your domain knowledge of the specific event

═══ SPORTS — TIER MATCHUP RULES ═══

NBA TIERS (2025-26):
- ELITE: Celtics, Thunder, Nuggets, Timberwolves, Cavs, Knicks, 76ers (playoffs)
- STRONG: Suns, Bucks, Clippers, Grizzlies, Magic, Pacers, Warriors, Mavs, Lakers, Pelicans, Kings, Heat
- MID: Hawks, Rockets, Raptors, Bulls
- WEAK: Hornets, Wizards, Pistons, Blazers, Nets, Jazz, Spurs

NBA PROBABILITIES (home team listed first):
- Elite host Weak: 80% | Elite host Mid: 72% | Elite host Strong: 58% | Elite host Elite: 55%
- Strong host Weak: 72% | Strong host Mid: 62% | Strong host Strong: 55%
- Mid host Weak: 65% | Weak host Strong: 32% | Weak host Elite: 22%

SOCCER (UCL/EPL/LaLiga/SerieA/Bundesliga):
ELITE: Man City, Real Madrid, Bayern, Arsenal, PSG, Barcelona, Liverpool, Inter
STRONG: Chelsea, Man Utd, Tottenham, Atletico, Juventus, Milan, Dortmund, Leverkusen
- Elite host Strong: 55-60% | Elite host Mid: 68-72% | Elite host Weak: 78-82%

MLB (April - pitcher is 60% of signal):
- Home team baseline: 54%
- Ace vs rookie: add 10%
- Back-to-back travel fatigue: subtract 5%

═══ CURRENT STATE (April 18, 2026) ═══

NBA PLAYOFFS: (Zig-Zag Theory: Favor TOR +8.5 and MIN +6.5 as market overreacted to G1)
- Boston dominant vs Philadelphia
- Warriors-Phoenix tied 2-2, home team ~58%
- Orlando STRONG hosts Charlotte WEAK: Orlando 72%+ = FAIR at 70-75c
- Cleveland closing out Toronto
- Knicks vs Atlanta (Knicks favored)

UCL SEMIS (Apr 28-May 5):
- PSG vs Bayern: Bayern slight edge ~55-58%
- Real Madrid vs Arsenal: Arsenal ~52-55% home

MLB: Early season, home team 52-55% baseline

═══ COMBAT SPORTS — VERY STRICT ═══
MMA/UFC/Boxing markets are efficient. DEFAULT = SKIP.
- Favorite at 65c+ = usually right. DO NOT fade without MAJOR news.
- Underdog at 25c- = there for a reason.
- Only bet when you have specific style/injury/camp info, NOT vibes.

═══ CRYPTO ═══
BTC ~$77k, ETH ~$2.4k, Fear & Greed: 21.
Only trade if strike within ±3% of spot AND volume >5k AND 2-20 hrs left.

═══ WEATHER ═══
Same-day temp markets after 3pm local = resolved, SKIP.
Next-day markets: only trade if NWS forecast differs from market by 5°+.

═══ WHAT TO DO ═══

For EACH market:
1. Identify the type (NBA game? MMA? Crypto? Weather?)
2. Apply the specific rule for that type
3. Compare your fair value to market price
4. If edge >= 8¢ AND you have concrete reason → BUY
5. If edge < 8¢ OR vague reason → SKIP
6. If market aligns with expected probability → SKIP (respect market)

═══ REASONING REQUIREMENTS ═══
- Name the specific teams/fighters/event
- State their tier or ranking if sports
- Give your estimated probability
- Explain the edge in one sentence
- Skip reasoning starting with "home court advantage" or "usually" without specifics

═══ CONFIDENCE SCALE ═══
- 85%+: Clear tier mismatch OR breaking news (star injury)
- 70-80%: Solid edge from tier matchup + additional factor
- 72-78%: Good situational edge
- <72%: Skip (agent auto-rejects)

═══ OUTPUT ═══
Respond ONLY with valid JSON:
{
  "estimated_prob": <0.0-1.0>,
  "action": "<buy_yes|buy_no|skip>",
  "confidence": <0.0-1.0>,
  "reasoning": "<specific, cite tier + rule + factors>",
  "factor_scores": {"<factor>": <0.0-1.0>}
}"""


class ClaudeReasoner:
    def __init__(self, api_key: str, model: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def score_market(self, market: KalshiMarket, signals: dict, category: str) -> Optional[TradeSignal]:
        h = market.hours_until_close
        spread = round(market.yes_ask - market.yes_bid, 3) if market.yes_bid and market.yes_ask else '?'
        prompt = f"""MARKET: {market.title}
TICKER: {market.ticker}
TIMEFRAME: {market.timeframe_label} ({f'{h:.0f}h' if h else '?'} left)
PRICE: bid={market.yes_bid:.2f} ask={market.yes_ask:.2f} mid={market.mid_price:.2f} spread={spread}
VOLUME: {market.volume:,}

SIGNALS:
{self._fmt(signals)}

Find the edge. What is the true YES probability? Is the market mispriced?"""

        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Response truncated mid-string. The fields we need (action,
                # confidence, estimated_prob) come before the long `reasoning`
                # field, so pull them out with a targeted match rather than
                # discarding the whole evaluation.
                import re as _re
                def _grab(key, cast):
                    m = _re.search(rf'"{key}"\s*:\s*"?([^",}}\s]+)"?', raw)
                    return cast(m.group(1)) if m else None
                ep_v   = _grab("estimated_prob", float)
                conf_v = _grab("confidence", float)
                act_v  = _grab("action", str)
                if ep_v is None or conf_v is None or act_v is None:
                    raise
                log.info(f"  ↻ salvaged truncated JSON for {market.ticker}")
                data = {"estimated_prob": ep_v, "confidence": conf_v,
                        "action": act_v, "reasoning": "(truncated)",
                        "factor_scores": {}}
            ep    = float(data["estimated_prob"])
            return TradeSignal(
                ticker=market.ticker, title=market.title,
                action=data.get("action", "skip"),
                confidence=float(data.get("confidence", 0.5)),
                estimated_prob=ep, kalshi_mid=market.mid_price,
                edge=round(ep - market.mid_price, 4),
                reasoning=data.get("reasoning", ""),
                factor_scores=data.get("factor_scores", {}),
            )
        except Exception as e:
            # Empty response / JSON parse errors are common for low-data markets.
            # Downgrade to debug level to keep logs clean.
            if "Expecting value" in str(e) or "line 1 column 1" in str(e):
                log.debug(f"Claude returned empty for {market.ticker}")
            else:
                log.warning(f"Claude failed {market.ticker}: {e}")
            return None

    def _fmt(self, signals: dict) -> str:
        if not signals:
            return "  None available — use base rates from context above."
        lines = []
        if "orderbook" in signals:
            ob = signals["orderbook"]
            imb = ob.get("imbalance", 0)
            conv = ob.get("conviction_score", 0)
            yes_total = ob.get("yes_dollars_total", 0)
            no_total = ob.get("no_dollars_total", 0)
            yes_depth = ob.get("yes_depth_3c", 0)
            no_depth = ob.get("no_depth_3c", 0)
            if imb > 0.4: bias = "STRONGLY YES-heavy"
            elif imb > 0.15: bias = "moderately YES-heavy"
            elif imb < -0.4: bias = "STRONGLY NO-heavy"
            elif imb < -0.15: bias = "moderately NO-heavy"
            else: bias = "balanced"
            conv_label = "high" if conv > 0.5 else ("moderate" if conv > 0.3 else "low")
            lines.append(f"  ORDER BOOK: ${yes_total:,.0f} YES vs ${no_total:,.0f} NO ({bias})")
            lines.append(f"  Depth within 3c: ${yes_depth:,.0f} YES | ${no_depth:,.0f} NO")
            lines.append(f"  Conviction: {conv:.2f} ({conv_label})")
        
        # LIVE GAME — the highest-value signal for late-game markets
        if "live_game" in signals:
            lg = signals["live_game"]
            if lg.get("is_live"):
                home = lg.get("home", "?")
                away = lg.get("away", "?")
                hs = lg.get("home_score", 0)
                as_ = lg.get("away_score", 0)
                detail = lg.get("detail", "")
                diff = hs - as_
                winning = home if diff > 0 else (away if diff < 0 else "TIED")
                margin = abs(diff)
                lines.append(f"  LIVE GAME: {away} {as_} @ {home} {hs} — {detail}")
                if winning == "TIED":
                    lines.append(f"  STATUS: Game is tied, {detail}")
                else:
                    lines.append(f"  STATUS: {winning} leading by {margin}, {detail}")
            elif lg.get("is_final"):
                lines.append(f"  FINAL: {lg.get('away')} {lg.get('away_score')} @ {lg.get('home')} {lg.get('home_score')}")
            else:
                lines.append(f"  PREGAME: {lg.get('away')} @ {lg.get('home')} — {lg.get('detail','')}")
        
        for k, v in signals.items():
            if k in ("orderbook", "live_game"):
                continue
            try:
                if isinstance(v, dict):
                    val = v.get("value") or v.get("prob") or v.get("count") or ""
                    desc = v.get("description") or v.get("agreement") or ""
                    if val or desc:
                        lines.append(f"  [{k}] {val} | {desc}".strip(" |"))
                    else:
                        lines.append(f"  [{k}] {v}")
                elif isinstance(v, (list, tuple)):
                    lines.append(f"  [{k}] {len(v)} items")
                else:
                    lines.append(f"  [{k}] {v}")
            except Exception:
                lines.append(f"  [{k}] (error rendering)")
        return "\n".join(lines) if lines else "  None available."

