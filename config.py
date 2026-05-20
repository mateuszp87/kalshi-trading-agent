"""
Configuration — loads from environment variables or .env file
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AgentConfig:
    category: str = "all"
    dry_run: bool = True
    scan_interval_min: int = 7200
    scan_interval_max: int = 10800
    max_bet_size: float = 40.0
    max_open_positions: int = 50   # lower limit — game trades cycle fast
    buy_threshold: float = 0.72
    sell_threshold: float = 0.28
    max_daily_loss: float = 100.0

    kalshi_api_key: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY", ""))
    kalshi_base_url: str = field(default_factory=lambda: os.getenv("KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2"))
    kalshi_private_key_path: str = field(default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY_PATH", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

    espn_api_key: str = field(default_factory=lambda: os.getenv("ESPN_API_KEY", ""))
    newsapi_key: str = field(default_factory=lambda: os.getenv("NEWSAPI_KEY", ""))
    fred_api_key: str = field(default_factory=lambda: os.getenv("FRED_API_KEY", ""))
    coingecko_api_key: str = field(default_factory=lambda: os.getenv("COINGECKO_API_KEY", ""))
    noaa_token: str = field(default_factory=lambda: os.getenv("NOAA_TOKEN", ""))

    claude_model: str = "claude-sonnet-4-6"  # Migrated from claude-sonnet-4-20250514 (deprecated June 15, 2026)

    def validate(self):
        errors = []
        if not self.kalshi_api_key:
            errors.append("KALSHI_API_KEY not set")
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY not set")
        if errors:
            raise EnvironmentError("Missing required config:\n  " + "\n  ".join(errors))

    @property
    def active_categories(self) -> list[str]:
        if self.category == "all":
            return ["sports", "politics", "econ", "entertainment", "crypto", "weather"]
        return [self.category]
