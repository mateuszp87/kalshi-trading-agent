"""Kalshi agent dashboard — reconstructed from fills + settlements.

Cutoff: 2026-07-08. Nothing before that date is counted.

Three balances, deliberately separate:
  CASH       — settled, spendable (Kalshi's number)
  AT RISK    — cost basis of open positions
  MARK       — open positions valued at current bid

Realized P&L counts settled markets only. Once a position settles its
number is final. Win/loss is settled trades only — open positions are
neither until they resolve.
"""
import asyncio, datetime, json, re
from collections import defaultdict
from flask import Flask, jsonify, send_file
from config import AgentConfig
from kalshi_client import KalshiClient

app = Flask(__name__)
cfg = AgentConfig()
CUTOFF = datetime.datetime(2026, 7, 8, 0, 0, 0, tzinfo=datetime.timezone.utc)
CUTOFF_TS = int(CUTOFF.timestamp())


def parse_ts(ts):
    """Kalshi sometimes returns 5-digit microseconds; fromisoformat wants 6."""
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    m = re.search(r"\.(\d+)\+", s)
    if m:
        micros = m.group(1)
        s = s.replace(f".{micros}+", f".{micros.ljust(6,'0')[:6]}+")
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def after_cutoff(ts):
    d = parse_ts(ts)
    return d is not None and d >= CUTOFF


SERIES_NAMES = {
    "KXMLBF5": "MLB 1st 5 innings", "KXMLBGAME": "MLB game",
    "KXMLBSPREAD": "MLB run line", "KXMLBTOTAL": "MLB total runs",
    "KXNBAGAME": "NBA game", "KXNHLGAME": "NHL game",
    "KXWNBAGAME": "WNBA game", "KXWNBASPREAD": "WNBA spread",
    "KXWNBATOTAL": "WNBA total",
    "KXWCGAME": "World Cup", "KXWCTOTAL": "World Cup total",
    "KXWCSPREAD": "World Cup spread",
    "KXATPMATCH": "ATP tennis", "KXWTAMATCH": "WTA tennis",
    "KXUELGAME": "Europa League", "KXMLSGAME": "MLS",
    "KXHIGHNY": "NYC high temp", "KXHIGHCHI": "Chicago high temp",
    "KXHIGHLAX": "LA high temp", "KXHIGHDEN": "Denver high temp",
    "KXCPI": "CPI", "KXCPIYOY": "CPI year-over-year",
    "KXFED": "Fed decision", "KXGDP": "GDP",
    "KXBTCD": "Bitcoin daily", "KXETHD": "Ethereum daily",
    "KXNASDAQ100": "Nasdaq 100",
}


def decode(ticker):
    """KXMLBF5-26JUL091840SEAMIA-MIA -> ('MLB 1st 5 innings', 'SEAMIA', 'MIA')"""
    parts = ticker.split("-")
    series = SERIES_NAMES.get(parts[0].upper(), parts[0])
    event = parts[1] if len(parts) > 1 else ""
    outcome = parts[-1] if len(parts) > 2 else ""
    # strip the leading date/time block: 26JUL091840SEAMIA -> SEAMIA
    m = re.match(r"\d{2}[A-Z]{3}\d{2}(?:\d{4})?(.*)$", event)
    if m:
        event = m.group(1)
    return series, event, outcome


async def fetch_price(client, ticker):
    try:
        m = await client.get_market(ticker)
        yb = float(m.get("yes_bid", 0) or 0)
        ya = float(m.get("yes_ask", 0) or 0)
        nb = float(m.get("no_bid", 0) or 0)
        return {"yes_bid": yb, "no_bid": nb, "title": m.get("title", ticker)}
    except Exception:
        return {"yes_bid": 0, "no_bid": 0, "title": ticker}


