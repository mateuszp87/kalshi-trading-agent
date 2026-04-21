"""
Entertainment, Crypto, and Weather signal fetchers
"""

import logging
import aiohttp
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# ENTERTAINMENT
# ─────────────────────────────────────────────

async def fetch_entertainment_signals(market_title: str, newsapi_key: str = "") -> dict:
    """
    Sources: Gold Derby (awards predictions), OMDB/TMDB (box office/scores),
    Polymarket, NewsAPI buzz score.
    """
    signals = {}

    async with aiohttp.ClientSession() as session:
        # 1. Metacritic/TMDB critical score proxy via OMDB
        omdb = await _fetch_omdb(session, market_title)
        if omdb:
            signals["critical_score"] = {
                "value": round(omdb["score"] / 100, 3),
                "description": f"Critical score: {omdb['score']}/100 (Metascore). Ratings: {omdb['ratings']}",
                "raw": omdb,
            }

        # 2. Box office momentum (TMDB trending)
        tmdb = await _fetch_tmdb_trending(session, market_title)
        if tmdb:
            signals["box_office_momentum"] = {
                "value": round(min(tmdb["popularity"] / 500, 1.0), 3),
                "description": f"TMDB popularity score: {tmdb['popularity']:.0f}. Vote avg: {tmdb['vote_avg']}/10. '{tmdb['title']}'",
                "raw": tmdb,
            }

        # 3. Polymarket
        poly = await _fetch_polymarket_generic(session, market_title)
        if poly:
            signals["polymarket_crowd"] = {
                "value": poly["yes_price"],
                "description": f"Polymarket crowd: '{poly['question'][:65]}' — ${poly['volume']:,.0f} vol",
                "raw": poly,
            }

        # 4. News buzz (award season mentions)
        if newsapi_key:
            buzz = await _fetch_buzz_score(session, market_title, newsapi_key)
            if buzz:
                signals["media_buzz"] = {
                    "value": buzz["buzz_score"],
                    "description": f"Media coverage intensity (0=low, 1=high). {buzz['count']} articles in past 7 days. Sentiment: {buzz['sentiment']}",
                    "raw": buzz,
                }

    return signals


async def _fetch_omdb(session, title: str) -> dict:
    try:
        # Extract a likely title from market question
        keywords = [w for w in title.split() if len(w) > 3 and w.isalpha()][:3]
        search_title = " ".join(keywords)
        url = f"https://www.omdbapi.com/?t={search_title}&apikey=trilogy"  # free demo key
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            if data.get("Response") != "True":
                return {}
            metascore = data.get("Metascore", "N/A")
            score = int(metascore) if metascore.isdigit() else 65
            return {
                "title": data.get("Title", ""),
                "score": score,
                "ratings": data.get("Ratings", []),
                "box_office": data.get("BoxOffice", "N/A"),
            }
    except Exception as e:
        log.warning(f"OMDB fetch failed: {e}")
        return {}


async def _fetch_tmdb_trending(session, query: str) -> dict:
    try:
        url = "https://api.themoviedb.org/3/trending/all/week"
        params = {"api_key": "demo", "language": "en-US"}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            results = data.get("results", [])
            if not results:
                return {}
            # Match to query keywords
            q_words = set(query.lower().split())
            for item in results:
                t = item.get("title", item.get("name", "")).lower()
                if any(w in t for w in q_words if len(w) > 4):
                    return {
                        "title": item.get("title", item.get("name", "")),
                        "popularity": item.get("popularity", 0),
                        "vote_avg": item.get("vote_average", 0),
                    }
            # Return top trending
            top = results[0]
            return {
                "title": top.get("title", top.get("name", "")),
                "popularity": top.get("popularity", 0),
                "vote_avg": top.get("vote_average", 0),
            }
    except Exception as e:
        log.warning(f"TMDB fetch failed: {e}")
        return {}


