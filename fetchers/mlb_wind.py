"""Wind at the ballpark, for MLB totals.

Wrigley with 15mph blowing out is a different game than 15mph blowing in —
several runs of difference on the total, and the market prices it thinly
because most people are looking at pitchers, not anemometers.

Same structure as weather_ensemble: compute an independent estimate, and
only let it speak when it's confident. It checks Claude rather than
advising him.
"""
import logging, math
import aiohttp

log = logging.getLogger(__name__)

# lat, lon, and the compass bearing from home plate toward center field.
# A wind blowing along that bearing is blowing OUT.
PARKS = {
    "CHC": (41.9484, -87.6553,  34),   # Wrigley
    "BOS": (42.3467, -71.0972,  46),   # Fenway
    "NYY": (40.8296, -73.9262,  74),
    "SF":  (37.7786, -122.3893, 62),   # Oracle
    "COL": (39.7559, -104.9942,  0),   # Coors
    "SD":  (32.7076, -117.1570, 32),
    "LAD": (34.0739, -118.2400, 26),
    "PHI": (39.9061, -75.1665,   2),
    "DET": (42.3390, -83.0485, 150),
    "SEA": (47.5914, -122.3325, 60),
    "MIA": (25.7781, -80.2197,  74),
    "PIT": (40.4469, -80.0057, 118),
    "MIL": (43.0280, -87.9712, 128),
    "ATL": (33.8907, -84.4677, 154),
    "TOR": (43.6414, -79.3894,   0),
    "CWS": (41.8300, -87.6339, 122),
    "ATH": (37.7516, -122.2005,  60),
    "TB":  (27.7683, -82.6534,  76),
    "BAL": (39.2840, -76.6217,  32),
    "CLE": (41.4962, -81.6852,   0),
    "MIN": (44.9817, -93.2777, 
        68),
    "KC":  (39.0517, -94.4803,  46),
    "HOU": (29.7573, -95.3555, 348),
    "TEX": (32.7473, -97.0847,  12),
    "LAA": (33.8003, -117.8827,  44),
    "STL": (38.6226, -90.1928,   62),
    "CIN": (39.0975, -84.5069,  120),
    "WSH": (38.8730, -77.0074,   30),
    "NYM": (40.7571, -73.8458,   26),
    "ARI": (33.4453, -112.0667,   0),
}

# Roughly how much a full 10mph out-wind moves an over/under, by park.
# Wrigley is the extreme; domes are zero.
WIND_SENSITIVITY = {
    "CHC": 0.9, "BOS": 0.5, "SF": 0.4, "COL": 0.3, "NYY": 0.5,
    "TOR": 0.0, "ARI": 0.0, "MIA": 0.0, "HOU": 0.0, "TEX": 0.0, "MIL": 0.0,
}
DEFAULT_SENSITIVITY = 0.35


async def wind_at_park(session, team_abbr: str, iso_hour: str):
    """Returns (component_mph, park_runs_adjustment, note) or None.

    component_mph > 0 means blowing OUT to center.
    """
    park = PARKS.get(team_abbr.upper())
    if not park:
        return None
    lat, lon, cf_bearing = park

    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&hourly=wind_speed_10m,wind_direction_10m"
           "&wind_speed_unit=mph&forecast_days=2")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            d = await r.json()
    except Exception as e:
        log.warning(f"open-meteo wind {team_abbr}: {e}")
        return None

    hours = d.get("hourly", {}).get("time", [])
    if iso_hour not in hours:
        return None
    i = hours.index(iso_hour)
    speed = d["hourly"]["wind_speed_10m"][i]
    direction = d["hourly"]["wind_direction_10m"][i]

    # Meteorological direction = where wind comes FROM.
    # Wind blowing out to center comes from behind home plate.
    blowing_toward = (direction + 180) % 360
    delta = math.radians(blowing_toward - cf_bearing)
    component = speed * math.cos(delta)

    sens = WIND_SENSITIVITY.get(team_abbr.upper(), DEFAULT_SENSITIVITY)
    runs = round(component / 10.0 * sens, 2)

    if sens == 0.0:
        note = "dome/retractable — wind irrelevant"
    elif component > 8:
        note = f"{component:.0f}mph blowing OUT — favors over"
    elif component < -8:
        note = f"{abs(component):.0f}mph blowing IN — favors under"
    else:
        note = f"{component:+.0f}mph crosswind/light — negligible"

    return round(component, 1), runs, note
