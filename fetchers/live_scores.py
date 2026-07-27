"""Live sports scores from ESPN public API — no auth required."""
import aiohttp
import asyncio
import logging
import re
import time
from typing import Optional

log = logging.getLogger(__name__)

ESPN_ENDPOINTS = {
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "ncaamb": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
    "ncaafb": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
    "wnba": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
}

_CACHE = {"data": {}, "ts": 0}


async def fetch_all_live_scores(session: aiohttp.ClientSession) -> dict:
    """Fetch all games across all sports, indexed by team-code pair."""
    if time.time() - _CACHE["ts"] < 60 and _CACHE["data"]:
        return _CACHE["data"]

    games = {}
    for sport, url in ESPN_ENDPOINTS.items():
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    continue
                data = await r.json()
        except Exception as e:
            log.debug(f"ESPN {sport} fetch failed: {e}")
            continue

        for ev in data.get("events", []):
            try:
                status = ev.get("status", {})
                status_type = status.get("type", {})
                state = status_type.get("state", "")
                detail = status_type.get("shortDetail", "")

                competitions = ev.get("competitions", [{}])[0]
                teams = competitions.get("competitors", [])
                if len(teams) != 2:
                    continue

                home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])
                away = next((t for t in teams if t.get("homeAway") == "away"), teams[1])

                home_abbr = (home.get("team", {}).get("abbreviation") or "").upper()
                away_abbr = (away.get("team", {}).get("abbreviation") or "").upper()
                if not home_abbr or not away_abbr:
                    continue

                try:
                    home_score = int(home.get("score", 0) or 0)
                    away_score = int(away.get("score", 0) or 0)
                except (ValueError, TypeError):
                    home_score = away_score = 0

                period = ""
                time_left = ""
                if " - " in detail:
                    tp, pp = detail.split(" - ", 1)
                    time_left = tp.strip()
                    period = pp.strip()
                elif state == "in":
                    period = detail

                game = {
                    "sport": sport,
                    "home": home_abbr,
                    "away": away_abbr,
                    "home_score": home_score,
                    "away_score": away_score,
                    "score_diff": home_score - away_score,
                    "period": period,
                    "time_left": time_left,
                    "detail": detail,
                    "state": state,
                    "is_live": state == "in",
                    "is_final": state == "post",
                }

                # Index both orderings
                games[home_abbr + away_abbr] = game
                games[away_abbr + home_abbr] = game
            except Exception as e:
                log.debug(f"ESPN parse error for {sport}: {e}")
                continue

    _CACHE["data"] = games
    _CACHE["ts"] = time.time()
    return games


# Ticker format: KX<SPORT><TYPE>-<DATE><TEAMA><TEAMB>-<OUTCOME>
# where OUTCOME starts with one of the team codes
#
# Strategy: match the DATE prefix, then the SUFFIX of OUTCOME tells us
# where one team starts. We check 2, 3, and 4-char lengths for the last team.
_TICKER_PREFIX = re.compile(
    r"^KX(NBA|NHL|MLB|NFL|WNBA|NCAAMB|NCAAFB|UCL|EPL|LALIGA|SERIEA|BUNDESLIGA|LIGUE1|MLS)"
    r"(GAME|SPREAD|TOTAL|SERIES)-"
    r"\d{2}[A-Z]{3}\d{2}"
    r"([A-Z]+)-([A-Z]+)",  # team-pair and outcome
    re.IGNORECASE,
)


def parse_ticker_teams(ticker: str) -> Optional[tuple]:
    """Returns (sport, team_a, team_b) where team_a and team_b are the two teams.
    Uses the outcome suffix to correctly split the team-pair.
    """
    m = _TICKER_PREFIX.match(ticker)
    if not m:
        return None
    sport = m.group(1).lower()
    pair = m.group(3).upper()      # e.g. "TORCLE" or "PORSAS"
    outcome = m.group(4).upper()   # e.g. "CLE" or "SAS14"

    # Strip trailing digits from outcome to get pure team code
    # SAS14 → SAS, DET6 → DET, OKC → OKC
    outcome_clean = re.sub(r"\d+$", "", outcome)

    # Try matching outcome against end of pair (3 or 4 char team codes)
    # and against start of pair (in case the ticker format puts outcome-team first)
    for team_len in [4, 3, 2]:
        if len(outcome_clean) >= team_len:
            suffix = outcome_clean[:team_len]
            if pair.endswith(suffix) and len(pair) > team_len:
                # suffix matched the END of pair → pair = team_a + suffix
                team_a = pair[:-team_len]
                team_b = suffix
                if len(team_a) >= 2:  # sanity check
                    return (sport, team_a, team_b)
            if pair.startswith(suffix) and len(pair) > team_len:
                # suffix matched the START → pair = suffix + team_b
                team_a = suffix
                team_b = pair[team_len:]
                if len(team_b) >= 2:
                    return (sport, team_a, team_b)

    # Fallback: assume 3+3 split (works for most NBA tickers)
    if len(pair) == 6:
        return (sport, pair[:3], pair[3:])
    elif len(pair) == 7:
        # Try 3+4 and 4+3
        return (sport, pair[:3], pair[3:])
    elif len(pair) == 8:
        return (sport, pair[:4], pair[4:])

    return None


async def get_live_game_for_ticker(session, ticker: str) -> dict:
    """Given a Kalshi ticker, find matching live game. Returns {} if no match."""
    parsed = parse_ticker_teams(ticker)
    if not parsed:
        return {}
    sport, team_a, team_b = parsed
    all_games = await fetch_all_live_scores(session)
    return all_games.get(team_a + team_b, {}) or all_games.get(team_b + team_a, {})
