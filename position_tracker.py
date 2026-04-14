"""
Position tracker — checks Kalshi portfolio for resolved positions and logs P&L
Run alongside the main agent or call periodically
"""

import asyncio
import logging
import datetime
import base64
from urllib.parse import urlparse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
import aiohttp
from dotenv import load_dotenv
import os
import json

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

KEY_ID = os.getenv("KALSHI_API_KEY")
KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi-private.key")
BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
RESULTS_FILE = "trade_results.json"

def make_headers(method, path):
    with open(KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    sign_path = urlparse(BASE_URL + path).path.split("?")[0]
    msg = f"{ts}{method}{sign_path}".encode()
    sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

async def get_portfolio():
    async with aiohttp.ClientSession() as session:
        # Get balance
        headers = make_headers("GET", "/portfolio/balance")
        async with session.get(f"{BASE_URL}/portfolio/balance", headers=headers) as r:
            bal = await r.json()
            balance = round(float(bal.get("balance", 0)) / 100, 2)

        # Get positions
        headers = make_headers("GET", "/portfolio/positions")
        async with session.get(f"{BASE_URL}/portfolio/positions", headers=headers) as r:
            pos_data = await r.json()
            positions = pos_data.get("market_positions", pos_data.get("positions", []))

        # Get settled positions (resolved)
        headers = make_headers("GET", "/portfolio/settlements")
        async with session.get(f"{BASE_URL}/portfolio/settlements", headers=headers) as r:
            settle_data = await r.json()
            settlements = settle_data.get("settlements", [])

        return balance, positions, settlements

def load_results():
    try:
        with open(RESULTS_FILE) as f:
            return json.load(f)
    except:
        return {"trades": [], "total_pnl": 0.0, "wins": 0, "losses": 0}

def save_results(data):
    with open(RESULTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

async def main():
    print("\n── Kalshi Portfolio Tracker ──────────────────────")
    try:
        balance, positions, settlements = await get_portfolio()
        results = load_results()

        print(f"  Account balance : ${balance:.2f}")
        print(f"  Open positions  : {len(positions)}")
        print(f"  Settled trades  : {len(settlements)}")
        print()

        # Process new settlements
        known_ids = {t.get("id") for t in results["trades"]}
        new_settlements = []

        for s in settlements:
            sid = s.get("id", s.get("market_ticker", ""))
            if sid not in known_ids:
                revenue = float(s.get("revenue", 0)) / 100
                cost = float(s.get("cost", 0)) / 100
                pnl = round(revenue - cost, 2)
                won = pnl > 0

                trade = {
                    "id": sid,
                    "ticker": s.get("market_ticker", ""),
                    "side": s.get("side", ""),
                    "count": s.get("count", 0),
                    "cost": cost,
                    "revenue": revenue,
                    "pnl": pnl,
                    "won": won,
                    "settled_at": s.get("created_time", ""),
                }
                results["trades"].append(trade)
                results["total_pnl"] = round(results["total_pnl"] + pnl, 2)
                if won:
                    results["wins"] += 1
                else:
                    results["losses"] += 1
                new_settlements.append(trade)

        if new_settlements:
            save_results(results)
            print(f"  NEW SETTLED TRADES:")
            for t in new_settlements:
                emoji = "WIN" if t["won"] else "LOSS"
                print(f"    [{emoji}] {t['ticker']} | P&L: ${t['pnl']:+.2f} | cost ${t['cost']:.2f} → revenue ${t['revenue']:.2f}")
        else:
            print("  No new settled trades since last check.")

        print()
        print(f"  ── All-time results ─────────────────────────")
        print(f"  Total P&L  : ${results['total_pnl']:+.2f}")
        total = results["wins"] + results["losses"]
        wr = round(results["wins"] / total * 100) if total > 0 else 0
        print(f"  Win rate   : {wr}% ({results['wins']}W / {results['losses']}L)")
        print(f"  Trades     : {total}")

        # Show open positions
        if positions:
            print()
            print(f"  ── Open positions ───────────────────────────")
            for p in positions:
                ticker = p.get("market_ticker", "")
                yes_count = p.get("position", p.get("yes_count", 0))
                no_count = p.get("no_count", 0)
                value = float(p.get("market_value", p.get("value", 0)) or 0) / 100
                print(f"    {ticker} | YES:{yes_count} NO:{no_count} | value: ${value:.2f}")

        print("──────────────────────────────────────────────\n")

    except Exception as e:
        print(f"  Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
