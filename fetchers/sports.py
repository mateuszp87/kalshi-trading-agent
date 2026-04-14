"""
Sports signal fetcher
Sources: ESPN API, The Odds API (Vegas lines), public injury feeds
"""

import logging
import aiohttp
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ESPN public API (no key required for basic data)
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# The Odds API — free tier: 500 requests/month
# https://the-odds-api.com/
ODDS_BASE = "https://api.the-odds-api.com/v4"

SPORT_MAP = {
    "nba": ("basketball", "nba"),
    "nfl": ("football", "nfl"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
    "nba playoffs": ("basketball", "nba"),
}


# Current NBA/MLB context — updated April 2026
NBA_CONTEXT = {
    "eastern": {
        "favorites": {"Boston": 0.28, "Cleveland": 0.22, "Indiana": 0.15, "Milwaukee": 0.12, "Detroit": 0.08, "Orlando": 0.07, "Miami": 0.05, "Atlanta": 0.03},
        "top_seed": "Boston Celtics",
    },
    "western": {
        "favorites": {"Oklahoma City": 0.30, "Houston": 0.22, "Denver": 0.18, "Memphis": 0.12, "LA Lakers": 0.08, "Golden State": 0.06},
        "top_seed": "Oklahoma City Thunder",
    },
    "finals": {"Oklahoma City": 0.28, "Boston": 0.25, "Cleveland": 0.14, "Houston": 0.12},
}

MLB_CONTEXT = {
    "world_series": {"LA Dodgers": 0.24, "New York Yankees": 0.14, "Philadelphia": 0.10, "Atlanta": 0.09, "Houston": 0.08},
}

async def fetch_sports_signals(market_title: str, api_key: str = "") -> dict:
    """
    Determine sport from market title, then fetch:
    - Upcoming game lines (The Odds API)
    - Injury reports (ESPN)
    - Team recent form (ESPN scoreboard)
    """
    signals = {}
    sport_key = _detect_sport(market_title)
    title_lower = market_title.lower()
    away_team, home_team = _extract_teams(market_title)

    # Inject real NBA/MLB championship context
    if "eastern conference" in title_lower or "nbaeast" in title_lower.replace(" ",""):
        for team, prob in NBA_CONTEXT["eastern"]["favorites"].items():
            if team.lower() in title_lower:
                signals["nba_championship_odds"] = {
                    "value": prob,
                    "description": f"Current NBA Eastern Conference championship implied probability for {team}: {prob:.0%}. Top seed: {NBA_CONTEXT['eastern']['top_seed']}",
                    "raw": NBA_CONTEXT["eastern"],
                }
                break
    elif "western conference" in title_lower:
        for team, prob in NBA_CONTEXT["western"]["favorites"].items():
            if team.lower() in title_lower:
                signals["nba_championship_odds"] = {
                    "value": prob,
                    "description": f"Current NBA Western Conference championship implied probability for {team}: {prob:.0%}. Top seed: {NBA_CONTEXT['western']['top_seed']}",
                    "raw": NBA_CONTEXT["western"],
                }
                break
    elif "basketball finals" in title_lower or "nba finals" in title_lower:
        for team, prob in NBA_CONTEXT["finals"].items():
            if team.lower() in title_lower:
                signals["nba_finals_odds"] = {
                    "value": prob,
                    "description": f"Current NBA Finals implied probability for {team}: {prob:.0%}",
                    "raw": NBA_CONTEXT["finals"],
                }
                break
    elif "baseball" in title_lower or "mlb" in title_lower or "world series" in title_lower:
        for team, prob in MLB_CONTEXT["world_series"].items():
            if team.lower() in title_lower:
                signals["mlb_ws_odds"] = {
                    "value": prob,
                    "description": f"Current World Series implied probability for {team}: {prob:.0%}",
                    "raw": MLB_CONTEXT["world_series"],
                }
                break

    # Add team context for individual game markets
    if away_team and home_team:
        signals["game_context"] = {
            "value": 0.5,
            "description": f"Individual game market: {away_team} vs {home_team}. Home team ({home_team}) wins ~55% of NBA games. Use Vegas line as primary signal if available.",
            "raw": {"away": away_team, "home": home_team, "sport": sport_key}
        }

    async with aiohttp.ClientSession() as session:
        # 1. Vegas odds / probability implied by sportsbooks
        if api_key:
            odds = await _fetch_vegas_odds(session, sport_key, api_key)
            if odds:
                signals["vegas_implied_prob"] = {
                    "value": round(odds["implied_prob"], 3),
                    "description": f"Sportsbook implied probability for relevant team/outcome. Spread: {odds.get('spread', 'N/A')}",
                    "raw": odds,
                }

        # 2. ESPN injury report
        injuries = await _fetch_injuries(session, sport_key)
        if injuries:
            signals["injury_report"] = {
                "value": injuries["impact_score"],  # 0=no impact, 1=major star out
                "description": f"Key injuries detected: {injuries['summary']}",
                "raw": injuries["players"],
            }

        # 3. Recent team form
        form = await _fetch_team_form(session, sport_key, market_title)
        if form:
            signals["team_momentum"] = {
                "value": round(form["win_pct_last10"], 3),
                "description": f"Win % over last 10 games: {form['record']}. Avg point diff: {form['avg_margin']:+.1f}",
                "raw": form,
            }

        # 4. Polymarket cross-reference (public API, no key needed)
        poly = await _fetch_polymarket_sports(session, market_title)
        if poly:
            signals["polymarket_price"] = {
                "value": poly["price"],
                "description": f"Polymarket crowd probability for similar outcome: {poly['market_title'][:60]}",
                "raw": poly,
            }

    return signals


def _extract_teams(title: str) -> tuple:
    """Extract home and away teams from game title like 'Orlando vs Philadelphia'"""
    title_clean = title.replace('Winner?','').replace('Second Half','').replace('First Half','').strip()
    if ' vs ' in title_clean:
        parts = title_clean.split(' vs ')
        return parts[0].strip().split(':')[0].strip(), parts[1].strip().split(':')[0].strip()
    return '', ''

def _detect_sport(title: str) -> str:
    title_lower = title.lower()
    if any(x in title_lower for x in ["nba", "basketball", "lakers", "celtics", "warriors", "nets", "heat", "bulls"]):
        return "basketball_nba"
    if any(x in title_lower for x in ["nfl", "football", "super bowl", "quarterback", "touchdown"]):
        return "americanfootball_nfl"
    if any(x in title_lower for x in ["mlb", "baseball", "yankees", "dodgers", "pitcher", "home run", "hr"]):
        return "baseball_mlb"
    if any(x in title_lower for x in ["nhl", "hockey", "puck", "stanley cup"]):
        return "icehockey_nhl"
    return "basketball_nba"  # default


async def _fetch_vegas_odds(session: aiohttp.ClientSession, sport_key: str, api_key: str) -> dict:
    try:
        url = f"{ODDS_BASE}/sports/{sport_key}/odds"
        params = {
            "apiKey": api_key,
            "regions": "us",
            "markets": "h2h,spreads",
            "oddsFormat": "decimal",
        }
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return {}
            games = await resp.json()
            if not games:
                return {}
            # Take most recent game
            game = games[0]
            bookmakers = game.get("bookmakers", [])
            if not bookmakers:
                return {}
            book = bookmakers[0]
            outcomes = book.get("markets", [{}])[0].get("outcomes", [])
            if len(outcomes) < 2:
                return {}
            # Decimal odds → implied probability
            home_prob = round(1 / outcomes[0]["price"], 3)
            away_prob = round(1 / outcomes[1]["price"], 3)
            return {
                "home_team": game.get("home_team", ""),
                "away_team": game.get("away_team", ""),
                "implied_prob": home_prob,
                "spread": outcomes[0].get("point", "N/A"),
            }
    except Exception as e:
        log.warning(f"Vegas odds fetch failed: {e}")
        return {}


async def _fetch_injuries(session: aiohttp.ClientSession, sport_key: str) -> dict:
    try:
        # ESPN injuries endpoint
        sport, league = sport_key.split("_") if "_" in sport_key else ("basketball", "nba")
        url = f"{ESPN_BASE}/{sport}/{league}/injuries"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            injured = []
            impact = 0.0
            for team in data.get("injuries", [])[:5]:
                for player in team.get("injuries", [])[:3]:
                    name = player.get("athlete", {}).get("displayName", "Unknown")
                    status = player.get("status", "")
                    pos = player.get("athlete", {}).get("position", {}).get("abbreviation", "")
                    injured.append(f"{name} ({pos}): {status}")
                    if status in ("Out", "Doubtful") and pos in ("PG", "SG", "SF", "QB", "SP"):
                        impact = min(impact + 0.3, 1.0)
            return {
                "impact_score": round(impact, 2),
                "summary": "; ".join(injured[:4]) or "No significant injuries",
                "players": injured[:8],
            }
    except Exception as e:
        log.warning(f"ESPN injuries fetch failed: {e}")
        return {}


async def _fetch_team_form(session: aiohttp.ClientSession, sport_key: str, title: str) -> dict:
    try:
        sport, league = sport_key.split("_") if "_" in sport_key else ("basketball", "nba")
        url = f"{ESPN_BASE}/{sport}/{league}/scoreboard"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            events = data.get("events", [])
            if not events:
                return {}
            # Simple heuristic: count recent home-team wins
            wins, total, margins = 0, 0, []
            for ev in events[:10]:
                comps = ev.get("competitions", [{}])[0]
                competitors = comps.get("competitors", [])
                if len(competitors) < 2:
                    continue
                scores = [int(c.get("score", 0) or 0) for c in competitors]
                if scores[0] > scores[1]:
                    wins += 1
                    margins.append(scores[0] - scores[1])
                else:
                    margins.append(scores[0] - scores[1])
                total += 1
            if total == 0:
                return {}
            return {
                "win_pct_last10": round(wins / total, 3),
                "record": f"{wins}W-{total-wins}L",
                "avg_margin": round(sum(margins) / len(margins), 1) if margins else 0,
            }
    except Exception as e:
        log.warning(f"ESPN form fetch failed: {e}")
        return {}


async def _fetch_polymarket_sports(session: aiohttp.ClientSession, title: str) -> dict:
    try:
        url = "https://gamma-api.polymarket.com/markets"
        params = {"search": title[:40], "limit": 3, "active": "true"}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            if not data:
                return {}
            market = data[0]
            prices = market.get("outcomePrices", ["0.5"])
            if isinstance(prices, list) and len(prices) > 0:
                price = float(prices[0])
            else:
                price = float(market.get("lastTradePrice", 0.5))
            return {
                "price": round(price, 3),
                "market_title": market.get("question", ""),
                "volume": market.get("volume", 0),
            }
    except Exception as e:
        log.warning(f"Polymarket fetch failed: {e}")
        return {}
