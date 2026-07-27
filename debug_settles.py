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
        async with s.get(BASE + '/portfolio/settlements?limit=5', headers=hdrs('GET', '/portfolio/settlements')) as r:
            d = await r.json()
        print("=== RAW SETTLEMENT OBJECTS ===")
        for st in d.get('settlements', [])[:3]:
            print(json.dumps(st, indent=2))
            print("---")

asyncio.run(go())
