"""
Claude reasoning engine — scores each market against collected signals.
Returns a composite probability estimate and trade recommendation.
"""

import json
import logging
import anthropic
from dataclasses import dataclass
from typing import Optional
from kalshi_client import KalshiMarket

log = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    ticker: str
    title: str
    action: str             # "buy_yes" | "buy_no" | "skip"
    confidence: float       # 0.0 – 1.0
    estimated_prob: float   # Claude's probability estimate for YES
    kalshi_mid: float       # current Kalshi mid price
    edge: float             # estimated_prob - kalshi_mid (positive = YES has edge)
    reasoning: str
    factor_scores: dict[str, float]


SYSTEM_PROMPT = """You are a quantitative prediction market analyst specializing in Kalshi markets.

Your job: given a market question and a set of real-time signals (data from external APIs), 
estimate the true probability that the YES outcome occurs, and determine whether there is 
a tradeable edge vs. the current Kalshi market price.

Rules:
- Be rigorous and calibrated. Do not be overconfident.
- Weight signals by their reliability and recency.
- Account for base rates and historical accuracy of each signal type.
- If signals conflict, explain why and discount both.
- Your probability estimate should be your honest best guess, NOT anchored to the market price.
- Only recommend a trade when |estimated_prob - market_price| > 0.05 (5 cent edge minimum).

Respond ONLY with valid JSON in this exact format:
{
  "estimated_prob": <float 0.0-1.0>,
  "action": "<buy_yes|buy_no|skip>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<2-3 sentence explanation>",
  "factor_scores": {
    "<factor_name>": <float 0.0-1.0>,
    ...
  }
}"""


class ClaudeReasoner:
    def __init__(self, api_key: str, model: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def score_market(
        self,
        market: KalshiMarket,
        signals: dict,
        category: str,
    ) -> Optional[TradeSignal]:
        """
        Ask Claude to evaluate a market given collected signals.
        signals: dict of {factor_name: {value, description, raw_data}}
        """
        signals_text = self._format_signals(signals)

        user_prompt = f"""
MARKET: {market.title}
TICKER: {market.ticker}
CATEGORY: {category}
CURRENT KALSHI PRICE: YES={market.yes_bid:.2f} bid / {market.yes_ask:.2f} ask (mid={market.mid_price:.2f})
VOLUME: {market.volume:,} contracts | OPEN INTEREST: {market.open_interest:,}
CLOSES: {market.close_time}

SIGNALS FROM EXTERNAL DATA SOURCES:
{signals_text}

Based on these signals, estimate the true probability that YES resolves correctly.
Compare to the Kalshi mid price ({market.mid_price:.2f}) and recommend a trade if edge > 5%.
"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)

            estimated_prob = float(data["estimated_prob"])
            action = data.get("action", "skip")
            edge = round(estimated_prob - market.mid_price, 4)

            return TradeSignal(
                ticker=market.ticker,
                title=market.title,
                action=action,
                confidence=float(data.get("confidence", 0.5)),
                estimated_prob=estimated_prob,
                kalshi_mid=market.mid_price,
                edge=edge,
                reasoning=data.get("reasoning", ""),
                factor_scores=data.get("factor_scores", {}),
            )

        except (json.JSONDecodeError, KeyError, Exception) as e:
            log.error(f"Claude scoring failed for {market.ticker}: {e}")
            return None

    def _format_signals(self, signals: dict) -> str:
        lines = []
        for factor, info in signals.items():
            val = info.get("value", "N/A")
            desc = info.get("description", "")
            raw = info.get("raw", "")
            line = f"  [{factor}] value={val} | {desc}"
            if raw:
                line += f"\n    raw: {str(raw)[:200]}"
            lines.append(line)
        return "\n".join(lines) if lines else "  No signals available."
