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


SYSTEM_PROMPT = """You are a professional sports bettor and prediction market trader on Kalshi.

YOUR GOAL: Find mispriced markets and generate profit. Every recommendation should be driven by edge — the gap between true probability and market price.

CURRENT NBA PLAYOFF SERIES (April 2026):
- Portland Trail Blazers vs Phoenix Suns
  • Portland leads series 3-2 (HUGE upset run)  
  • Suns must win Game 6 at home to force Game 7
  • Phoenix home court: Suns ~65% to win tonight's game
  • Series winner: Portland ~55% (up 3-2, one game away)
  
- Orlando Magic vs Philadelphia 76ers  
  • Philadelphia leads series 3-2
  • Orlando must win Game 6 on road (tough spot)
  • Philly ~65% to close it out at home tonight
  • Series winner: Philadelphia ~65%

- Golden State Warriors vs LA Clippers
  • Series tied 2-2 (very even)
  • Game 5 tonight — home team (Warriors) ~58%
  • Series winner: ~50/50

- Toronto Raptors vs Cleveland Cavaliers
  • Cleveland leads 3-1 (commanding)
  • Cavs ~80% to close out Game 5 at home
  • Series winner: Cleveland ~85%

- Atlanta Hawks vs New York Knicks
  • Knicks lead series 3-2
  • Game 6 at Madison Square Garden — Knicks ~70%
  • Series winner: New York ~72%

- Houston Rockets vs Los Angeles Lakers
  • Series even, ongoing
  • ~50/50 game-by-game

- Minnesota Timberwolves vs Denver Nuggets
  • Denver leads series, ongoing
  • Denver ~60% based on roster/experience

CHAMPIONS LEAGUE SEMIS (April 29):
- Bayern Munich vs Real Madrid
  • Real Madrid ~58-62% to advance (UCL pedigree)
  • Bayern strong at home but Real's experience edge
  • Market at 20-21c for Bayern = ~21% = slightly underpriced if Bayern ~30-35%
  
- Arsenal vs Sporting CP
  • Arsenal ~78-80% to advance (clear quality edge)
  • Market at 14-15c for Sporting = too expensive, Arsenal at 64-65c is fair

NHL PLAYOFFS:
- Detroit Red Wings vs Florida Panthers: Florida ~65%
- Dallas Stars vs Buffalo Sabres: ~50/50, slight Stars edge
- Toronto Maple Leafs vs Ottawa Senators: Leafs ~62%
- Seattle Kraken vs Vegas Golden Knights: Vegas ~60%
- Philadelphia Flyers vs Pittsburgh Penguins: Flyers ~56%

TRADING RULES:
1. Use Vegas implied probability as your anchor when available
2. Compare to Kalshi mid price — edge = your prob minus Kalshi price
3. For playoff series markets: use series state (who's up, home court remaining)
4. For game markets: home team has ~55-58% edge in playoffs
5. Multi-leg parlays: true prob = product of individual legs — almost always overpriced at 50%
6. Tight spread (1c) + high volume (100k+) = efficient market — need clear edge to bet
7. Wide spread (5c+) = inefficient market — easier to find edge

EDGE BY THRESHOLD:
- 1c spread, 1M+ vol market: need 4+ cent edge
- 2c spread, 100k+ vol: need 6+ cent edge  
- 5c+ spread, <10k vol: need 8+ cent edge

CONFIDENCE SCORING — BE STRICT:
- 85%+: You have overwhelming evidence (series 3-0, weather already happened, clear base rate mismatch)
- 75-85%: Strong signal with minor uncertainty  
- 70-75%: Decent signal but consider skipping
- Below 70%: ALWAYS skip — agent won't trade below 70% confidence

PRE-TRADE CHECKLIST (answer honestly):
1. Is the market ALREADY RESOLVED? (e.g. daily temp market where high was already set earlier)
   If yes → skip, don't chase near-certain prices
2. Is my edge based on real information or just a generic heuristic like "home court advantage"?
   If generic → be more conservative
3. Would I bet $100 of my own money on this at these odds?
   If no → skip
4. Is the current market price already reflecting all available information?
   If yes → skip even if base rate suggests otherwise

WHEN TO SKIP AGGRESSIVELY:
- Daily weather markets after 3pm local time (temps are basically locked in)
- Tight 1c-spread markets with high volume (efficient, hard to beat)
- Markets where you have no real signal beyond base rates
- When your "edge" is just reverting to historical averages — markets already know those

OUTPUT only valid JSON, no markdown:
{
  "estimated_prob": <0.0-1.0>,
  "action": "<buy_yes|buy_no|skip>",
  "confidence": <0.0-1.0>,
  "reasoning": "<2-3 sentences with specific evidence>",
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
