import json
import logging

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("KalshiAgent")

class MarketDispatcher:
    @staticmethod
    def get_signals(market_id):
        if "PGATOUR" in market_id:
            return ['pga_form_stats', 'course_history_aronimink', 'datagolf_rankings']
        elif "NBA" in market_id:
            return ['nba_injury_report', 'team_momentum_stats', 'rest_days']
        elif "HIGHNY" in market_id:
            return ['nws_forecast', 'historical_base_rate', 'current_temp_sensor']
        return ["general_macro_news"]

class RobustTradeLogic:
    def parse_claude_response(self, response_text):
        if not response_text or response_text.strip() == "":
            return None
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            return None

    def evaluate_edge(self, market_price, model_prob, confidence):
        if None in [market_price, model_prob, confidence]:
            return "skip", 0
        
        buy_edge = model_prob - market_price
        sell_edge = (1 - model_prob) - (1 - market_price)

        if buy_edge > 0.05 and confidence > 0.80:
            return "buy", buy_edge
        elif sell_edge > 0.05 and confidence > 0.80:
            return "sell", sell_edge
        return "skip", 0

class KalshiAgent:
    def __init__(self):
        self.active_positions = set()
        self.logic = RobustTradeLogic()

    def scan_market(self, market):
        m_id = market['id']
        m_price = market['mid']
        
        if m_id in self.active_positions:
            logger.info(f"→ SKIP: Already holding position in {m_id}")
            return

        signals = MarketDispatcher.get_signals(m_id)
        logger.info(f"Scanning {m_id} with signals: {signals}")

        raw_llm_output = '{"prob": 0.05, "conf": 0.95}' 
        analysis = self.logic.parse_claude_response(raw_llm_output)

        if not analysis: return

        action, edge = self.logic.evaluate_edge(m_price, analysis['prob'], analysis['conf'])

        if action != "skip":
            self.execute_trade(m_id, action, edge)

    def execute_trade(self, m_id, action, edge):
        logger.info(f"🚀 EXECUTING {action.upper()} on {m_id} | Edge: {edge:.2f}")
        self.active_positions.add(m_id)

if __name__ == "__main__":
    agent = KalshiAgent()
    test_market = {'id': 'KXHIGHNY-26APR18-B65.5', 'mid': 0.84}
    agent.scan_market(test_market)
