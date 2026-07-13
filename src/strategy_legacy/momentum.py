import pandas as pd

def sma_crossover_strategy(prices: pd.DataFrame, short_window: int = 10, long_window: int = 30) -> pd.Series:
    """
    Estrategia de cruce de Medias Móviles Simples (SMA).
    Detecta 'subidas' cuando la media corta cruza por encima de la media larga (Señal 1: Compra).
    Detecta 'bajadas' cuando la media corta cruza por debajo de la media larga (Señal -1: Venta/Short).
    
    Args:
        prices: DataFrame con columna 'Close' (Sprint 43 L5 fix:
            was 'close' lowercase, inconsistent with the rest of the
            pipeline which uses 'Close')
        short_window: Periodo para la media móvil rápida
        long_window: Periodo para la media móvil lenta

    Returns:
        pd.Series: Serie de señales (1, 0, -1)
    """
    # Calcular medias móviles
    sma_short = prices['Close'].rolling(window=short_window).mean()
    sma_long = prices['Close'].rolling(window=long_window).mean()
    
    # Crear serie de señales
    signals = pd.Series(0, index=prices.index)
    
    # Lógica de señales (1 para Long, -1 para Short)
    # Long: Media corta > Media larga
    signals[sma_short > sma_long] = 1
    
    # Short / Flat: Media corta < Media larga
    # Por ahora sólo vamos 'long' o 'cash' (0) para ser conservadores, pero si queremos short, ponemos -1
    signals[sma_short < sma_long] = -1
    
    return signals
