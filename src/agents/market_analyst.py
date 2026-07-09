import yfinance as yf
import pandas as pd

class MarketAnalystAgent:
    """
    Agent responsible for fetching and preparing market data (DataNode).
    """
    def __init__(self, event_bus=None):
        self.event_bus = event_bus

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
                        # Flatten column MultiIndex if exists (yfinance returns MultiIndex in newer versions)
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                        
                        # --- CÁLCULO DE INDICADORES TÉCNICOS ---
                        close = df['Close']
                        
                        # EMAs (Tendencia)
                        df['EMA_20'] = close.ewm(span=20, adjust=False).mean()
                        df['EMA_50'] = close.ewm(span=50, adjust=False).mean()
                        
                        # RSI (Momento)
                        delta = close.diff()
                        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                        rs = gain / loss
                        df['RSI'] = 100 - (100 / (1 + rs))
                        
                        # MACD (Momento/Tendencia)
                        ema_12 = close.ewm(span=12, adjust=False).mean()
                        ema_26 = close.ewm(span=26, adjust=False).mean()
                        df['MACD'] = ema_12 - ema_26
                        df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
                        
                        data[asset][tf] = df
                except Exception as e:
                    print(f"Error fetching {asset} at {tf}: {e}")
        
        # Publicar evento en el EventBus (Estilo Nautilus DataNode)
        if self.event_bus:
            self.event_bus.publish("MARKET_DATA_READY", data)
            
        return {"market_data": data}
