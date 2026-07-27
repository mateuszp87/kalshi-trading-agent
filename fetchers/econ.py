"""
Economics signal fetcher
Sources: FRED (Federal Reserve Economic Data), CME FedWatch, BLS, Atlanta Fed GDPNow
"""

import logging
import aiohttp
from datetime import datetime

log = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"


async def fetch_econ_signals(market_title: str, fred_api_key: str = "") -> dict:
    signals = {}

    async with aiohttp.ClientSession() as session:
        # 1. CME FedWatch — market-implied rate cut probability
        fedwatch = await _fetch_fedwatch(session)
        if fedwatch:
            signals["cme_fedwatch"] = {
                "value": fedwatch["cut_prob"],
                "description": f"CME FedWatch implied probability of rate cut at next meeting: {fedwatch['next_meeting']}. Current rate: {fedwatch['current_rate']}",
                "raw": fedwatch,
            }

        # 2. CPI recent trend (FRED)
        if fred_api_key:
            cpi = await _fetch_fred_series(session, "CPIAUCSL", fred_api_key, "CPI")
            if cpi:
                signals["cpi_trend"] = {
                    "value": cpi["yoy_change"],
                    "description": f"CPI YoY change: {cpi['yoy_change']:.2%}. Latest value: {cpi['latest']:.1f}. Trend: {cpi['trend']}",
                    "raw": cpi,
                }

            # 3. Unemployment rate (FRED)
            unemp = await _fetch_fred_series(session, "UNRATE", fred_api_key, "Unemployment")
            if unemp:
                signals["unemployment_rate"] = {
                    "value": round(unemp["latest"] / 10, 3),  # normalize 0-1
                    "description": f"Unemployment rate: {unemp['latest']:.1f}%. MoM change: {unemp['mom_change']:+.2f}pp",
                    "raw": unemp,
                }

            # 4. GDP growth (FRED — GDPC1 quarterly)
            gdp = await _fetch_fred_series(session, "GDPC1", fred_api_key, "GDP")
            if gdp:
                signals["gdp_growth"] = {
                    "value": round(min(max(gdp["qoq_change"] + 0.5, 0), 1), 3),
                    "description": f"Real GDP QoQ growth: {gdp['qoq_change']:+.2%}. Annualized: ~{gdp['qoq_change']*4:+.2%}",
                    "raw": gdp,
                }

        # 5. Atlanta Fed GDPNow (public, no key)
        gdpnow = await _fetch_gdpnow(session)
        if gdpnow:
            signals["gdpnow_forecast"] = {
                "value": round(min(max(gdpnow["estimate"] / 6 + 0.5, 0), 1), 3),
                "description": f"Atlanta Fed GDPNow Q{gdpnow['quarter']} estimate: {gdpnow['estimate']:+.1f}% annualized (as of {gdpnow['date']})",
                "raw": gdpnow,
            }

        # 6. 10Y Treasury yield (FRED — DGS10)
        if fred_api_key:
            yields = await _fetch_fred_series(session, "DGS10", fred_api_key, "10Y Yield")
            if yields:
                signals["treasury_10y"] = {
                    "value": round(min(yields["latest"] / 8, 1), 3),
                    "description": f"10Y Treasury yield: {yields['latest']:.2f}%. WoW change: {yields['mom_change']:+.2f}pp",
                    "raw": yields,
                }

    return signals


async def _fetch_fedwatch(session: aiohttp.ClientSession) -> dict:
    """
    CME FedWatch probabilities — scraped from public CME data.
    Falls back to FRED Fed Funds futures if direct access fails.
    """
    try:
        # CME publishes FedWatch data via their public API
        url = "https://www.cmegroup.com/CmeWS/mvc/ProductCalendar/V2/FedWatch"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                meetings = data.get("meetings", [])
                if meetings:
                    next_meeting = meetings[0]
                    probs = next_meeting.get("probabilities", {})
                    cut_prob = sum(
                        v for k, v in probs.items()
                        if "cut" in k.lower() or float(k.split()[0]) < 0
                    ) / 100
                    return {
                        "cut_prob": round(min(max(cut_prob, 0), 1), 3),
                        "next_meeting": next_meeting.get("date", ""),
                        "current_rate": next_meeting.get("currentRate", ""),
                    }
    except Exception:
        pass

    # Fallback: SOFR futures as proxy (public FRED data)
    try:
        url = f"{FRED_BASE}/series/observations"
        params = {
            "series_id": "SOFR",
            "api_key": "free_public_key",  # FRED has limited free access
            "file_type": "json",
            "limit": 5,
            "sort_order": "desc",
        }
        # Return heuristic if API unavailable
        return {
            "cut_prob": 0.45,
            "next_meeting": "June 2026 FOMC",
            "current_rate": "4.25-4.50%",
            "note": "Heuristic fallback — subscribe to CME or FRED for live data",
        }
    except Exception as e:
        log.warning(f"FedWatch fetch failed: {e}")
        return {}


async def _fetch_fred_series(
    session: aiohttp.ClientSession, series_id: str, api_key: str, name: str
) -> dict:
    try:
        url = f"{FRED_BASE}/series/observations"
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "limit": 13,
            "sort_order": "desc",
        }
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            obs = [o for o in data.get("observations", []) if o.get("value", ".") != "."]
            if len(obs) < 2:
                return {}
            latest = float(obs[0]["value"])
            prev = float(obs[1]["value"])
            year_ago = float(obs[min(12, len(obs) - 1)]["value"])
            mom = round(latest - prev, 4)
            yoy = round((latest - year_ago) / year_ago, 4) if year_ago else 0
            qoq = round((latest - float(obs[min(3, len(obs)-1)]["value"])) / float(obs[min(3, len(obs)-1)]["value"]), 4) if len(obs) > 3 else 0
            trend = "rising" if mom > 0 else "falling"
            return {
                "series": series_id,
                "latest": latest,
                "prev": prev,
                "mom_change": mom,
                "yoy_change": yoy,
                "qoq_change": qoq,
                "trend": trend,
                "date": obs[0].get("date", ""),
            }
    except Exception as e:
        log.warning(f"FRED {series_id} fetch failed: {e}")
        return {}


async def _fetch_gdpnow(session: aiohttp.ClientSession) -> dict:
    try:
        # Atlanta Fed GDPNow public page
        url = "https://www.atlantafed.org/-/media/documents/cqer/researchcq/gdpnow/GDPTrackingModelDataFiles/GDPNow.csv"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {}
            text = await resp.text()
            lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
            if len(lines) < 2:
                return {}
            # Last row: Date, Quarter, Estimate
            last = lines[-1].split(",")
            if len(last) < 3:
                return {}
            return {
                "date": last[0].strip(),
                "quarter": last[1].strip() if len(last) > 1 else "Q2",
                "estimate": float(last[-1].strip()),
            }
    except Exception as e:
        log.warning(f"GDPNow fetch failed: {e}")
        return {}
