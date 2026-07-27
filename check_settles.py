import os, base64, datetime, asyncio, json
from urllib.parse import urlparse
import aiohttp
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

load_dotenv()

KEY_ID = os.getenv('KALSHI_API_KEY', '')
BASE = 'https://api.elections.kalshi.com/trade-api/v2'

with open('./kalshi-private.key', 'rb') as f:
    key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def hdrs(method, path):
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    sp = urlparse(BASE + path).path.split('?')[0]
    sig = key.sign(f'{ts}{method}{sp}'.encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    return {'KALSHI-ACCESS-KEY': KEY_ID,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
            'KALSHI-ACCESS-TIMESTAMP': ts}

async def go():
    async with aiohttp.ClientSession() as s:
        async with s.get(BASE + '/portfolio/settlements?limit=30', headers=hdrs('GET', '/portfolio/settlements')) as r:
            d = await r.json()
        settles = d.get('settlements', [])
        print(f"Total settlements returned: {len(settles)}")
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        print(f"Current UTC: {now_utc.isoformat()}")
        cutoff = (now_utc - datetime.timedelta(days=7)).isoformat().replace("+00:00", "Z")
        print(f"Dashboard cutoff: {cutoff}")
        print()
        print(f"{'Created':<22} {'Ticker':<38} {'Cost':>7} {'Rev':>7} {'P&L':>7}  In?")
        print("-" * 95)
        settles.sort(key=lambda x: x.get('created_time', ''), reverse=True)
        for st in settles[:20]:
            created = st.get('created_time', 'N/A')
            ticker = st.get('market_ticker', 'N/A')[:38]
            cost = float(st.get('cost', 0)) / 100
            rev = float(st.get('revenue', 0)) / 100
            pnl = rev - cost
            has_cost = cost > 0
            in_window = created >= cutoff
            status = "✓" if (has_cost and in_window) else ("✗ old" if not in_window else "✗ $0 cost")
            print(f"{created[:19]:<22} {ticker:<38} ${cost:>5.2f} ${rev:>5.2f} ${pnl:>+5.2f}  {status}")

asyncio.run(go())
