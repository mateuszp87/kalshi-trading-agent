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
- Only recommend a trade when |estimated_prob - market_price| > 0.04 (4 cent edge minimum).

CRITICAL CURRENT EVENTS (April 2026) — use these as strong priors:
- SCOTUS Alito (KXSCOTUSRESIGN-29-SA): HIGH retirement probability ~50-70%. Book releasing Oct 2026 day after term starts, wife wants him out, had hospital visit, Senate GOP actively preparing for vacancy. Kalshi at 71% YES is REASONABLE — do NOT buy NO here.
- SCOTUS Thomas (KXSCOTUSRESIGN-29-CT): LOW ~15-20%. No retirement signals, ideologically committed.
- SCOTUS Roberts (KXSCOTUSRESIGN-29-JR): LOW ~8-12%. Chief Justice, no signals.
- SCOTUS Sotomayor (KXSCOTUSRESIGN-29-SS): VERY LOW ~5%. Would not resign under Trump.
- NYC April 15 temperature: Average high is 58F. >85F is very unusual. >90F essentially impossible in April. Markets pricing these high are OVERPRICED — buy NO.
- CPI trends: Currently 3.29% YoY, tariff impacts may push higher in coming months.

For NBA/MLB championship markets:
- KXNBAEAST: Current NBA Eastern Conference playoffs. Boston Celtics and Cleveland Cavaliers are top seeds. Indiana Pacers, Milwaukee Bucks, Detroit Pistons, Orlando Magic, Miami Heat, Atlanta Hawks are competing. Use current standings.
- KXNBAWEST: Oklahoma City Thunder and Houston Rockets are top seeds. Denver Nuggets, Memphis Grizzlies, LA Lakers, Golden State Warriors competing.
- KXNBA Finals: OKC Thunder and Boston Celtics are current favorites at ~25-30% each.
- KXMLB: Dodgers are heavy World Series favorites (~25%). Yankees, Phillies, Braves competing.
- For any market at 1-3 cents: these are likely correct (extreme longshots). Skip unless you have strong evidence.
- For any market at 15-30 cents: evaluate carefully — these are the interesting ones with real edge potential.
- For championship markets, use implied probabilities: if a team is 18¢ on Kalshi but Vegas has them at 25%, that is a buy.

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
