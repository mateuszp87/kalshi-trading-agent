import os, json, time
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
STATS_FILE = 'stats.json'

def load_stats():
    if not os.path.exists(STATS_FILE):
        return {"balance": 0, "positions": [], "realized_pnl_dollars": 0, "unrealized_pnl": 0}
    with open(STATS_FILE, 'r') as f:
        try:
            return json.load(f)
        except:
            return {"balance": 0, "positions": [], "realized_pnl_dollars": 0, "unrealized_pnl": 0}

@app.route('/')
def index():
    # Returning the original complex UI layout
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Kalshi Alpha Agent</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>body { background-color: #0f172a; color: white; }</style>
    </head>
    <body class="p-8">
        <div class="max-w-6xl mx-auto">
            <div class="flex justify-between items-center mb-8">
                <h1 class="text-3xl font-bold text-blue-400">Kalshi Terminal v2.0</h1>
                <div class="text-right">
                    <p class="text-sm text-slate-400">System Status: <span class="text-green-400">OPERATIONAL</span></p>
                    <p id="clock" class="text-xs font-mono text-slate-500"></p>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
                <div class="bg-slate-800 p-6 rounded-xl border border-slate-700">
                    <p class="text-slate-400 text-sm uppercase font-semibold">Total Balance</p>
                    <h2 id="balance" class="text-4xl font-bold mt-2">$0.00</h2>
                </div>
                <div class="bg-slate-800 p-6 rounded-xl border border-slate-700">
                    <p class="text-slate-400 text-sm uppercase font-semibold">Realized P&L</p>
                    <h2 id="pnl" class="text-4xl font-bold mt-2 text-green-400">$0.00</h2>
                </div>
                <div class="bg-slate-800 p-6 rounded-xl border border-slate-700">
                    <p class="text-slate-400 text-sm uppercase font-semibold">Open Trades</p>
                    <h2 id="pos-count" class="text-4xl font-bold mt-2 text-blue-400">0</h2>
                </div>
            </div>

            <div class="bg-slate-800 rounded-xl border border-slate-700 overflow-hidden">
                <div class="p-4 bg-slate-900/50 border-b border-slate-700">
                    <h2 class="text-xl font-bold">Active Market Positions</h2>
                </div>
                <table class="w-full text-left">
                    <thead class="bg-slate-900/30 text-slate-400 text-sm uppercase">
                        <tr>
                            <th class="p-4">Ticker</th>
                            <th class="p-4">Side</th>
                            <th class="p-4">Qty</th>
                            <th class="p-4">Avg Price</th>
                            <th class="p-4">Unrealized</th>
                        </tr>
                    </thead>
                    <tbody id="portfolio-body" class="divide-y divide-slate-700"></tbody>
                </table>
            </div>
        </div>

        <script>
            async function update() {
                const res = await fetch('/api/data');
                const stats = await res.json();
                
                document.getElementById('balance').innerText = '$' + (stats.balance || 0).toLocaleString(undefined, {minimumFractionDigits: 2});
                document.getElementById('pnl').innerText = '$' + (stats.realized_pnl_dollars || 0).toLocaleString(undefined, {minimumFractionDigits: 2});
                
                const positions = stats.positions || [];
                document.getElementById('pos-count').innerText = positions.length;
                
                const body = document.getElementById('portfolio-body');
                body.innerHTML = positions.map(p => `
                    <tr class="hover:bg-slate-700/30 transition-colors">
                        <td class="p-4 font-mono text-sm text-blue-300">${p.ticker}</td>
                        <td class="p-4"><span class="px-2 py-1 rounded text-xs font-bold ${p.side === 'yes' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}">${p.side.toUpperCase()}</span></td>
                        <td class="p-4">${parseFloat(p.count_fp || p.count || 0)}</td>
                        <td class="p-4">$${(parseFloat(p.avg_price_dollars || p.avg_price || 0) || 0).toFixed(2)}</td>
                        <td class="p-4 font-bold ${parseFloat(p.realized_pnl_dollars || p.pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}">$${(parseFloat(p.realized_pnl_dollars || p.pnl || 0) || 0).toFixed(2)}</td>
                    </tr>
                `).join('');
                document.getElementById('clock').innerText = new Date().toLocaleTimeString();
            }
            setInterval(update, 5000); update();
        </script>
    </body>
    </html>
    '''

@app.route('/api/data')
@app.route('/api/data') # Mapping both for legacy support
def data():
    return jsonify(load_stats())

if __name__ == '__main__':
    app.run(port=8080, host='0.0.0.0', debug=False)