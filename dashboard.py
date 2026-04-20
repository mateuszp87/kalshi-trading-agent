"""
Kalshi Dashboard — reads from Kalshi API using your existing auth.
Shows: balance, open position_fp_fps with payouts, orders/settlements past 7 days, live markets.
"""
import os, json, base64, datetime, asyncio, traceback
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

# Filter cutoff for orders and settlements
CUTOFF = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=4)).isoformat().replace("+00:00", "Z")

app = Flask(__name__)
_CACHE = {"data": None, "ts": 0}

def load_key():
    for p in KEY_PATHS:
        if p and os.path.exists(p):
            with open(p, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    raise FileNotFoundError("Kalshi key not found at any of: " + ", ".join([p for p in KEY_PATHS if p]))

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
    try:
        async with sess.get(BASE_URL + path, headers=headers("GET", path), params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception as e:
        print(f"[api] {path}: {e}")
        return None

def dollars(val):
    if val is None: return 0.0
    try:
        v = float(val)
        return v / 100 if abs(v) > 1 else v
    except: return 0.0


async def fetch_all():
    async with aiohttp.ClientSession() as sess:
        # 1. Balance
        bal = await api(sess, "/portfolio/balance")
        balance = round(float(bal.get("balance", 0)) / 100, 2) if bal else 0

        # 2. Open position_fp_fps enriched with live market data
        pos_data = await api(sess, "/portfolio/position_fp_fps")
        raw = pos_data.get("market_position_fp_fps", []) if pos_data else []

        position_fp_fps = []
        total_unrealized = 0.0
        total_open_cost = 0.0
        for p in raw:
            ticker = p.get("ticker", "")
            pos_fp = float(p.get("position_fp_fp_fp", 0) or 0)
            if not ticker or pos_fp == 0: continue

            side = "YES" if pos_fp > 0 else "NO"
            count = int(abs(pos_fp))
            cost = float(p.get("market_exposure_dollars", 0) or 0)

            mkt = await api(sess, f"/markets/{ticker}")
            if mkt:
                m = mkt.get("market", mkt)
                yes_bid = dollars(m.get("yes_bid_dollars") or m.get("yes_bid"))
                yes_ask = dollars(m.get("yes_ask_dollars") or m.get("yes_ask"))
                title = m.get("title", ticker)
                close_time = m.get("close_time", "")
            else:
                yes_bid, yes_ask, title, close_time = 0, 0, ticker, ""

            current = yes_bid if side == "YES" else (round(1 - yes_ask, 4) if yes_ask else 0)
            market_value = round(current * count, 2)
            max_payout = float(count)
            profit_if_win = round(max_payout - cost, 2)
            unrealized = round(market_value - cost, 2)

            position_fp_fps.append({
                "ticker": ticker, "title": title, "side": side, "count": count,
                "entry_avg": round(cost / count, 4) if count else 0,
                "current_price": current,
                "cost": round(cost, 2),
                "market_value": market_value,
                "unrealized_pnl_dollars": unrealized,
                "max_payout": round(max_payout, 2),
                "profit_if_win": profit_if_win,
                "close_time": close_time,
            })
            total_unrealized += unrealized
            total_open_cost += cost

        # 3. Orders past 7 days
        orders_data = await api(sess, "/portfolio/orders", {"limit": 200, "status": "executed"})
        raw_orders = orders_data.get("orders", []) if orders_data else []
        orders = []
        title_cache = {}  # shared cache for market titles
        for o in raw_orders:
            created = o.get("created_time", "")
            if not created or created < CUTOFF: continue
            ticker = o.get("ticker", "")
            if not ticker: continue
            filled = int(o.get("filled_count", 0) or 0)
            if filled == 0: continue
            side = (o.get("side", "") or "").upper()
            action = (o.get("action", "") or "").lower()
            yes_price_dollars = dollars(o.get("yes_price_dollars") or o.get("yes_price_dollars"))
            no_price_dollars = dollars(o.get("no_price_dollars") or o.get("no_price_dollars"))
            fill_price = yes_price_dollars if side == "YES" else no_price_dollars
            if not fill_price:
                fill_price = dollars(o.get("fill_price") or o.get("avg_price"))
            # Fetch market title
            if ticker in title_cache:
                title = title_cache[ticker]
            else:
                mkt = await api(sess, f"/markets/{ticker}")
                if mkt:
                    m = mkt.get("market", mkt)
                    title = m.get("title", ticker) or ticker
                else:
                    title = ticker
                title_cache[ticker] = title

            orders.append({
                "ticker": ticker,
                "title": title,
                "side": side,
                "action": action.upper() if action else "BUY",
                "count": filled, "price": fill_price,
                "total": round(fill_price * filled, 2),
                "time": created[:19].replace("T", " "),
            })
        orders.sort(key=lambda x: x["time"], reverse=True)

        # 4. Settlements past 7 days
        settles_raw = []
        cursor = None
        for _ in range(5):
            params = {"limit": 100}
            if cursor: params["cursor"] = cursor
            data = await api(sess, "/portfolio/settlements", params)
            if not data: break
            batch = data.get("settlements", [])
            settles_raw.extend(batch)
            cursor = data.get("cursor")
            if not cursor or len(batch) < 100: break

        settles = []
        for s in settles_raw:
            settled = s.get("settled_time", "")
            ticker = s.get("ticker", "")
            if not settled or not ticker: continue
            if settled < CUTOFF: continue

            yes_cost = float(s.get("yes_total_cost_dollars", 0) or 0)
            no_cost = float(s.get("no_total_cost_dollars", 0) or 0)
            yes_ct = float(s.get("yes_count_fp", 0) or 0)
            no_ct = float(s.get("no_count_fp", 0) or 0)

            if yes_ct > 0:
                side = "YES"; cost = round(yes_cost, 2); count = int(yes_ct)
            elif no_ct > 0:
                side = "NO"; cost = round(no_cost, 2); count = int(no_ct)
            else:
                continue

            if cost <= 0: continue
            rev = round(float(s.get("revenue", 0)) / 100, 2)
            pnl = round(rev - cost, 2)

            # Fetch market title for human-readable description
            if ticker in title_cache:
                title = title_cache[ticker]
            else:
                mkt = await api(sess, f"/markets/{ticker}")
                if mkt:
                    m = mkt.get("market", mkt)
                    title = m.get("title", ticker) or ticker
                    # Also grab event title for more context
                    subtitle = m.get("yes_sub_title") or m.get("subtitle") or ""
                    if subtitle and subtitle.lower() not in title.lower():
                        title = f"{title} · {subtitle}"
                else:
                    title = ticker
                title_cache[ticker] = title

            # Describe the exact bet in plain English
            result = s.get("market_result", "")
            outcome = "WON" if (rev > cost) else "LOST"
            bet_description = f"Bet {side} on: {title}"

            settles.append({
                "ticker": ticker,
                "title": title,
                "description": bet_description,
                "outcome": outcome,
                "result": result.upper() if result else "",
                "side": side,
                "count": count,
                "cost": cost,
                "revenue": rev,
                "pnl": pnl,
                "time": settled[:19].replace("T", " "),
            })
        settles.sort(key=lambda x: x["time"], reverse=True)

        wins = [t for t in settles if t["pnl"] > 0]
        losses = [t for t in settles if t["pnl"] < 0]
        realized = round(sum(t["pnl"] for t in settles), 2)
        total_pnl = round(realized + total_unrealized, 2)

        return {
            "balance": balance,
            "position_fp_fps": position_fp_fps,
            "orders": orders[:50],
            "settlements": settles[:50],
            "stats": {
                "realized_pnl_dollars": realized,
                "unrealized_pnl_dollars": round(total_unrealized, 2),
                "total_pnl": total_pnl,
                "total_open_cost": round(total_open_cost, 2),
                "wins": len(wins),
                "losses": len(losses),
                "total_settled": len(settles),
                "total_orders": len(orders),
                "win_rate": round(len(wins) / max(len(wins) + len(losses), 1) * 100) if settles else 0,
                "biggest_win": round(max([t["pnl"] for t in wins], default=0), 2),
                "biggest_loss": round(min([t["pnl"] for t in losses], default=0), 2),
            },
        }


async def fetch_markets():
    series = ["KXNBAGAME","KXNHLGAME","KXUCLGAME","KXMLBGAME","KXEPLGAME",
              "KXNBA1HWINNER","KXMLBF5","KXSERIEAGAME","KXLALIGAGAME"]
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now + datetime.timedelta(days=14)
    markets = []
    async with aiohttp.ClientSession() as sess:
        for s in series:
            try:
                async with sess.get(BASE_URL + "/markets",
                                    params={"series_ticker": s, "status": "open", "limit": 12}) as r:
                    data = await r.json()
                for m in data.get("markets", []):
                    ct = m.get("close_time", "")
                    if not ct: continue
                    try:
                        dt = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                        h = (dt - now).total_seconds() / 3600
                        if h < 0 or dt > cutoff: continue
                    except: continue
                    bid = dollars(m.get("yes_bid_dollars"))
                    ask = dollars(m.get("yes_ask_dollars"))
                    if bid <= 0 and ask <= 0: continue
                    mid = round((bid + ask) / 2, 2) if bid and ask else (ask or bid)
                    spread = round(ask - bid, 3) if bid and ask else 1
                    vol = float(m.get("volume_fp", 0) or 0)
                    if vol < 100: continue
                    markets.append({
                        "title": m.get("title", "")[:70],
                        "ticker": m.get("ticker", ""),
                        "mid": mid, "bid": bid, "ask": ask,
                        "spread": spread, "volume": vol,
                        "hours_left": round(h, 1), "close_time": ct,
                        "category": s.replace("KX", "").replace("GAME", "").replace("SERIES", " series"),
                    })
            except Exception as e:
                print(f"[markets] {s}: {e}")
    markets.sort(key=lambda x: x["volume"], reverse=True)
    return markets[:50]


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
        if _CACHE["data"] and now_ts - _CACHE["ts"] < 10:
            return jsonify(_CACHE["data"])
        data = asyncio.run(fetch_all())
        _CACHE["data"] = data
        _CACHE["ts"] = now_ts
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/markets")
def r_markets():
    try:
        return jsonify({"markets": asyncio.run(fetch_markets())})
    except Exception as e:
        return jsonify({"error": str(e), "markets": []}), 500


if __name__ == "__main__":
    print("\n" + "="*60)
    print(" Kalshi Dashboard")
    print(" Open: http://localhost:8080")
    print(f" Cutoff: {CUTOFF[:10]}")
    print("="*60 + "\n")
    app.run(port=8080, debug=False, host="0.0.0.0")
