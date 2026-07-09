import pandas as pd
import ta

class TrendFollowingStrategy:
    """
    Trend Following Strategy for Commodities (Gold, Oil) on 4h timeframe.
    Uses slower moving averages to avoid entry noise and catch cleaner waves.
    """
    def __init__(self, slow_ma=200, fast_ma=50):
        self.slow_ma = slow_ma
        self.fast_ma = fast_ma

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < self.slow_ma:
            return df
            
        df['signal'] = 0
        
        # Calculate moving averages
        df['fast_ma'] = ta.trend.sma_indicator(df['Close'], window=self.fast_ma)
        df['slow_ma'] = ta.trend.sma_indicator(df['Close'], window=self.slow_ma)
        
        # Trend conditions:
        # Long: Fast MA crosses above Slow MA (Golden Cross)
        long_condition = (df['fast_ma'].shift(1) <= df['slow_ma'].shift(1)) & (df['fast_ma'] > df['slow_ma'])
        
        # Short: Fast MA crosses below Slow MA (Death Cross)
        short_condition = (df['fast_ma'].shift(1) >= df['slow_ma'].shift(1)) & (df['fast_ma'] < df['slow_ma'])
        
        df.loc[long_condition, 'signal'] = 1
        df.loc[short_condition, 'signal'] = -1
        
        return df