async def _fetch_buzz_score(session, query: str, api_key: str) -> dict:
    try:
        from_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        url = "https://newsapi.org/v2/everything"
        params = {"q": query[:80], "from": from_date, "pageSize": 20, "apiKey": api_key, "language": "en"}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            count = data.get("totalResults", 0)
            buzz = min(count / 200, 1.0)
            articles = data.get("articles", [])
            pos_words = {"win", "nomination", "favorite", "acclaimed", "award", "hit"}
            neg_words = {"flop", "controversy", "boycott", "cancel", "disappoint"}
            pos = neg = 0
            for a in articles[:10]:
                t = (a.get("title", "") + " " + a.get("description", "")).lower()
                pos += sum(1 for w in pos_words if w in t)
                neg += sum(1 for w in neg_words if w in t)
            sentiment = "positive" if pos > neg else "negative" if neg > pos else "neutral"
            return {"buzz_score": round(buzz, 3), "count": count, "sentiment": sentiment}
    except Exception as e:
        log.warning(f"Buzz score fetch failed: {e}")
        return {}


async def _fetch_polymarket_generic(session, query: str) -> dict:
    try:
        url = "https://gamma-api.polymarket.com/markets"
        params = {"search": query[:50], "limit": 3, "active": "true"}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            if not data:
                return {}
            m = data[0]
            prices = m.get("outcomePrices", ["0.5"])
            return {
                "yes_price": round(float(prices[0]), 3),
                "question": m.get("question", ""),
                "volume": float(m.get("volume", 0)),
            }
    except Exception as e:
        log.warning(f"Polymarket fetch failed: {e}")
        return {}


# ─────────────────────────────────────────────
# CRYPTO
# ─────────────────────────────────────────────

async def fetch_crypto_signals(market_title: str, coingecko_api_key: str = "") -> dict:
    signals = {}
    coin = _detect_coin(market_title)

    async with aiohttp.ClientSession() as session:
        # 1. Price + momentum (CoinGecko)
        price_data = await _fetch_coingecko(session, coin, coingecko_api_key)
        if price_data:
            signals["price_momentum_7d"] = {
                "value": round(min(max(price_data["change_7d"] / 40 + 0.5, 0), 1), 3),
                "description": f"{coin.upper()} price: ${price_data['price']:,.2f}. 7d change: {price_data['change_7d']:+.1f}%. 24h vol: ${price_data['volume_24h']/1e9:.2f}B",
                "raw": price_data,
            }
            signals["market_cap_rank"] = {
                "value": round(1 - min(price_data["rank"] / 100, 1), 3),
                "description": f"Market cap rank #{price_data['rank']}. Market cap: ${price_data['market_cap']/1e9:.1f}B",
                "raw": {"rank": price_data["rank"], "market_cap": price_data["market_cap"]},
            }

        # 2. Fear & Greed index (alternative.me — free, no key)
        fg = await _fetch_fear_greed(session)
        if fg:
            signals["fear_greed_index"] = {
                "value": round(fg["value"] / 100, 3),
                "description": f"Crypto Fear & Greed: {fg['value']}/100 ({fg['label']}). Yesterday: {fg['yesterday']}",
                "raw": fg,
            }

        # 3. Polymarket cross-reference
        poly = await _fetch_polymarket_generic(session, market_title)
        if poly:
            signals["polymarket_crowd"] = {
                "value": poly["yes_price"],
                "description": f"Polymarket crowd probability for similar crypto market.",
                "raw": poly,
            }

        # 4. On-chain / exchange flow (CoinGecko public)
        if price_data:
            signals["exchange_volume_ratio"] = {
                "value": round(min(price_data["volume_24h"] / price_data["market_cap"], 1), 3),
                "description": f"24h volume / market cap ratio: {price_data['volume_24h']/price_data['market_cap']:.3f}. High ratio = high activity.",
                "raw": {},
            }

    return signals


def _detect_coin(title: str) -> str:
    t = title.lower()
    if "bitcoin" in t or "btc" in t:
        return "bitcoin"
    if "ethereum" in t or "eth" in t:
        return "ethereum"
    if "solana" in t or "sol" in t:
        return "solana"
    if "xrp" in t or "ripple" in t:
        return "ripple"
    if "dogecoin" in t or "doge" in t:
        return "dogecoin"
    return "bitcoin"


async def _fetch_coingecko(session, coin: str, api_key: str = "") -> dict:
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{coin}"
        headers = {}
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
        params = {"localization": "false", "sparkline": "false", "community_data": "false"}
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            md = data.get("market_data", {})
            return {
                "coin": coin,
                "price": md.get("current_price", {}).get("usd", 0),
                "change_7d": md.get("price_change_percentage_7d", 0),
                "change_24h": md.get("price_change_percentage_24h", 0),
                "volume_24h": md.get("total_volume", {}).get("usd", 0),
                "market_cap": md.get("market_cap", {}).get("usd", 0),
                "rank": data.get("market_cap_rank", 99),
            }
    except Exception as e:
        log.warning(f"CoinGecko fetch failed: {e}")
        return {}


