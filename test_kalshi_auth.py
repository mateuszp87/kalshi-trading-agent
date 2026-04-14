"""
Direct Kalshi auth test — matches their exact sample code from docs.kalshi.com
Run: python3 test_kalshi_auth.py
"""

import base64
import datetime
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from dotenv import load_dotenv
import os

load_dotenv()

KEY_ID = os.getenv("KALSHI_API_KEY", "")
KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi-private.key")
BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

print(f"Key ID   : {KEY_ID}")
print(f"Key path : {KEY_PATH}")
print(f"Key exists: {os.path.exists(KEY_PATH)}")
print()

# Load private key exactly as Kalshi docs show
with open(KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(
        f.read(),
        password=None,
        backend=default_backend()
    )
print("Private key loaded OK")

# Sign exactly as Kalshi docs show
def sign_pss_text(private_key, text: str) -> str:
    message = text.encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

# Build request exactly as Kalshi docs show
ts = str(int(datetime.datetime.now().timestamp() * 1000))
method = "GET"
path = "/trade-api/v2/portfolio/balance"
path_no_query = path.split('?')[0]
msg_string = ts + method + path_no_query

print(f"Message to sign: {msg_string[:60]}...")
sig = sign_pss_text(private_key, msg_string)
print(f"Signature (first 40 chars): {sig[:40]}...")
print()

headers = {
    'KALSHI-ACCESS-KEY': KEY_ID,
    'KALSHI-ACCESS-SIGNATURE': sig,
    'KALSHI-ACCESS-TIMESTAMP': ts
}

print("Sending request to Kalshi...")
response = requests.get(BASE_URL + path, headers=headers)
print(f"Status: {response.status_code}")
print(f"Response: {response.text[:300]}")
