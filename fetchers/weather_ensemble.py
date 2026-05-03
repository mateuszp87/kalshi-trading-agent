"""Multi-source weather forecast ensemble for Kalshi weather markets.

Sources:
1. NWS (National Weather Service) — free, no auth, US-only
2. Open-Meteo — free, no auth
3. Tomorrow.io — needs TOMORROW_API_KEY

Returns ensemble forecasts. If sources disagree, returns SKIP.
"""
import os
import re
import logging
import statistics
import asyncio
from datetime import datetime, timezone
from typing import Optional
import aiohttp

log = logging.getLogger(__name__)

CITY_COORDS = {
    "NY":   (40.7128, -74.0060, "OKX", "New York"),
    "CHI":  (41.8781, -87.6298, "LOT", "Chicago"),
    "LAX":  (34.0522, -118.2437, "LOX", "Los Angeles"),
    "DEN":  (39.7392, -104.9903, "BOU", "Denver"),
    "MIA":  (25.7617, -80.1918, "MFL", "Miami"),
    "PHL":  (39.9526, -75.1652, "PHI", "Philadelphia"),
    "AUS":  (30.2672, -97.7431, "EWX", "Austin"),
    "BOS":  (42.3601, -71.0589, "BOX", "Boston"),
    "DC":   (38.9072, -77.0369, "LWX", "Washington DC"),
    "HOU":  (29.7604, -95.3698, "HGX", "Houston"),
    "ATL":  (33.7490, -84.3880, "FFC", "Atlanta"),
    "PHX":  (33.4484, -112.0740, "PSR", "Phoenix"),
    "SEA":  (47.6062, -122.3321, "SEW", "Seattle"),
    "SF":   (37.7749, -122.4194, "MTR", "San Francisco"),
}


def parse_weather_ticker(ticker: str) -> Optional[dict]:
    """Parse KXHIGHNY-26APR24-T67 or KXHIGHCHI-26APR24-B70.5"""
    m = re.match(
        r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])(\d+(?:\.\d+)?)",
        ticker
    )
    if not m:
        return None
    
    city_code = m.group(1)
    if city_code not in CITY_COORDS:
        return None
    
    yy = int(m.group(2)) + 2000
    mon_str = m.group(3)
    dd = int(m.group(4))
    direction = "above" if m.group(5) == "T" else "below"
    threshold = float(m.group(6))
    
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    mon = months.get(mon_str)
    if not mon:
        return None
    
    return {
        "ticker": ticker,
        "city_code": city_code,
        "city_name": CITY_COORDS[city_code][3],
        "lat": CITY_COORDS[city_code][0],
        "lon": CITY_COORDS[city_code][1],
        "date": datetime(yy, mon, dd, tzinfo=timezone.utc),
        "date_str": f"{yy}-{mon:02d}-{dd:02d}",
        "direction": direction,
        "threshold_f": threshold,
    }


async def _fetch_nws(session, parsed):
    try:
        async with session.get(
            f"https://api.weather.gov/points/{parsed['lat']:.4f},{parsed['lon']:.4f}",
            headers={"User-Agent": "kalshi-bot"},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                return None
            point_data = await r.json()
        forecast_url = point_data.get("properties", {}).get("forecastHourly")
        if not forecast_url:
            return None
        async with session.get(forecast_url, headers={"User-Agent": "kalshi-bot"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data = await r.json()
        target = parsed["date_str"]
        max_temp = None
        for period in data.get("properties", {}).get("periods", []):
            if period.get("startTime", "").startswith(target):
                temp = period.get("temperature")
                if temp is not None and (max_temp is None or temp > max_temp):
                    max_temp = temp
        return max_temp
    except Exception as e:
        log.debug(f"NWS failed: {e}")
        return None


async def _fetch_open_meteo(session, parsed):
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={parsed['lat']}&longitude={parsed['lon']}"
               f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
               f"&timezone=America/New_York"
               f"&start_date={parsed['date_str']}&end_date={parsed['date_str']}")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data = await r.json()
        highs = data.get("daily", {}).get("temperature_2m_max", [])
        if highs and highs[0] is not None:
            return float(highs[0])
        return None
    except Exception as e:
        log.debug(f"Open-Meteo failed: {e}")
        return None


async def _fetch_tomorrow(session, parsed):
    key = os.getenv("TOMORROW_API_KEY")
    if not key:
        return None
    try:
        url = (f"https://api.tomorrow.io/v4/timelines"
               f"?location={parsed['lat']},{parsed['lon']}"
               f"&fields=temperatureMax&timesteps=1d&units=imperial&apikey={key}")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data = await r.json()
        target = parsed["date_str"]
        intervals = data.get("data", {}).get("timelines", [{}])[0].get("intervals", [])
        for iv in intervals:
            if iv.get("startTime", "").startswith(target):
                return iv.get("values", {}).get("temperatureMax")
        return None
    except Exception as e:
        log.debug(f"Tomorrow.io failed: {e}")
        return None


async def get_ensemble_forecast(ticker: str) -> Optional[dict]:
    parsed = parse_weather_ticker(ticker)
    if not parsed:
        return None
    
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            _fetch_nws(session, parsed),
            _fetch_open_meteo(session, parsed),
            _fetch_tomorrow(session, parsed),
            return_exceptions=True,
        )
    
    sources = ["NWS", "Open-Meteo", "Tomorrow.io"]
    forecasts = []
    source_results = {}
    for name, val in zip(sources, results):
        if isinstance(val, Exception) or val is None:
            source_results[name] = None
        else:
            forecasts.append(float(val))
            source_results[name] = float(val)
    
    if len(forecasts) < 2:
        return {"parsed": parsed, "forecasts": forecasts, "source_results": source_results,
                "median": None, "agreement": False, "recommendation": "SKIP",
                "confidence": "LOW", "reason": f"only {len(forecasts)} sources available"}
    
    median = statistics.median(forecasts)
    threshold = parsed["threshold_f"]
    direction = parsed["direction"]
    
    if direction == "above":
        all_yes = all(f > threshold for f in forecasts)
        all_no = all(f < threshold for f in forecasts)
    else:
        all_yes = all(f < threshold for f in forecasts)
        all_no = all(f > threshold for f in forecasts)
    
    agreement = all_yes or all_no
    margin = abs(median - threshold)
    
    if not agreement:
        rec, conf = "SKIP", "LOW"
        reason = f"sources disagree (range {min(forecasts):.0f}-{max(forecasts):.0f}°, threshold {threshold}°)"
    elif margin < 4.0:
        rec, conf = "SKIP", "LOW"
        reason = f"too close to threshold (median {median:.1f}°, threshold {threshold}°, margin {margin:.1f}°)"
    elif all_yes:
        rec = "BUY_YES"
        conf = "HIGH" if margin >= 8.0 else "MEDIUM"
        reason = f"all {len(forecasts)} sources agree YES (median {median:.1f}°, threshold {threshold}°, margin {margin:.1f}°)"
    else:
        rec = "BUY_NO"
        conf = "HIGH" if margin >= 8.0 else "MEDIUM"
        reason = f"all {len(forecasts)} sources agree NO (median {median:.1f}°, threshold {threshold}°, margin {margin:.1f}°)"
    
    return {"parsed": parsed, "forecasts": forecasts, "source_results": source_results,
            "median": median, "margin": margin, "agreement": agreement,
            "recommendation": rec, "confidence": conf, "reason": reason}
