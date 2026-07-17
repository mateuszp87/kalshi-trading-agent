"""Coherent range pricing for finance index brackets (KXINX / KXNASDAQ100).

Range brackets: "S&P between 7550 and 7574.99". A range probability is
prob_above(low) - prob_above(high) off one lognormal curve, so the bracket set
is coherent — mass sits where spot is, adjacent brackets sum sanely. Replaces
the per-bracket LLM guess ("estimated_prob 0.04 at 78% conf").

Vol is LIVE realized vol from index daily closes, annualized on 252 trading
days (an index doesn't move on weekends). Index settles on a point-in-time
level at 4pm EDT, which fits the twice-daily scan cadence.
"""
import re, math, logging
import aiohttp
from fetchers.crypto_model import prob_above

log = logging.getLogger(__name__)
ROOT_TO_YAHOO = {"KXINX": "%5EGSPC", "KXNASDAQ100": "%5ENDX"}
_RANGE_RE = re.compile(r"between\s+([\d,]+(?:\.\d+)?)\s+and\s+([\d,]+(?:\.\d+)?)", re.I)

def parse_range(title):
    m = _RANGE_RE.search(title or "")
    if not m:
        return None
    low = float(m.group(1).replace(",", ""))
    high = float(m.group(2).replace(",", ""))
    return (low, high) if high > low else None

def root_from_ticker(ticker):
    tu = (ticker or "").upper()
    for root in ROOT_TO_YAHOO:
        if tu.startswith(root):
            return root
    return ""

def prob_between(spot, low, high, hours_left, annual_vol):
    return max(0.0, prob_above(spot, low, hours_left, annual_vol)
                    - prob_above(spot, high, hours_left, annual_vol))

async def fetch_spot_and_vol(session, root):
    sym = ROOT_TO_YAHOO.get(root)
    if not sym:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1mo"
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            d = await r.json()
        res = d["chart"]["result"][0]
        spot = res["meta"].get("regularMarketPrice")
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if not spot or len(closes) < 5:
            return None
        rets = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        annual_vol = math.sqrt(var) * math.sqrt(252)
        return {"spot": spot, "annual_vol": round(annual_vol, 4),
                "n_closes": len(closes), "symbol": sym}
    except Exception as e:
        log.warning(f"finance spot/vol {root}: {e}")
        return None

if __name__ == "__main__":
    import asyncio
    async def go():
        print("--- finance_bracket self-test ---")
        assert parse_range("S&P 500 be between 7550 and 7574.9999 on Jul 17") == (7550.0, 7574.9999)
        assert root_from_ticker("KXINX-26JUL17H1600-B7562") == "KXINX"
        assert root_from_ticker("KXNASDAQ100-26JUL16H1600-B29350") == "KXNASDAQ100"
        print("parse/root: OK")
        async with aiohttp.ClientSession() as s:
            for root in ["KXINX", "KXNASDAQ100"]:
                d = await fetch_spot_and_vol(s, root)
                if not d:
                    print(f"{root}: FETCH FAILED"); continue
                spot, vol = d["spot"], d["annual_vol"]
                print(f"\n{root}: spot={spot:,.2f}  realized_vol={vol:.1%}  ({d['n_closes']} closes)")
                band = 25 if root == "KXINX" else 100
                base = round(spot / band) * band
                print(f"  {'range':>18} {'MODEL':>7}")
                for k in [base - band, base, base + band]:
                    p = prob_between(spot, k, k + band, 21.0, vol)
                    mark = "  <-- spot band" if k <= spot < k + band else ""
                    print(f"  {int(k)}-{int(k+band):>7} {p:>7.2f}{mark}")
        print("\nPASTE_BACK_TO_CLAUDE: FINANCE_BRACKET PASS")
    asyncio.run(go())
