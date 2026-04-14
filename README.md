# Kalshi Multi-Category Trading Agent

AI-powered prediction market trading across 6 categories, using real external data sources and Claude as the reasoning engine.

---

## Architecture

```
main.py                  ← CLI entry point
├── config.py            ← Loads env vars, validates keys
├── agent.py             ← Main orchestrator (scan loop, risk limits, trade execution)
├── kalshi_client.py     ← Kalshi REST API client (markets, orders, balance, positions)
├── reasoner.py          ← Claude reasoning engine (scores markets → TradeSignal)
└── fetchers/
    ├── sports.py        ← ESPN, The Odds API, Polymarket
    ├── politics.py      ← NewsAPI, Polymarket, Metaculus, 538
    ├── econ.py          ← FRED, CME FedWatch, Atlanta Fed GDPNow
    └── other.py         ← Entertainment (OMDB, TMDB), Crypto (CoinGecko, Fear&Greed), Weather (NWS, Open-Meteo)
```

**How a trade cycle works:**
1. Agent fetches open Kalshi markets matching category keywords
2. For each market, external APIs collect live signals (odds, polls, prices, forecasts)
3. Claude receives the market title + all signals → returns `estimated_prob`, `action`, `edge`, `reasoning`
4. If edge > threshold and Claude says `buy_yes`/`buy_no`, a limit order is placed via Kalshi API
5. Risk checks enforce max bet size and daily loss cap

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API keys
```bash
cp .env.example .env
# Edit .env and fill in your keys
```

**Required:**
- `KALSHI_API_KEY` — from [kalshi.com](https://kalshi.com) → Account → API
- `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com)

**Recommended (one per category you want to trade):**

| Category | API | Cost | Link |
|---|---|---|---|
| Sports | The Odds API | Free (500 req/mo) | [the-odds-api.com](https://the-odds-api.com) |
| Politics / Entertainment | NewsAPI | Free (100 req/day) | [newsapi.org](https://newsapi.org) |
| Economics | FRED | Free | [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) |
| Crypto | CoinGecko | Free tier | [coingecko.com/api](https://www.coingecko.com/api) |
| Weather | NOAA CDO | Free | [ncdc.noaa.gov/cdo-web/token](https://www.ncdc.noaa.gov/cdo-web/token) |

> **Note:** The agent degrades gracefully — if an API key is missing, Claude still reasons from the market title + whatever signals are available (Polymarket and Open-Meteo require no keys).

---

## Usage

### Paper trading (dry run, no real orders)
```bash
python main.py --dry-run --category all --interval 300
```

### Live trading — single category
```bash
python main.py --category sports --interval 300 --max-bet 20
```

### Live trading — all categories
```bash
python main.py --category all --interval 600 --max-bet 10 --max-daily-loss 50
```

### CLI flags
```
--category        sports | politics | econ | entertainment | crypto | weather | all
--dry-run         Simulate trades without placing real orders
--interval        Seconds between scan cycles (default: 300)
--max-bet         Max dollars per trade (default: $20)
--buy-threshold   Signal threshold to buy YES (default: 0.72)
--sell-threshold  Signal threshold to buy NO (default: 0.28)
--max-daily-loss  Stop trading if cumulative loss hits this (default: $100)
```

---

## Category Signal Sources

### Sports
| Signal | Source | Key Required |
|---|---|---|
| Vegas implied probability | The Odds API | Yes |
| Injury report (star player out) | ESPN public API | No |
| Team recent form (L10 record) | ESPN scoreboard | No |
| Polymarket cross-reference | Polymarket gamma API | No |

### Politics
| Signal | Source | Key Required |
|---|---|---|
| News sentiment score | NewsAPI | Yes |
| Crowd probability | Polymarket gamma API | No |
| Community prediction | Metaculus API | No |
| Polling average | FiveThirtyEight public JSON | No |

### Economics
| Signal | Source | Key Required |
|---|---|---|
| Fed rate cut probability | CME FedWatch | No |
| CPI trend (YoY) | FRED (CPIAUCSL) | Yes |
| Unemployment rate | FRED (UNRATE) | Yes |
| GDP growth | FRED (GDPC1) | Yes |
| GDPNow nowcast | Atlanta Fed public CSV | No |
| 10Y Treasury yield | FRED (DGS10) | Yes |

### Entertainment
| Signal | Source | Key Required |
|---|---|---|
| Metacritic score | OMDB API (free key) | No |
| Box office / popularity | TMDB trending API | No |
| Media buzz intensity | NewsAPI | Yes |
| Crowd probability | Polymarket gamma API | No |

### Crypto
| Signal | Source | Key Required |
|---|---|---|
| Price + 7d momentum | CoinGecko | Optional |
| Market cap rank | CoinGecko | Optional |
| Fear & Greed index | alternative.me | No |
| Exchange volume ratio | CoinGecko | Optional |
| Polymarket probability | Polymarket gamma API | No |

### Weather
| Signal | Source | Key Required |
|---|---|---|
| NWS official forecast | api.weather.gov | No |
| Historical base rate | Built-in climatology table | No |
| Current conditions | Open-Meteo | No |
| ECMWF ensemble forecast | Open-Meteo ensemble API | No |

---

## Risk Management

Built-in safeguards:
- **Max bet size** — per-trade dollar limit (CLI: `--max-bet`)
- **Daily loss cap** — agent stops if cumulative loss exceeds limit (CLI: `--max-daily-loss`)
- **Minimum edge** — Claude only recommends trades when |estimated_prob - market_price| > 5%
- **Confidence gate** — Claude returns a confidence score; low-confidence signals → skip
- **Dry run mode** — always test with `--dry-run` before going live

**Start with paper trading (`--dry-run`) for at least 1 week before trading real money.**

---

## Deploying as a Cron / Service

### cron (simple)
```bash
# Run every 5 minutes
*/5 * * * * /path/to/venv/bin/python /path/to/kalshi_agent/main.py --category all --max-bet 10 >> /var/log/kalshi_agent.log 2>&1
```

### systemd service (persistent)
```ini
[Unit]
Description=Kalshi Trading Agent
After=network.target

[Service]
WorkingDirectory=/path/to/kalshi_agent
ExecStart=/path/to/venv/bin/python main.py --category all --interval 300 --max-bet 10
Restart=on-failure
RestartSec=60
EnvironmentFile=/path/to/kalshi_agent/.env

[Install]
WantedBy=multi-user.target
```

### AWS Lambda (event-driven)
Wrap `agent._scan_cycle()` in a Lambda handler. Set EventBridge rule to trigger every N minutes.

---

## Extending with New Factors

To add a new signal to any category, edit the relevant fetcher in `fetchers/` and return it in the signals dict:

```python
signals["my_new_signal"] = {
    "value": 0.72,          # 0.0 – 1.0 (higher = more bullish for YES)
    "description": "What this signal means in plain English",
    "raw": {...}            # raw API response for Claude's reference
}
```

Claude will automatically incorporate it in its reasoning. No prompt changes needed.
