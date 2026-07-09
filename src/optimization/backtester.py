import pandas as pd
import numpy as np
from typing import Callable, Dict, Any

class VectorizedBacktester:
    """
    Backtester vectorizado ultrarrápido inspirado en los estándares institucionales.
    Calcula PnL, Drawdown y Sharpe Ratio de forma vectorial usando Pandas.
    """

    def __init__(
        self,
        initial_capital: float = 10000.0,
        commission: float = 0.001,  # 0.1% de comisión por trade
        slippage: float = 0.0005    # 0.05% de slippage
    ):
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage

    def run(
        self,
        prices: pd.DataFrame,
        signal_func: Callable[[pd.DataFrame], pd.Series]
    ) -> Dict[str, Any]:
        """
        Ejecuta el backtest.
        prices: DataFrame que contiene al menos la columna 'Close'.
        signal_func: Función que recibe el df y devuelve una Serie con señales (-1, 0, 1).
        """
        if len(prices) == 0:
            return {"metrics": {"total_return": 0, "sharpe_ratio": 0, "max_drawdown": 0}}
            
        # Generamos las señales. Hacemos shift(1) para evitar el sesgo de mirar al futuro (Look-ahead bias)
        # La señal del cierre de hoy determina la posición de mañana.
        signals = signal_func(prices).shift(1).fillna(0)
        
        # Retornos logarítmicos o porcentuales del activo
        returns = prices["Close"].pct_change().fillna(0)
        
        # Costos de trading (comisión + slippage) cada vez que la señal cambia
        position_changes = signals.diff().abs().fillna(0)
        trading_costs = position_changes * (self.commission + self.slippage)
        
        # Retornos de la estrategia
        strategy_returns = (signals * returns) - trading_costs
        
        # Curva de capital (Equity Curve)
        equity = (1 + strategy_returns).cumprod() * self.initial_capital
        
        return {
            "equity": equity,
            "returns": strategy_returns,
            "metrics": self._calculate_metrics(strategy_returns, equity)
        }

    def _calculate_metrics(self, returns: pd.Series, equity: pd.Series) -> Dict[str, float]:
        """
        Calcula las métricas institucionales.
        """
        if len(equity) == 0 or len(returns) == 0:
             return {"total_return": 0, "annual_return": 0, "sharpe_ratio": 0, "max_drawdown": 0, "win_rate": 0, "num_trades": 0}
             
        total_return = (equity.iloc[-1] / self.initial_capital) - 1
        
        # Asumimos que los datos pueden no ser exactamente diarios, pero promediamos
        # En cripto es 365, en bolsa es 252. Usaremos 365 como proxy por ahora.
        annual_vol = returns.std() * np.sqrt(365)
        
        # Ratio de Sharpe (Asumiendo Risk Free Rate = 0 por simplicidad)
        annual_return = (1 + total_return) ** (365 / len(returns)) - 1 if len(returns) > 0 else 0
        sharpe = annual_return / annual_vol if annual_vol > 0 else 0
        
        # Drawdown Máximo
        rolling_max = equity.cummax()
        drawdown = (equity - rolling_max) / rolling_max
        max_drawdown = drawdown.min()
        
        # Win Rate
        winning_days = (returns > 0).sum()
        total_days = (returns != 0).sum()
        win_rate = winning_days / total_days if total_days > 0 else 0
        
        return {
            "total_return": float(total_return),
            "sharpe_ratio": float(sharpe),
            "max_drawdown": float(max_drawdown),
            "win_rate": float(win_rate),
            "num_trades": int((returns != 0).sum())
        }
