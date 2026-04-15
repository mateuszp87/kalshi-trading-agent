"""Claude scoring engine — game-market focused."""

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


SYSTEM_PROMPT = """You are a sharp prediction market trader on Kalshi focused on GAME markets.

PRIMARY TARGETS (same-day, resolve fast):
- NBA game winners, half winners, quarter winners
- NHL game winners
- MLB game winners, first 5 innings
- Champions League / Premier League game winners
- NBA/NHL playoff series winners and spreads
- NBA player points props

CURRENT CONTEXT (April 2026):

NBA PLAYOFFS:
- Portland Trail Blazers vs Phoenix Suns  — Suns ~68% favorite
- Orlando Magic vs Philadelphia 76ers     — Philly ~53% at home
- Golden State Warriors vs LA Clippers   — Warriors ~65%
- Toronto Raptors vs Cleveland Cavaliers  — Cleveland ~75%
- Atlanta Hawks vs New York Knicks        — Knicks ~60%
Home court in NBA playoffs: ~58%. Half-winner matches full-game winner ~75%.

NHL PLAYOFFS:
- Detroit Red Wings vs Florida Panthers   — Florida ~68%
- Dallas Stars vs Buffalo Sabres          — Stars ~52%
- Toronto Maple Leafs vs Ottawa Senators  — Leafs ~62%
- Seattle Kraken vs Vegas Golden Knights  — Vegas ~60%
- Philadelphia Flyers vs Pittsburgh       — Flyers ~55%
Home court in NHL: ~55%.

CHAMPIONS LEAGUE SEMIS (Apr 29):
- Bayern Munich vs Real Madrid  — Real Madrid ~58% to advance
- Arsenal vs Sporting CP        — Arsenal ~75%

MLB (April, early season):
- Small sample, use Vegas line. Home team ~54%.

CRYPTO (April 15 2026):
- BTC ~$74k, ETH ~$2330. Fear & Greed 21 (Extreme Fear).
- Daily markets: compare current price vs strike precisely.

WEATHER:
- NYC Apr 15: forecast ~85F. Record = 87F (1941). >90F impossible.

SCOTUS ALITO: ~60% retirement this year. Market at 71% YES is reasonable — do NOT buy NO.

RULES:
- Multi-leg parlays: true probability = product of each leg. "yes Team A, yes Player B 20+" at 50% is almost always overpriced.
- Wide spreads (>30 cents bid-ask) = low liquidity = skip unless very confident.
- If signals don't match market, say so and use base rates only.
- Never anchor to Kalshi price — form independent estimate first.

EDGE THRESHOLDS (enforced by agent):
- Same-day game:  4 cents minimum
- This week:      6 cents minimum
- This month:     8 cents + confidence >70%
- Long-term:     12 cents + confidence >80%

SIZING (report your confidence, agent handles sizing):
- 85%+  → $5.00  | 70-85% → $3.50  | 55-70% → $2.50  | <55% → $1.50

Respond ONLY with valid JSON — no markdown, no preamble:
{
  "estimated_prob": <float 0.0-1.0>,
  "action": "<buy_yes|buy_no|skip>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<2-3 sentences>",
  "factor_scores": {"<factor>": <float>}
}"""


class ClaudeReasoner:
    def __init__(self, api_key: str, model: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def score_market(self, market: KalshiMarket, signals: dict, category: str) -> Optional[TradeSignal]:
        h = market.hours_until_close
        prompt = f"""MARKET: {market.title}
TICKER: {market.ticker}
CATEGORY: {category}
TIMEFRAME: {market.timeframe_label} ({f'{h:.1f}h' if h else '?'} until close)
PRICE: YES bid={market.yes_bid:.2f} ask={market.yes_ask:.2f} mid={market.mid_price:.2f}
VOLUME: {market.volume:,} | CLOSES: {market.close_time}

SIGNALS:
{self._fmt(signals)}

Estimate true YES probability. Recommend trade if edge meets threshold for {market.timeframe_label}."""

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
            data = json.loads(raw)
            ep = float(data["estimated_prob"])
            return TradeSignal(
                ticker=market.ticker, title=market.title,
                action=data.get("action", "skip"),
                confidence=float(data.get("confidence", 0.5)),
                estimated_prob=ep,
                kalshi_mid=market.mid_price,
                edge=round(ep - market.mid_price, 4),
                reasoning=data.get("reasoning", ""),
                factor_scores=data.get("factor_scores", {}),
            )
        except Exception as e:
            log.error(f"Claude failed {market.ticker}: {e}")
            return None

    def _fmt(self, signals: dict) -> str:
        if not signals:
            return "  None — use base rates and market title only."
        lines = []
        for k, v in signals.items():
            line = f"  [{k}] value={v.get('value','?')} | {v.get('description','')}"
            if v.get("raw"): line += f"\n    raw: {str(v['raw'])[:200]}"
            lines.append(line)
        return "\n".join(lines)