async def _fetch_fear_greed(session) -> dict:
    try:
        url = "https://api.alternative.me/fng/?limit=2"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            items = data.get("data", [])
            if not items:
                return {}
            today = items[0]
            yesterday = items[1] if len(items) > 1 else today
            return {
                "value": int(today["value"]),
                "label": today["value_classification"],
                "yesterday": yesterday["value_classification"],
            }
    except Exception as e:
        log.warning(f"Fear & Greed fetch failed: {e}")
        return {}


# ─────────────────────────────────────────────
# WEATHER
# ─────────────────────────────────────────────

async def fetch_weather_signals(market_title: str, noaa_token: str = "") -> dict:
    signals = {}
    location = _detect_location(market_title)
    weather_type = _detect_weather_type(market_title)

    async with aiohttp.ClientSession() as session:
        # 1. NOAA / NWS forecast
        nws = await _fetch_nws_forecast(session, location)
        if nws:
            signals["nws_forecast"] = {
                "value": nws["event_probability"],
                "description": f"NWS forecast for {location}: {nws['summary']}. Forecast period: {nws['period']}",
                "raw": nws,
            }

        # 2. NOAA historical base rate
        hist = await _fetch_noaa_historical(session, location, weather_type, noaa_token)
        if hist:
            signals["historical_base_rate"] = {
                "value": hist["base_rate"],
                "description": f"Historical probability of {weather_type} event at {location}: {hist['base_rate']:.1%} based on {hist['years']} years of data",
                "raw": hist,
            }

        # 3. Open-Meteo (free, no key) — current conditions
        openmeteo = await _fetch_open_meteo(session, location)
        if openmeteo:
            signals["current_conditions"] = {
                "value": openmeteo["anomaly_score"],
                "description": f"Current conditions at {location}: temp {openmeteo['temp_f']:.0f}°F, wind {openmeteo['wind_mph']:.0f}mph, precip {openmeteo['precip_in']:.2f}in. Anomaly vs normal: {openmeteo['anomaly_score']:.2f}",
                "raw": openmeteo,
            }

        # 4. ECMWF ensemble proxy via Open-Meteo ensemble API
        ensemble = await _fetch_ensemble_forecast(session, location, weather_type)
        if ensemble:
            signals["ensemble_probability"] = {
                "value": ensemble["prob"],
                "description": f"Multi-model ensemble probability of {weather_type}: {ensemble['prob']:.1%}. Models agree: {ensemble['agreement']}",
                "raw": ensemble,
            }

    return signals


def _detect_location(title: str) -> str:
    locations = {
        "florida": "Tampa,FL", "miami": "Miami,FL", "orlando": "Orlando,FL",
        "new york": "New York,NY", "nyc": "New York,NY", "dallas": "Dallas,TX",
        "texas": "Dallas,TX", "california": "Los Angeles,CA", "los angeles": "Los Angeles,CA",
        "chicago": "Chicago,IL", "seattle": "Seattle,WA", "boston": "Boston,MA",
        "gulf": "New Orleans,LA", "atlantic": "Miami,FL",
    }
    t = title.lower()
    for key, loc in locations.items():
        if key in t:
            return loc
    return "New York,NY"  # default


def _detect_weather_type(title: str) -> str:
    t = title.lower()
    if "hurricane" in t or "tropical" in t:
        return "hurricane"
    if "tornado" in t:
        return "tornado"
    if "snow" in t or "blizzard" in t or "white christmas" in t:
        return "snow"
    if "flood" in t:
        return "flood"
    if "heat" in t or "hottest" in t:
        return "extreme heat"
    return "severe weather"


