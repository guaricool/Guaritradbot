import pandas as pd
import numpy as np

class BacktestEngine:
    """
    Vectorized and Iterative Backtester depending on the need.
    Uses the RiskManager for position sizing and stop loss.
    """
    def __init__(self, df: pd.DataFrame, risk_manager):
        self.df = df.copy()
        self.risk_manager = risk_manager
        self.initial_capital = self.risk_manager.initial_account_size
        
    def run(self) -> dict:
        """
        Runs an iterative backtest to properly calculate compounding capital,
        dynamic position sizes, and ATR-based stop losses.
        """
        if 'signal' not in self.df.columns or 'atr' not in self.df.columns:
            return {"error": "DataFrame must contain 'signal' and 'atr' columns"}
            
        capital = self.initial_capital
        position = 0 # 1 for Long, -1 for Short, 0 for None
        entry_price = 0
        position_size = 0
        stop_loss = 0
        
        # To store results
        equity_curve = []
        trades = []
        
        for i, row in self.df.iterrows():
            current_price = row['Close']
            
            # Check for Stop Loss first
            if position == 1:
                if row['Low'] <= stop_loss:
                    # SL hit
                    loss = (entry_price - stop_loss) * position_size
                    capital -= loss
                    trades.append({'type': 'Long SL', 'price': stop_loss, 'pnl': -loss})
                    position = 0
                    
            elif position == -1:
                if row['High'] >= stop_loss:
                    # SL hit
                    loss = (stop_loss - entry_price) * position_size
                    capital -= loss
                    trades.append({'type': 'Short SL', 'price': stop_loss, 'pnl': -loss})
                    position = 0

            # Process new signals if not in position
            if position == 0 and row['signal'] != 0:
                if row['signal'] == 1:
                    position = 1
                    entry_price = current_price
                    sl_distance = row['sl_distance']
                    stop_loss = entry_price - sl_distance
                    position_size = self.risk_manager.calculate_position_size(capital, entry_price, sl_distance)
                    trades.append({'type': 'Long Entry', 'price': entry_price, 'size': position_size})
                
                elif row['signal'] == -1:
                    position = -1
                    entry_price = current_price
                    sl_distance = row['sl_distance']
                    stop_loss = entry_price + sl_distance
                    position_size = self.risk_manager.calculate_position_size(capital, entry_price, sl_distance)
                    trades.append({'type': 'Short Entry', 'price': entry_price, 'size': position_size})
            
            # Close positions on opposite signals
            elif position == 1 and row['signal'] == -1:
                # Close Long
                profit = (current_price - entry_price) * position_size
                capital += profit
                trades.append({'type': 'Long Close', 'price': current_price, 'pnl': profit})
                
                # Enter Short
                position = -1
                entry_price = current_price
                sl_distance = row['sl_distance']
                stop_loss = entry_price + sl_distance
                position_size = self.risk_manager.calculate_position_size(capital, entry_price, sl_distance)
                trades.append({'type': 'Short Entry', 'price': entry_price, 'size': position_size})

            elif position == -1 and row['signal'] == 1:
                # Close Short
                profit = (entry_price - current_price) * position_size
                capital += profit
                trades.append({'type': 'Short Close', 'price': current_price, 'pnl': profit})
                
                # Enter Long
                position = 1
                entry_price = current_price
                sl_distance = row['sl_distance']
                stop_loss = entry_price - sl_distance
                position_size = self.risk_manager.calculate_position_size(capital, entry_price, sl_distance)
                trades.append({'type': 'Long Entry', 'price': entry_price, 'size': position_size})
            
            # Track equity (approximate mark-to-market)
            mtm_capital = capital
            if position == 1:
                mtm_capital += (current_price - entry_price) * position_size
            elif position == -1:
                mtm_capital += (entry_price - current_price) * position_size
                
            equity_curve.append(mtm_capital)
            
        self.df['equity'] = equity_curve
        
        return {
            'initial_capital': self.initial_capital,
            'final_capital': equity_curve[-1] if equity_curve else self.initial_capital,
            'return_pct': ((equity_curve[-1] - self.initial_capital) / self.initial_capital * 100) if equity_curve else 0,
            'total_trades': len([t for t in trades if 'Close' in t['type'] or 'SL' in t['type']]),
            'trades_log': trades
        }
