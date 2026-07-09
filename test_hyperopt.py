import pandas as pd
import numpy as np

# Si yfinance o el entorno fallara, podemos simular datos dummy para probar.
def create_dummy_data(days=1000):
    np.random.seed(42)
    dates = pd.date_range("2020-01-01", periods=days)
    returns = np.random.normal(0, 0.01, size=days)
    close = 100 * np.exp(returns.cumsum())
    df = pd.DataFrame({"Close": close}, index=dates)
    
    # Calcular indicadores básicos para el df dummy
    df['EMA_20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
    
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

def test_hyperopt():
    from src.optimization.hyperopt import HyperoptManager
    from src.agents.strategy_agent import StrategyAgent
    
    print("=== Iniciando Prueba de Optimización ===")
    df = create_dummy_data()
    df = df.dropna()
    
    hyperopt = HyperoptManager()
    
    # Probando RSI
    print("\n[RSI Test]")
    param_space = {
        "rsi_oversold": [20, 25, 30, 35],
        "rsi_overbought": [65, 70, 75, 80]
    }
    
    # Wrapper function for RSI signal generator
    def rsi_signal_func(data, **params):
        return StrategyAgent.generate_vectorized_signals(data, strategy_type="RSI", **params)
        
    best = hyperopt.optimize("RSI_MeanReversion", df, param_space, rsi_signal_func, metric="total_return")
    print(f"Resultado RSI: {best}")

if __name__ == "__main__":
    test_hyperopt()