async def gather():
    async with KalshiClient(cfg.kalshi_api_key, cfg.kalshi_base_url,
                            cfg.kalshi_private_key_path) as c:
        cash = await c.get_balance()

        # ---- fills since cutoff -----------------------------------------
        fills = []
        cursor = None
        for _ in range(20):
            p = {"limit": 200, "min_ts": CUTOFF_TS}
            if cursor:
                p["cursor"] = cursor
            d = await c._get("/portfolio/fills", params=p)
            batch = d.get("fills", [])
            fills.extend(x for x in batch if after_cutoff(x.get("created_time")))
            cursor = d.get("cursor")
            if not cursor or not batch:
                break

        # ---- settlements since cutoff ------------------------------------
        settled = [s for s in await c.get_settlements()
                   if after_cutoff(s.get("settled_time"))]
        settled_tickers = {s.get("ticker") for s in settled}

        # ---- open positions ----------------------------------------------
        positions = [p for p in await c.get_positions()
                     if float(p.get("position_fp", 0) or 0) != 0]

        prices = {}
        tickers = list({p["ticker"] for p in positions} | settled_tickers)
        for i in range(0, len(tickers), 10):
            chunk = tickers[i:i+10]
            got = await asyncio.gather(*[fetch_price(c, t) for t in chunk])
            prices.update(dict(zip(chunk, got)))

        # ---- group fills by ticker for entry times ------------------------
        first_fill = {}
        fill_rows = defaultdict(list)
        for f in fills:
            t = f.get("ticker", "")
            fill_rows[t].append(f)
            ct = f.get("created_time", "")
            if t not in first_fill or ct < first_fill[t]:
                first_fill[t] = ct

        # ---- open positions -----------------------------------------------
        open_rows, at_risk, mark = [], 0.0, 0.0
        for p in positions:
            t = p["ticker"]
            pos = float(p.get("position_fp", 0) or 0)
            qty = abs(pos)
            side = "YES" if pos > 0 else "NO"
            cost = float(p.get("market_exposure_dollars", 0) or 0)
            fees = float(p.get("fees_paid_dollars", 0) or 0)
            pr = prices.get(t, {})
            bid = pr.get("yes_bid", 0) if side == "YES" else pr.get("no_bid", 0)
            value = round(bid * qty, 2)
            unreal = round(value - cost, 2)
            at_risk += cost
            mark += value
            series, event, outcome = decode(t)
            open_rows.append({
                "ticker": t, "title": pr.get("title", t),
                "series": series, "event": event, "outcome": outcome,
                "side": side,
                "qty": int(qty), "entry_time": first_fill.get(t, ""),
                "avg_price": round(cost / qty, 3) if qty else 0,
                "cost": round(cost, 2), "fees": round(fees, 2),
                "now": round(bid, 3), "value": value,
                "unrealized": unreal,
                "unrealized_pct": round(unreal / cost * 100, 1) if cost else 0,
            })

        # ---- closed (settled) -----------------------------------------------
        closed_rows, realized, wins, losses = [], 0.0, 0, 0
        for s in settled:
            t = s.get("ticker", "")
            yc = float(s.get("yes_count_fp", 0) or 0)
            nc = float(s.get("no_count_fp", 0) or 0)
            ycost = float(s.get("yes_total_cost_dollars", 0) or 0)
            ncost = float(s.get("no_total_cost_dollars", 0) or 0)
            fees = float(s.get("fee_cost", 0) or 0)
            res = s.get("market_result", "")

            side = "YES" if yc > nc else "NO"
            qty = yc if side == "YES" else nc
            cost = ycost + ncost
            won = (res == "yes" and side == "YES") or (res == "no" and side == "NO")
            proceeds = qty if won else 0.0
            pnl = round(proceeds - cost - fees, 2)

            realized += pnl
            if pnl > 0: wins += 1
            else: losses += 1
            series, event, outcome = decode(t)
            closed_rows.append({
                "ticker": t, "title": prices.get(t, {}).get("title", t),
                "series": series, "event": event, "outcome": outcome,
                "side": side, "qty": int(qty),
                "entry_time": first_fill.get(t, ""),
                "settled_time": s.get("settled_time", ""),
                "cost": round(cost, 2), "fees": round(fees, 2),
                "result": res, "won": won, "pnl": pnl,
            })

        open_rows.sort(key=lambda r: r["entry_time"], reverse=True)
        closed_rows.sort(key=lambda r: r["settled_time"], reverse=True)

        total = wins + losses
        return {
            "cutoff": "2026-07-08",
            "cash": round(cash, 2),
            "at_risk": round(at_risk, 2),
            "mark": round(mark, 2),
            "equity": round(cash + mark, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(mark - at_risk, 2),
            "wins": wins, "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total else None,
            "n_open": len(open_rows), "n_closed": len(closed_rows),
            "n_fills": len(fills),
            "open": open_rows, "closed": closed_rows,
            "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }


@app.route("/api/data")
def api():
    return jsonify(asyncio.run(gather()))


@app.route("/")
def index():
    return send_file("dashboard.html")


if __name__ == "__main__":
    print("dashboard → http://localhost:8080   (cutoff 2026-07-08)")
    app.run(host="0.0.0.0", port=8080, debug=False)
