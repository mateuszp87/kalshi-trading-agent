"""Kalshi Agent Dashboard — Live P&L tracking"""
import os, json, base64, datetime, asyncio, traceback
from urllib.parse import urlparse
from flask import Flask, jsonify, send_from_directory
import aiohttp
from dotenv import load_dotenv
load_dotenv()

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

KEY_ID = os.getenv("KALSHI_API_KEY", "")
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
        if r.status != 200: return None
        return await r.json()

def dollar(val):
    if val is None: return 0.0
    try:
        v = float(val)
        return v / 100 if abs(v) > 1 else v
    except: return 0.0

async def fetch_everything():
    async with aiohttp.ClientSession() as sess:
        bal_data = await api(sess, "/portfolio/balance")
        balance = round(float(bal_data.get("balance", 0)) / 100, 2) if bal_data else 0

        pos_data = await api(sess, "/portfolio/positions")
        raw_positions = pos_data.get("market_positions", []) if pos_data else []

        positions = []
        total_unrealized = 0.0
        total_open_cost = 0.0

        for p in raw_positions:
            ticker = p.get("ticker", "")
            pos_fp = float(p.get("position_fp", 0) or 0)
            if not ticker or pos_fp == 0: continue

            side = "YES" if pos_fp > 0 else "NO"
            count = int(abs(pos_fp))
            exposure = float(p.get("market_exposure_dollars", 0) or 0)

            mkt_data = await api(sess, f"/markets/{ticker}")
            if mkt_data:
                m = mkt_data.get("market", mkt_data)
                yes_bid = dollar(m.get("yes_bid_dollars") or m.get("yes_bid"))
                yes_ask = dollar(m.get("yes_ask_dollars") or m.get("yes_ask"))
                title = m.get("title", ticker)
                close_time = m.get("close_time", "")
            else:
                yes_bid, yes_ask, title, close_time = 0, 0, ticker, ""

            current = yes_bid if side == "YES" else (round(1 - yes_ask, 4) if yes_ask else 0)
            market_value = round(current * count, 2)
            unrealized = round(market_value - exposure, 2)
            entry_avg = round(exposure / count, 4) if count else 0

            positions.append({
                "ticker": ticker, "title": title, "side": side, "count": count,
                "entry_avg": entry_avg, "current_price": current,
                "cost": round(exposure, 2), "market_value": market_value,
                "unrealized_pnl": unrealized, "max_payout": round(float(count), 2),
                "profit_if_win": round(float(count) - exposure, 2),
                "close_time": close_time,
            })
            total_unrealized += unrealized
            total_open_cost += exposure

        settles_data = []
        cursor = None
        for _ in range(10):
            params = {"limit": 100}
            if cursor: params["cursor"] = cursor
            data = await api(sess, "/portfolio/settlements", params)
            if not data: break
            batch = data.get("settlements", [])
            settles_data.extend(batch)
            cursor = data.get("cursor")
            if not cursor or len(batch) < 100: break

        settles = []
        for s in settles_data:
            cost = round(float(s.get("cost", 0)) / 100, 2)
            rev = round(float(s.get("revenue", 0)) / 100, 2)
            if cost <= 0: continue
            ticker = s.get("market_ticker", "")
            if not ticker: continue
            settles.append({
                "ticker": ticker, "side": s.get("side", ""),
                "count": int(s.get("count", 0) or 0),
                "cost": cost, "revenue": rev,
                "pnl": round(rev - cost, 2),
                "time": s.get("created_time", "")[:19].replace("T", " "),
            })
        settles.sort(key=lambda x: x["time"], reverse=True)

        wins = [t for t in settles if t["pnl"] > 0]
        losses = [t for t in settles if t["pnl"] < 0]
        realized_pnl = round(sum(t["pnl"] for t in settles), 2)
        total_pnl = round(realized_pnl + total_unrealized, 2)

        return {
            "balance": balance, "positions": positions, "trades": settles[:50],
            "stats": {
                "realized_pnl": realized_pnl,
                "unrealized_pnl": round(total_unrealized, 2),
                "total_pnl": total_pnl,
                "total_open_cost": round(total_open_cost, 2),
                "wins": len(wins), "losses": len(losses),
                "total_settled": len(settles),
                "win_rate": round(len(wins) / max(len(wins) + len(losses), 1) * 100) if settles else 0,
                "total_wagered": round(sum(t["cost"] for t in settles), 2),
                "total_returned": round(sum(t["revenue"] for t in settles), 2),
                "biggest_win": round(max([t["pnl"] for t in wins], default=0), 2),
                "biggest_loss": round(min([t["pnl"] for t in losses], default=0), 2),
            },
        }

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@app.route("/api/portfolio")
def r_portfolio():
    try:
        now_ts = datetime.datetime.now().timestamp()
        if _CACHE["data"] and now_ts - _CACHE["ts"] < 10:
            return jsonify(_CACHE["data"])
        data = asyncio.run(fetch_everything())
        _CACHE["data"] = data
        _CACHE["ts"] = now_ts
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  Kalshi Dashboard — http://localhost:8080")
    print("="*60 + "\n")
    app.run(port=8080, debug=False, host="0.0.0.0")
