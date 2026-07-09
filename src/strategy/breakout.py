import pandas as pd
import numpy as np

class BreakoutStrategy:
    """
    Momentum Breakout Strategy for Bitcoin (BTC) on 1h timeframe.
    Waits for price to blast through a key level (e.g., N-period high/low) 
    with heavy volume behind it to avoid fakeouts.
    """
    def __init__(self, window=24, volume_ma_window=24, volume_multiplier=1.5):
        self.window = window
        self.volume_ma_window = volume_ma_window
        self.volume_multiplier = volume_multiplier

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < max(self.window, self.volume_ma_window):
            return df
            
        df['signal'] = 0
        
        # Calculate key levels (Highs and Lows over the window)
        # Shift by 1 to not include the current candle in the lookback
        df['rolling_high'] = df['High'].shift(1).rolling(window=self.window).max()
        df['rolling_low'] = df['Low'].shift(1).rolling(window=self.window).min()
        
        # Calculate Volume Moving Average
        df['volume_ma'] = df['Volume'].shift(1).rolling(window=self.volume_ma_window).mean()
        
        # Breakout conditions:
        # Long: Close > rolling_high AND Volume > volume_multiplier * volume_ma
        long_breakout = (df['Close'] > df['rolling_high']) & (df['Volume'] > df['volume_ma'] * self.volume_multiplier)
        
        # Short: Close < rolling_low AND Volume > volume_multiplier * volume_ma
        short_breakout = (df['Close'] < df['rolling_low']) & (df['Volume'] > df['volume_ma'] * self.volume_multiplier)
        
        df.loc[long_breakout, 'signal'] = 1
        df.loc[short_breakout, 'signal'] = -1
        
        return df
