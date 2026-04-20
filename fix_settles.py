# Patches dashboard.py to use the REAL Kalshi settlement field names
with open('dashboard.py', 'r') as f:
    content = f.read()

old_block = '''        settles = []
        for s in settles_raw:
            created = s.get("created_time", "")
            cost = round(float(s.get("cost", 0)) / 100, 2)
            rev = round(float(s.get("revenue", 0)) / 100, 2)
            ticker = s.get("market_ticker", "")
            if cost <= 0: continue
            if not created or created < CUTOFF: continue
            if not ticker: continue
            settles.append({
                "ticker": ticker,
                "side": s.get("side", ""),
                "count": int(s.get("count", 0) or 0),
                "cost": cost, "revenue": rev,
                "pnl": round(rev - cost, 2),
                "time": created[:19].replace("T", " "),
            })'''

new_block = '''        settles = []
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

if old_block not in content:
    print("ERROR: old block not found. Current settlement code:")
    import re
    m = re.search(r'settles = \[\].*?settles\.sort', content, re.DOTALL)
    if m: print(m.group(0)[:1500])
else:
    content = content.replace(old_block, new_block)
    with open('dashboard.py', 'w') as f:
        f.write(content)
    print("✓ Fixed settlement parsing")
