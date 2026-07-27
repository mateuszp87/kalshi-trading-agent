"""
Politics signal fetcher
Sources: NewsAPI (headlines), Polymarket, 538/RCP polling averages, Metaculus
"""

import logging
import aiohttp

log = logging.getLogger(__name__)


async def fetch_politics_signals(market_title: str, newsapi_key: str = "") -> dict:
    signals = {}

    async with aiohttp.ClientSession() as session:
        # 1. News sentiment via NewsAPI
        if newsapi_key:
            news = await _fetch_news_sentiment(session, market_title, newsapi_key)
            if news:
                signals["news_sentiment"] = {
                    "value": news["sentiment_score"],
                    "description": f"News sentiment score (0=negative, 1=positive). {news['article_count']} articles analyzed. Top: {news['top_headline'][:80]}",
                    "raw": news["headlines"][:5],
                }

        # 2. Polymarket crowd probability
        poly = await _fetch_polymarket(session, market_title)
        if poly:
            signals["polymarket_crowd"] = {
                "value": poly["yes_price"],
                "description": f"Polymarket crowd probability: '{poly['question'][:70]}' — ${poly['volume']:,.0f} volume",
                "raw": poly,
            }

        # 3. Metaculus community prediction
        meta = await _fetch_metaculus(session, market_title)
        if meta:
            signals["metaculus_prediction"] = {
                "value": meta["community_prediction"],
                "description": f"Metaculus community median: {meta['question'][:60]}. {meta['forecasters']} forecasters.",
                "raw": meta,
            }

        # 4. 538 / RCP polling snapshot (public RSS/JSON)
        polls = await _fetch_polling_snapshot(session, market_title)
        if polls:
            signals["polling_average"] = {
                "value": polls["implied_prob"],
                "description": f"Polling-implied probability. Latest avg: {polls['summary']}",
                "raw": polls,
            }

    return signals


async def _fetch_news_sentiment(session: aiohttp.ClientSession, query: str, api_key: str) -> dict:
    try:
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query[:100],
            "sortBy": "publishedAt",
            "pageSize": 10,
            "language": "en",
            "apiKey": api_key,
        }
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            articles = data.get("articles", [])
            if not articles:
                return {}

            headlines = [a.get("title", "") for a in articles]
            # Simple keyword-based sentiment (Claude will do deeper analysis)
            positive_words = {"win", "surge", "lead", "up", "rise", "strong", "likely", "ahead", "favored"}
            negative_words = {"lose", "fall", "trail", "down", "drop", "weak", "unlikely", "behind", "scandal"}

            score = 0.5
            for h in headlines:
                hl = h.lower()
                pos = sum(1 for w in positive_words if w in hl)
                neg = sum(1 for w in negative_words if w in hl)
                score += (pos - neg) * 0.03
            score = max(0.1, min(0.9, score))

            return {
                "sentiment_score": round(score, 3),
                "article_count": len(articles),
                "top_headline": headlines[0] if headlines else "",
                "headlines": headlines[:5],
            }
    except Exception as e:
        log.warning(f"NewsAPI fetch failed: {e}")
        return {}


async def _fetch_polymarket(session: aiohttp.ClientSession, query: str) -> dict:
    try:
        url = "https://gamma-api.polymarket.com/markets"
        params = {"search": query[:50], "limit": 5, "active": "true"}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            markets = await resp.json()
            if not markets:
                return {}
            m = markets[0]
            prices = m.get("outcomePrices", ["0.5", "0.5"])
            yes_price = float(prices[0]) if prices else 0.5
            return {
                "yes_price": round(yes_price, 3),
                "question": m.get("question", ""),
                "volume": float(m.get("volume", 0)),
                "liquidity": float(m.get("liquidity", 0)),
            }
    except Exception as e:
        log.warning(f"Polymarket fetch failed: {e}")
        return {}


async def _fetch_metaculus(session: aiohttp.ClientSession, query: str) -> dict:
    try:
        url = "https://www.metaculus.com/api2/questions/"
        params = {"search": query[:50], "status": "open", "limit": 3}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            results = data.get("results", [])
            if not results:
                return {}
            q = results[0]
            pred = q.get("community_prediction", {})
            cp = pred.get("full", {}).get("q2", 0.5) if isinstance(pred, dict) else 0.5
            return {
                "community_prediction": round(float(cp), 3),
                "question": q.get("title", ""),
                "forecasters": q.get("number_of_forecasters", 0),
            }
    except Exception as e:
        log.warning(f"Metaculus fetch failed: {e}")
        return {}


async def _fetch_polling_snapshot(session: aiohttp.ClientSession, query: str) -> dict:
    """
    Lightweight polling proxy — pulls from FiveThirtyEight's public data
    where available, otherwise returns empty.
    """
    try:
        # 538 publishes CSV/JSON for major races
        url = "https://projects.fivethirtyeight.com/polls/polls.json"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            # Filter by keyword match
            query_words = set(query.lower().split())
            relevant = [
                p for p in data
                if any(w in p.get("question", "").lower() for w in query_words)
            ]
            if not relevant:
                return {}
            # Average pct_yes across polls
            pcts = [p.get("pct", 50) for p in relevant[:5]]
            avg = sum(pcts) / len(pcts)
            implied = round(avg / 100, 3)
            return {
                "implied_prob": implied,
                "summary": f"Avg {avg:.1f}% across {len(relevant)} polls",
                "polls": relevant[:3],
            }
    except Exception as e:
        log.warning(f"Polling fetch failed: {e}")
        return {}
