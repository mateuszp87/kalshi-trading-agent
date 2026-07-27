content = open('agent.py').read()

# Replace the scan cycle to use direct series-based market fetching
old = """            keyword = cat_cfg["keywords"][self.stats.markets_scanned % len(cat_cfg["keywords"])]
            markets = await client.get_markets(keyword=keyword, limit=25)"""

new = """            # Rotate through keywords
            keyword = cat_cfg["keywords"][self.stats.markets_scanned % len(cat_cfg["keywords"])]
            # Try events endpoint first for clean markets, fall back to search
            try:
                all_markets = await client.get_events(limit=50)
                # Filter by keyword relevance
                kw_lower = keyword.lower()
                markets = [m for m in all_markets if any(
                    w in m.title.lower() for w in kw_lower.split()
                )] or all_markets[:25]
            except Exception:
                markets = await client.get_markets(keyword=keyword, limit=25)"""

content = content.replace(old, new)
open('agent.py', 'w').write(content)
print('Done')
