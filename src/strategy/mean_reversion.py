import pandas as pd
import numpy as np
import ta

class MeanReversionStrategy:
    """
    Mean Reversion Strategy for S&P 500 (SPY) and NASDAQ (QQQ) on 15m timeframe.
    Uses Bollinger Bands to detect when price goes too far in one direction,
    and catches the 'snap back'.
    """
    def __init__(self, window=20, dev=2.0):
        self.window = window
        self.dev = dev

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < self.window:
            return df
            
        # Calculate Bollinger Bands
        indicator_bb = ta.volatility.BollingerBands(close=df['Close'], window=self.window, window_dev=self.dev)
        df['bb_bbm'] = indicator_bb.bollinger_mavg()
        df['bb_bbh'] = indicator_bb.bollinger_hband()
        df['bb_bbl'] = indicator_bb.bollinger_lband()
        
        # Initialize signals
        df['signal'] = 0
        
        # Mean reversion logic:
        # Buy when price crosses below lower band, assuming it will snap back to mean
        # Sell when price crosses above upper band, assuming it will snap back to mean
        
        # Long signal: Close was below lower band yesterday, but today's close is above it (reversal)
        long_condition = (df['Close'].shift(1) < df['bb_bbl'].shift(1)) & (df['Close'] > df['bb_bbl'])
        
        # Short signal: Close was above upper band yesterday, but today's close is below it
        short_condition = (df['Close'].shift(1) > df['bb_bbh'].shift(1)) & (df['Close'] < df['bb_bbh'])
        
        df.loc[long_condition, 'signal'] = 1
        df.loc[short_condition, 'signal'] = -1
        
        return df
