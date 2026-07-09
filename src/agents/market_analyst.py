import yfinance as yf
import pandas as pd

class MarketAnalystAgent:
    """
    Agent responsible for fetching and preparing market data.
    """
    def fetch_and_analyze(self, inputs: dict, state: dict):
        assets = inputs.get("assets", [])
        timeframes = inputs.get("timeframes", ["1h"])
        
        # We will map our conceptual timeframes to yfinance intervals
        tf_map = {
            "15m": "15m",
            "1h": "60m",
            "4h": "1h" # yfinance doesn't easily support 4h directly without resampling, but we will mock or use 60m for now
        }
        
        print(f"[MarketAnalystAgent] Fetching data for {len(assets)} assets...")
        data = {}
        for asset in assets:
            data[asset] = {}
            for tf in timeframes:
                interval = tf_map.get(tf, "1d")
                try:
                    df = yf.download(asset, period="1mo", interval=interval, progress=False)
                    if not df.empty:
                        data[asset][tf] = df
                except Exception as e:
                    print(f"Error fetching {asset} at {tf}: {e}")
                    
        return {"market_data": data}