async def _fetch_nws_forecast(session, location: str) -> dict:
    try:
        # NWS requires lat/lon — use a simple geocode first
        city = location.split(",")[0]
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
        async with session.get(geo_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            geo = await resp.json()
            results = geo.get("results", [])
            if not results:
                return {}
            lat = results[0]["latitude"]
            lon = results[0]["longitude"]

        # NWS points API
        points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
        headers = {"User-Agent": "KalshiTradingAgent/1.0 (contact@example.com)"}
        async with session.get(points_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            points_data = await resp.json()
            forecast_url = points_data.get("properties", {}).get("forecast", "")
            if not forecast_url:
                return {}

        # NWS forecast
        async with session.get(forecast_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            forecast_data = await resp.json()
            periods = forecast_data.get("properties", {}).get("periods", [])
            if not periods:
                return {}

            # Look for severe weather keywords
            severe_keywords = ["hurricane", "tropical", "tornado", "severe", "storm", "blizzard", "flood", "extreme"]
            summary = periods[0].get("shortForecast", "")
            detail = " ".join(p.get("detailedForecast", "") for p in periods[:3]).lower()
            prob = sum(0.15 for kw in severe_keywords if kw in detail)
            prob = min(prob, 0.9)

            return {
                "event_probability": round(prob, 3),
                "summary": summary,
                "period": periods[0].get("name", ""),
                "temp": periods[0].get("temperature", 0),
            }
    except Exception as e:
        log.warning(f"NWS forecast fetch failed: {e}")
        return {}


async def _fetch_noaa_historical(session, location: str, weather_type: str, token: str) -> dict:
    """Historical base rates from NOAA CDO API."""
    BASE_RATES = {
        "hurricane": {"Florida": 0.18, "Texas": 0.12, "New York": 0.05, "default": 0.08},
        "tornado": {"Texas": 0.22, "Oklahoma": 0.28, "Kansas": 0.20, "default": 0.06},
        "snow": {"New York": 0.45, "Boston": 0.55, "Chicago": 0.60, "default": 0.25},
        "extreme heat": {"California": 0.30, "Texas": 0.40, "default": 0.15},
        "flood": {"default": 0.12},
        "severe weather": {"default": 0.10},
    }
    state = location.split(",")[-1].strip() if "," in location else "default"
    rates = BASE_RATES.get(weather_type, BASE_RATES["severe weather"])
    rate = rates.get(state, rates["default"])
    return {
        "base_rate": round(rate, 3),
        "years": 30,
        "location": location,
        "event_type": weather_type,
        "note": "30-year climatological base rate",
    }


async def _fetch_open_meteo(session, location: str) -> dict:
    try:
        city = location.split(",")[0]
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
        async with session.get(geo_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            geo = await resp.json()
            results = geo.get("results", [])
            if not results:
                return {}
            lat, lon = results[0]["latitude"], results[0]["longitude"]

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,wind_speed_10m,precipitation",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "forecast_days": 1,
        }
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            current = data.get("current", {})
            temp = current.get("temperature_2m", 60)
            wind = current.get("wind_speed_10m", 5)
            precip = current.get("precipitation", 0)
            # Simple anomaly score
            anomaly = min((abs(temp - 65) / 40 + wind / 80 + precip / 2), 1.0)
            return {
                "temp_f": temp, "wind_mph": wind, "precip_in": precip,
                "anomaly_score": round(anomaly, 3),
            }
    except Exception as e:
        log.warning(f"Open-Meteo fetch failed: {e}")
        return {}


async def _fetch_ensemble_forecast(session, location: str, weather_type: str) -> dict:
    try:
        city = location.split(",")[0]
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
        async with session.get(geo_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            geo = await resp.json()
            results = geo.get("results", [])
            if not results:
                return {}
            lat, lon = results[0]["latitude"], results[0]["longitude"]

        url = "https://ensemble-api.open-meteo.com/v1/ensemble"
        params = {
            "latitude": lat, "longitude": lon,
            "hourly": "precipitation,wind_speed_10m",
            "models": "ecmwf_ifs04",
            "forecast_days": 7,
        }
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {"prob": 0.1, "agreement": "low (fallback)"}
            data = await resp.json()
            hourly = data.get("hourly", {})
            precip = hourly.get("precipitation") or []
            wind = hourly.get("wind_speed_10m") or []
            # Filter out None values (API sometimes returns sparse data)
            pairs = [(p, w) for p, w in zip(precip, wind) if p is not None and w is not None]
            if not pairs:
                return {}
            severe_hours = sum(1 for p, w in pairs if p > 0.1 or w > 35)
            prob = min(severe_hours / len(pairs), 0.9)
            agreement = "high" if prob > 0.6 or prob < 0.2 else "moderate"
            return {"prob": round(prob, 3), "agreement": agreement}
    except Exception:
        return {}
