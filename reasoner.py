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
                model=self.model, max_tokens=400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
            data  = json.loads(raw)
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
            log.error(f"Claude failed {market.ticker}: {e}")
            return None

    def _fmt(self, signals: dict) -> str:
        if not signals: return "  None available — use base rates from context above."
        return "\n".join(
            f"  [{k}] {v.get('value','?')} | {v.get('description','')}"
            + (f"\n    {str(v.get('raw',''))[:200]}" if v.get('raw') else "")
            for k, v in signals.items()
        )
