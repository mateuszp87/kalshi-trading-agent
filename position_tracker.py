"""
Kalshi Position Tracker — run locally anytime.
  python3 position_tracker.py

Shows: balance, all-time P&L, open positions, upcoming game markets.
"""

import asyncio, datetime, base64, json, os, urllib.request
from urllib.parse import urlparse
import aiohttp
from dotenv import load_dotenv
load_dotenv()

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
except ImportError:
    print("pip3 install cryptography aiohttp python-dotenv")
    exit(1)

KEY_ID   = os.getenv("KALSHI_API_KEY", "")
BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
KEY_PATHS = [
    os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
    "./kalshi-private.key",
    os.path.expanduser("~/Desktop/kalshi_agent/kalshi-private.key"),
    "/opt/render/project/src/kalshi-private.key",
]

GAME_SERIES = [
    "KXNBAGAME","KXNHLGAME","KXUCLGAME","KXMLBGAME",
    "KXNBA1HWINNER","KXNBA2HWINNER","KXEPLGAME","KXSERIEAGAME",
    "KXLALIGAGAME","KXBUNDESLIGAGAME","KXMLSGAME",
]


def load_key():
    for p in KEY_PATHS:
        if p and os.path.exists(p):
            with open(p, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    raise FileNotFoundError(f"Key not found. Tried: {[p for p in KEY_PATHS if p]}")


def make_headers(method, path):
    key = load_key()
    ts  = str(int(datetime.datetime.now().timestamp() * 1000))
    sp  = urlparse(BASE_URL + path).path.split("?")[0]
    sig = key.sign(f"{ts}{method}{sp}".encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts}


async def api(session, path, params=None):
    async with session.get(BASE_URL + path, headers=make_headers("GET", path), params=params) as r:
        r.raise_for_status()
        return await r.json()


def time_label(ct):
    if not ct: return "?"
    try:
        dt = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
        h  = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600
        if h < 0:   return "CLOSED"
        if h <= 3:  return f"🔥 {h:.1f}h (NOW)"
        if h <= 12: return f"⚡ {h:.1f}h (TODAY)"
        if h <= 48: return f"📅 {h:.0f}h"
        if h <= 168:return f"📆 {h/24:.1f}d"
        return      f"🗓  {h/24:.0f}d"
    except:
        return ct[:10]


async def main():
    print("\n╔══════════════════════════════════════════════╗")
    print("║       Kalshi Portfolio Tracker               ║")
    print("╚══════════════════════════════════════════════╝\n")

    async with aiohttp.ClientSession() as sess:

        # Balance
        try:
            d = await api(sess, "/portfolio/balance")
            bal = round(float(d.get("balance", 0)) / 100, 2)
            print(f"  💰  Balance : ${bal:.2f}")
        except Exception as e:
            print(f"  ❌  Balance error: {e}")

        # Local P&L log
        pnl = {}
        try:
            with open("pnl_log.json") as f:
                pnl = json.load(f)
        except:
            pass

        atp   = pnl.get("all_time_pnl", 0)
        wins  = pnl.get("wins", 0)
        losses= pnl.get("losses", 0)
        total = wins + losses
        wr    = round(wins / total * 100) if total else 0
        sign  = "📈" if atp >= 0 else "📉"
        print(f"  {sign}  All-time P&L: ${atp:+.2f}")
        print(f"  🎯  Win rate:     {wr}%  ({wins}W / {losses}L / {total} trades)\n")

        # New settlements
        try:
            known = set(pnl.get("known_ids", []))
            sdata = await api(sess, "/portfolio/settlements")
            new_s = [s for s in sdata.get("settlements", [])
                     if (s.get("id") or s.get("market_ticker","")) not in known]
            if new_s:
                print(f"  🆕  {len(new_s)} NEW SETTLEMENTS:")
                for s in new_s:
                    rev  = float(s.get("revenue", 0)) / 100
                    cost = float(s.get("cost", 0)) / 100
                    p2   = round(rev - cost, 2)
                    t    = s.get("market_ticker", "")
                    tag  = "✅ WIN" if p2 > 0 else "❌ LOSS"
                    print(f"      {tag} | {t[:42]:<42} | ${p2:+.2f}")
                print()
            else:
                print("  ℹ️   No new settlements.\n")
        except Exception as e:
            print(f"  ⚠️  Settlements error: {e}\n")

        # Open positions
        try:
            pdata = await api(sess, "/portfolio/positions")
            pos   = pdata.get("market_positions", pdata.get("positions", []))
            if pos:
                print(f"  📊  OPEN POSITIONS ({len(pos)})\n")
                print(f"  {'Ticker':<42} {'Side':>5} {'Qty':>4}  {'Closes':<20} {'Value':>7}")
                print(f"  {'─'*42} {'─'*5} {'─'*4}  {'─'*20} {'─'*7}")
                for p in pos:
                    ticker = p.get("market_ticker", p.get("ticker", ""))
                    yes_ct = int(p.get("position", p.get("yes_count", 0)) or 0)
                    no_ct  = int(p.get("no_count", 0) or 0)
                    val    = round(float(p.get("market_value", p.get("value", 0)) or 0) / 100, 2)
                    side   = "YES" if yes_ct > 0 else "NO"
                    qty    = yes_ct if yes_ct > 0 else no_ct
                    close_lbl = "?"
                    try:
                        mkt = await api(sess, f"/markets/{ticker}")
                        ct  = mkt.get("market", mkt).get("close_time", "")
                        close_lbl = time_label(ct)
                    except:
                        pass
                    short = (ticker[:40] + "..") if len(ticker) > 42 else ticker
                    print(f"  {short:<42} {side:>5} {qty:>4}  {close_lbl:<20} ${val:>6.2f}")
            else:
                print("  ℹ️   No open positions.\n")
        except Exception as e:
            print(f"  ⚠️  Positions error: {e}\n")

        # Upcoming game markets (next 48h, priced)
        print(f"\n  🏀  UPCOMING GAME MARKETS (next 48h)\n")
        now     = datetime.datetime.now(datetime.timezone.utc)
        cutoff  = now + datetime.timedelta(hours=48)
        upcoming = []
        for series in GAME_SERIES:
            try:
                url = f"{BASE_URL}/markets?series_ticker={series}&status=open&limit=10"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = json.loads(r.read())
                for m in data.get("markets", []):
                    ct = m.get("close_time", "")
                    if not ct: continue
                    try:
                        dt = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                        if dt > cutoff: continue
                    except:
                        continue
                    bid = float(m.get("yes_bid_dollars", 0) or 0)
                    ask = float(m.get("yes_ask_dollars", 0) or 0)
                    if bid <= 0 and ask <= 0: continue
                    mid  = round((bid + ask) / 2, 2) if bid and ask else (ask or bid)
                    vol  = float(m.get("volume_fp", 0) or 0)
                    h_left = (dt - now).total_seconds() / 3600
                    upcoming.append({
                        "title":  m.get("title", "")[:55],
                        "ticker": m.get("ticker", ""),
                        "mid": mid, "bid": bid, "ask": ask,
                        "vol": vol, "close": ct, "h": h_left,
                    })
            except:
                pass

        upcoming.sort(key=lambda x: x["h"])
        if upcoming:
            print(f"  {'Title':<55} {'Mid':>5} {'Vol':>9}  Closes")
            print(f"  {'─'*55} {'─'*5} {'─'*9}  {'─'*15}")
            for g in upcoming[:20]:
                print(f"  {g['title']:<55} {g['mid']:>5.2f} {g['vol']:>9,.0f}  {time_label(g['close'])}")
        else:
            print("  No priced game markets in next 48h — check back closer to game time.")

        print()

if __name__ == "__main__":
    asyncio.run(main())
