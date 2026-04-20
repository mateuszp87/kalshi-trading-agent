content = open('dashboard.py').read()

# Replace the settlement processing block to enrich with market titles
old = '''        settles = []
        for s in settles_raw:
            # Kalshi returns: settled_time, ticker, revenue (cents),
            # yes_total_cost_dollars, no_total_cost_dollars, market_result,
            # yes_count_fp, no_count_fp
            settled = s.get("settled_time", "")
            ticker = s.get("ticker", "")
            if not settled or not ticker: continue
            if settled < CUTOFF: continue

            yes_cost = float(s.get("yes_total_cost_dollars", 0) or 0)
            no_cost = float(s.get("no_total_cost_dollars", 0) or 0)
            yes_ct = float(s.get("yes_count_fp", 0) or 0)
            no_ct = float(s.get("no_count_fp", 0) or 0)

            # Determine which side you were on (whichever has count > 0)
            if yes_ct > 0:
                side = "YES"
                cost = round(yes_cost, 2)
                count = int(yes_ct)
            elif no_ct > 0:
                side = "NO"
                cost = round(no_cost, 2)
                count = int(no_ct)
            else:
                continue

            if cost <= 0: continue

            # Revenue is in cents
            rev = round(float(s.get("revenue", 0)) / 100, 2)
            pnl = round(rev - cost, 2)

            settles.append({
                "ticker": ticker,
                "side": side,
                "count": count,
                "cost": cost,
                "revenue": rev,
                "pnl": pnl,
                "time": settled[:19].replace("T", " "),
            })'''

new = '''        settles = []
        # Cache market titles so we don't re-fetch for same ticker
        title_cache = {}
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
            })'''

content = content.replace(old, new)

# Also enrich orders with titles
old_orders = '''            orders.append({
                "ticker": ticker, "side": side,
                "action": action.upper() if action else "BUY",
                "count": filled, "price": fill_price,
                "total": round(fill_price * filled, 2),
                "time": created[:19].replace("T", " "),
            })'''

new_orders = '''            # Fetch market title
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
            })'''

content = content.replace(old_orders, new_orders)

# Move title_cache up so it's available for orders too (shared between orders + settles)
# Find where orders block starts and inject the cache declaration before it
content = content.replace(
    '        orders = []\n        for o in raw_orders:',
    '        orders = []\n        title_cache = {}  # shared cache for market titles\n        for o in raw_orders:'
)

# Remove duplicate declaration in settles block now that cache is shared
content = content.replace(
    '''        settles = []
        # Cache market titles so we don't re-fetch for same ticker
        title_cache = {}
        for s in settles_raw:''',
    '''        settles = []
        for s in settles_raw:'''
)

open('dashboard.py', 'w').write(content)
print("✓ Dashboard now enriches orders & settlements with market titles")
