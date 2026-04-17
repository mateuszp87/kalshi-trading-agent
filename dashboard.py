"""Kalshi Agent Dashboard — real-time view of portfolio, P&L, positions, markets."""
import os, json, base64, datetime, asyncio
from urllib.parse import urlparse
from flask import Flask, jsonify, send_from_directory
import aiohttp
from dotenv import load_dotenv
load_dotenv()

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

KEY_ID   = os.getenv("KALSHI_API_KEY", "")
BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
KEY_PATHS = [
    os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
    "./kalshi-private.key",
    os.path.expanduser("~/Desktop/kalshi_agent/kalshi-private.key"),
]

app = Flask(__name__)
_CACHE = {"data": None, "ts": 0}

def load_key():
    for p in KEY_PATHS:
        if p and os.path.exists(p):
            with open(p, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    raise FileNotFoundError("Kalshi key not found")

def headers(method, path):
    key = load_key()
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    sp = urlparse(BASE_URL + path).path.split("?")[0]
    sig = key.sign(f"{ts}{method}{sp}".encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts}

async def api(sess, path, params=None):
    async with sess.get(BASE_URL + path, headers=headers("GET", path), params=params) as r:
        r.raise_for_status()
        return await r.json()

def price(val):
    if val is None: return 0.0
    try: v = float(val)
    except: return 0.0
    return v if v <= 1.0 else v / 100

async def fetch_portfolio():
    async with aiohttp.ClientSession() as sess:
        bal_data = await api(sess, "/portfolio/balance")
        balance = round(float(bal_data.get("balance", 0)) / 100, 2)

        pos_data = await api(sess, "/portfolio/positions")
        raw_positions = pos_data.get("market_positions", pos_data.get("positions", []))

        # Enrich positions with live market data
        positions = []
        for p in raw_positions:
            ticker = p.get("market_ticker", p.get("ticker", ""))
            yes_ct = int(p.get("position", p.get("yes_count", 0)) or 0)
            no_ct  = int(p.get("no_count", 0) or 0)
            if not ticker or (yes_ct == 0 and no_ct == 0): continue
            try:
                mkt = await api(sess, f"/markets/{ticker}")
                m = mkt.get("market", mkt)
                yes_bid = price(m.get("yes_bid_dollars") or m.get("yes_bid"))
                yes_ask = price(m.get("yes_ask_dollars") or m.get("yes_ask"))
                title = m.get("title", ticker)
                close_time = m.get("close_time", "")
            except Exception:
                yes_bid, yes_ask, title, close_time = 0, 0, ticker, ""
            side = "YES" if yes_ct > 0 else "NO"
            count = yes_ct if yes_ct > 0 else no_ct
            current = yes_bid if side == "YES" else (round(1 - yes_ask, 4) if yes_ask else 0)
            val = round(float(p.get("market_value", p.get("value", 0)) or 0) / 100, 2)
            positions.append({
                "ticker": ticker, "title": title, "side": side, "count": count,
                "current_price": current, "yes_bid": yes_bid, "yes_ask": yes_ask,
                "market_value": val, "close_time": close_time,
            })

        # Pull settlements with pagination
        settlements = []
        cursor = None
        for _ in range(5):
            params = {"limit": 100}
            if cursor: params["cursor"] = cursor
            data = await api(sess, "/portfolio/settlements", params)
            batch = data.get("settlements", [])
            settlements.extend(batch)
            cursor = data.get("cursor")
            if not cursor or len(batch) < 100: break

        real = []
        for s in settlements:
            cost = round(float(s.get("cost", 0)) / 100, 2)
            rev  = round(float(s.get("revenue", 0)) / 100, 2)
            if cost == 0 and rev == 0: continue
            real.append({
                "ticker": s.get("market_ticker", ""),
                "side": s.get("side", ""),
                "count": s.get("count", 0),
                "cost": cost, "revenue": rev, "pnl": round(rev - cost, 2),
                "time": s.get("created_time", "")[:16].replace("T", " "),
            })
        real.sort(key=lambda x: x["time"], reverse=True)

        wins = [t for t in real if t["pnl"] > 0]
        losses = [t for t in real if t["pnl"] < 0]
        total_wagered = round(sum(t["cost"] for t in real), 2)
        total_returned = round(sum(t["revenue"] for t in real), 2)
        net_pnl = round(total_returned - total_wagered, 2)

        return {
            "balance": balance,
            "positions": positions,
            "trades": real[:50],
            "stats": {
                "all_time_pnl": net_pnl,
                "wins": len(wins), "losses": len(losses),
                "total_trades": len(real),
                "win_rate": round(len(wins) / max(len(wins) + len(losses), 1) * 100),
                "total_wagered": total_wagered,
                "total_returned": total_returned,
                "biggest_win": round(max([t["pnl"] for t in wins], default=0), 2),
                "biggest_loss": round(min([t["pnl"] for t in losses], default=0), 2),
            }
        }

async def fetch_markets():
    """All upcoming game markets in next 21 days with liquidity."""
    series = ["KXNBAGAME","KXNHLGAME","KXUCLGAME","KXMLBGAME",
              "KXNBA1HWINNER","KXNBA2HWINNER","KXMLBF5","KXNBAPLAYOFFPTS",
              "KXNHLSERIES","KXNBASERIESSPREAD","KXNBASERIESGAMES",
              "KXEPLGAME","KXSERIEAGAME","KXLALIGAGAME"]
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now + datetime.timedelta(days=21)
    markets = []
    async with aiohttp.ClientSession() as sess:
        for s in series:
            try:
                async with sess.get(BASE_URL + "/markets",
                                    params={"series_ticker": s, "status": "open", "limit": 15}) as r:
                    data = await r.json()
                for m in data.get("markets", []):
                    ct = m.get("close_time", "")
                    if not ct: continue
                    try:
                        dt = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                        h = (dt - now).total_seconds() / 3600
                        if h < 0 or dt > cutoff: continue
                    except: continue
                    bid = price(m.get("yes_bid_dollars"))
                    ask = price(m.get("yes_ask_dollars"))
                    if bid <= 0 and ask <= 0: continue
                    mid = round((bid + ask) / 2, 2) if bid and ask else (ask or bid)
                    spread = round(ask - bid, 3) if bid and ask else 1
                    vol = float(m.get("volume_fp", 0) or 0)
                    if vol < 50 and spread > 0.10: continue
                    score = ((vol + 100) / 10000) * (1 / max(spread, 0.01)) * (1000 / max(h, 1))
                    markets.append({
                        "title": m.get("title", ""), "ticker": m.get("ticker", ""),
                        "mid": mid, "bid": bid, "ask": ask,
                        "spread": spread, "volume": vol,
                        "hours_left": round(h, 1), "close_time": ct,
                        "series": s, "score": round(score, 1),
                    })
            except Exception as e:
                print(f"[markets] {s}: {e}")
    markets.sort(key=lambda x: x["score"], reverse=True)
    return markets[:40]

def load_agent_status():
    for path in ["./trade_log.json", os.path.expanduser("~/Desktop/kalshi_agent/trade_log.json")]:
        if os.path.exists(path):
            try:
                with open(path) as f: return json.load(f)
            except: pass
    return None

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/api/portfolio")
def r_portfolio():
    try:
        now_ts = datetime.datetime.now().timestamp()
        if _CACHE["data"] and now_ts - _CACHE["ts"] < 20:
            return jsonify(_CACHE["data"])
        data = asyncio.run(fetch_portfolio())
        _CACHE["data"] = data
        _CACHE["ts"] = now_ts
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/markets")
def r_markets():
    try:
        return jsonify({"markets": asyncio.run(fetch_markets())})
    except Exception as e:
        return jsonify({"error": str(e), "markets": []}), 500

@app.route("/api/agent")
def r_agent():
    status = load_agent_status()
    if status:
        return jsonify({"online": True, **status})
    return jsonify({"online": False})

if __name__ == "__main__":
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  Kalshi Agent Dashboard                              ║")
    print("║  Open: http://localhost:8080                         ║")
    print("╚══════════════════════════════════════════════════════╝\n")
    app.run(port=8080, debug=False)
