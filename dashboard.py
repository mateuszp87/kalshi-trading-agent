import os, json, time
from flask import Flask, render_template_string, jsonify

app = Flask(__name__)
STATS_FILE = 'stats.json'

def load_data():
    if not os.path.exists(STATS_FILE):
        return {}
    with open(STATS_FILE, 'r') as f:
        try:
            return json.load(f)
        except:
            return {}

@app.route('/')
def index():
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Kalshi Alpha | Pro Dashboard</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            body { background: #0f172a; color: #e2e8f0; font-family: 'Inter', sans-serif; }
            .stat-card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.5rem; }
            .market-row:hover { background: #1e293b; transition: 0.2s; }
        </style>
    </head>
    <body class="p-6">
        <div class="max-w-7xl mx-auto">
            <div class="flex justify-between items-end mb-8">
                <div>
                    <h1 class="text-3xl font-bold text-white">Kalshi Terminal <span class="text-blue-500">v3.1</span></h1>
                    <p class="text-slate-400 text-sm">24/7 Autonomic Trading Active</p>
                </div>
                <div class="text-right">
                    <p id="clock" class="text-xl font-mono text-blue-400 font-bold"></p>
                    <p class="text-xs text-slate-500 uppercase tracking-widest">Eugene, OR | UTC-7</p>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
                <div class="stat-card">
                    <p class="text-slate-400 text-xs uppercase font-bold tracking-wider">Total Liquidity</p>
                    <h2 id="balance" class="text-3xl font-bold text-white mt-1">$0.00</h2>
                </div>
                <div class="stat-card">
                    <p class="text-slate-400 text-xs uppercase font-bold tracking-wider">Realized P&L (All-Time)</p>
                    <h2 id="pnl" class="text-3xl font-bold text-green-400 mt-1">$0.00</h2>
                </div>
                <div class="stat-card">
                    <p class="text-slate-400 text-xs uppercase font-bold tracking-wider">Active Exposure</p>
                    <h2 id="unrealized" class="text-3xl font-bold text-blue-400 mt-1">$0.00</h2>
                </div>
                <div class="stat-card">
                    <p class="text-slate-400 text-xs uppercase font-bold tracking-wider">Success Rate</p>
                    <h2 id="winrate" class="text-3xl font-bold text-purple-400 mt-1">0%</h2>
                </div>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
                <div class="lg:col-span-2 bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
                    <div class="p-4 border-b border-slate-800 bg-slate-800/50 flex justify-between">
                        <h3 class="font-bold uppercase text-sm tracking-widest">Active Market Positions</h3>
                        <span id="pos-count" class="bg-blue-600 text-white text-xs px-2 py-1 rounded">0 ACTIVE</span>
                    </div>
                    <table class="w-full">
                        <thead class="text-slate-500 text-xs uppercase bg-slate-900">
                            <tr>
                                <th class="p-4 text-left">Market</th>
                                <th class="p-4 text-center">Qty</th>
                                <th class="p-4 text-center">Avg Entry</th>
                                <th class="p-4 text-right">Return</th>
                            </tr>
                        </thead>
                        <tbody id="pos-table" class="divide-y divide-slate-800"></tbody>
                    </table>
                </div>

                <div class="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
                    <div class="p-4 border-b border-slate-800 bg-slate-800/50">
                        <h3 class="font-bold uppercase text-sm tracking-widest">Recent Activity</h3>
                    </div>
                    <div id="activity-feed" class="p-4 space-y-4 max-h-[500px] overflow-y-auto text-sm">
                        </div>
                </div>
            </div>
        </div>

        <script>
            function fmt(val) { return parseFloat(val || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}); }

            async function update() {
                const res = await fetch('/api/data');
                const d = await res.json();
                
                document.getElementById('balance').innerText = '$' + fmt(d.balance);
                document.getElementById('pnl').innerText = '$' + fmt(d.realized_pnl_dollars);
                document.getElementById('unrealized').innerText = '$' + fmt(d.unrealized_pnl);
                
                const positions = d.positions || [];
                document.getElementById('pos-count').innerText = positions.length + ' ACTIVE';
                
                const table = document.getElementById('pos-table');
                table.innerHTML = positions.map(p => `
                    <tr class="market-row">
                        <td class="p-4 font-mono text-sm text-blue-300">${p.ticker}</td>
                        <td class="p-4 text-center font-bold">${parseInt(parseFloat(p.count_fp || p.count || 0))}</td>
                        <td class="p-4 text-center">$${fmt(p.avg_price_dollars || p.avg_price)}</td>
                        <td class="p-4 text-right font-bold ${p.pnl >= 0 ? 'text-green-400' : 'text-red-400'}">$${fmt(p.pnl)}</td>
                    </tr>
                `).join('');

                const winrate = d.win_rate || 0;
                document.getElementById('winrate').innerText = winrate + '%';

                document.getElementById('clock').innerText = new Date().toLocaleTimeString();
            }
            setInterval(update, 5000); update();
        </script>
    </body>
    </html>
    ''')

@app.route('/api/data')
def api_data():
    return jsonify(load_data())

if __name__ == '__main__':
    app.run(port=8080, host='0.0.0.0')