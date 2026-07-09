import pandas as pd
import numpy as np
import ta

class RiskManager:
    """
    Risk Management Module.
    Calculates Stop Loss using ATR (Average True Range) and determines position size
    to ensure a strict 1% risk of total account equity per trade.
    """
    def __init__(self, account_size=10000, risk_per_trade=0.01, atr_window=14, atr_multiplier=2.0):
        self.initial_account_size = account_size
        self.risk_per_trade = risk_per_trade
        self.atr_window = atr_window
        self.atr_multiplier = atr_multiplier

    def add_risk_metrics(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < self.atr_window:
            return df
            
        # Calculate ATR for volatility
        df['atr'] = ta.volatility.average_true_range(df['High'], df['Low'], df['Close'], window=self.atr_window)
        
        # Calculate Stop Loss distance based on ATR
        df['sl_distance'] = df['atr'] * self.atr_multiplier
        
        return df

    def calculate_position_size(self, current_capital: float, entry_price: float, sl_distance: float) -> float:
        """
        Calculate the position size (number of shares/contracts) such that if the stop loss is hit,
        the loss equals exactly risk_per_trade * current_capital.
        """
        if pd.isna(sl_distance) or sl_distance <= 0:
            return 0.0
            
        risk_amount = current_capital * self.risk_per_trade
        
        # Risk amount = Position Size * SL Distance
        # Therefore, Position Size = Risk Amount / SL Distance
        position_size = risk_amount / sl_distance
        
        # Ensure we don't buy more than we can afford (no margin used here)
        max_size = current_capital / entry_price
        
        return min(position_size, max_size)
