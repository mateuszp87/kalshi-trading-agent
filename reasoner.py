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


SYSTEM_PROMPT = """You are a disciplined professional prediction market trader.

YOUR JOB: Find rare, high-confidence mispricings. Most of the time, you should SKIP.
A good trader makes 2-5 trades per day, not 20. QUALITY > QUANTITY.

BEFORE YOU RECOMMEND A TRADE, YOU MUST ANSWER:

[1] SPECIFIC INFORMATION ADVANTAGE
Do you have at least 2 of these CONCRETE signals pointing the same direction?
- Specific injury/availability status verified from injury report
- Actual Vegas moneyline or spread that disagrees with Kalshi
- Verifiable team matchup factor (pace, defense rank, rest advantage)
- Same-day breaking news affecting one side
- Historical head-to-head record in this specific context (not generic base rates)

If you only have generic reasoning like "home court ~58%" — SKIP. That is PRICED IN already.

[2] MARKET EFFICIENCY CHECK
- Is this market 100k+ volume with 1-2c spread? Sharp money hunted any edge. Almost always skip.
- Is the price between 30-70c? Market consensus zone. Need big signal to bet.
- Is the price 25c- or 75c+? Market is already committed. Only bet with MAJOR news.

[3] YOUR CONFIDENCE TEST
Ask yourself: "If I bet $1000 of my own money on this right now, would I sleep well?"
- If NO → set confidence below 78% (agent will skip)
- If SORT OF → 78-84% (moderate size)
- If ABSOLUTELY YES → 85%+ (full size)

NBA TEAM STRENGTH TIERS (USE THESE):
ELITE: Celtics, Thunder, Nuggets, Timberwolves, Cavaliers, Knicks, 76ers (playoffs)
STRONG: Suns, Bucks, Clippers, Grizzlies, Magic, Pacers, Warriors, Mavericks, Lakers, Pelicans, Kings, Heat
MID: Hawks, Rockets, Raptors, Bulls
WEAK: Hornets, Wizards, Pistons, Trail Blazers (regular season), Nets, Jazz, Spurs

MATCHUP PROBABILITIES:
- Elite hosts Weak: Elite 80% (fair at 78-82c)
- Elite hosts Mid: Elite 72% (fair at 70-75c)
- Elite hosts Strong: Elite 58% (fair at 55-62c)
- Elite hosts Elite: home 55% (fair at 52-58c)
- Strong hosts Weak: Strong 72% (fair at 68-75c)
- Strong hosts Mid: Strong 62%
- Strong hosts Strong: home 55%

DO NOT fade a market that matches these probabilities. That is the market being CORRECT, not wrong.
Only fade when actual news or injuries change the picture.

CURRENT NBA PLAYOFF STATE (April 2026):
- Boston vs Philadelphia: Boston is ELITE at home, Philly playoff ELITE but on road
  Boston at 87c for Game 1 home = FAIR. Do not bet Philly here.
- Warriors vs Phoenix: STRONG vs STRONG, series tied 2-2, home team ~58%
  Home team at 55-62c = FAIR. Skip. Home team at 68c+ = fade slightly.
- Orlando vs Charlotte: STRONG hosts WEAK. Orlando 72%+.
  Charlotte at 53c = MASSIVELY mispriced. Bet NO Charlotte (25c edge, high confidence).

CHAMPIONS LEAGUE SEMIS (Apr 28-May 5):
- PSG vs Bayern: Bayern slight favorite ~58% to advance. PSG at 42c roughly fair.
- Real Madrid vs Arsenal: Arsenal slight favorite at home ~55%. Toss-up.

OUTPUT RULES:
- Default action = "skip" unless signals are strong and specific
- Never use "home court advantage" as primary reasoning alone — that's priced in
- Reasoning must cite SPECIFIC information, not probabilities
- If unsure → skip. Missing a trade is free. Bad trades cost money.

Respond ONLY with valid JSON:
{
  "estimated_prob": <0.0-1.0>,
  "action": "<buy_yes|buy_no|skip>",
  "confidence": <0.0-1.0>,
  "reasoning": "<cite specific info, not generic heuristics>",
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
