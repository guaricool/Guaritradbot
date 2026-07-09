import ccxt
import pandas as pd
import datetime
import time

def fetch_historical_data(symbol: str, timeframe: str, since: str, limit: int = 1000) -> pd.DataFrame:
    """
    Downloads historical OHLCV data from Binance using CCXT.
    
    Args:
        symbol: The trading pair (e.g., 'BTC/USDT')
        timeframe: The timeframe (e.g., '1d', '1h', '15m')
        since: The start date as a string (e.g., '2023-01-01T00:00:00Z')
        limit: Number of candles per request (max 1000 for Binance)
        
    Returns:
        pd.DataFrame: A DataFrame with OHLCV data.
    """
    exchange = ccxt.kraken({
        'enableRateLimit': True,
    })
    
    since_timestamp = exchange.parse8601(since)
    all_ohlcv = []
    
    print(f"Descargando datos para {symbol} ({timeframe}) desde {since}...")
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since_timestamp, limit)
            if not len(ohlcv):
                break
                
            all_ohlcv.extend(ohlcv)
            since_timestamp = ohlcv[-1][0] + 1  # Get the next timestamp
            
            # Binance sometimes returns exactly limit, sometimes less if it's the end
            if len(ohlcv) < limit:
                break
                
            # Prevent hitting rate limits aggressively
            time.sleep(0.1)
            
        except Exception as e:
            print(f"Error descargando datos: {e}")
            break
            
    if not all_ohlcv:
        print("No se encontraron datos.")
        return pd.DataFrame()
        
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    
    print(f"Descarga completada: {len(df)} velas descargadas.")
    return df

if __name__ == "__main__":
    # Test the function
    df = fetch_historical_data("BTC/USDT", "1d", "2023-01-01T00:00:00Z")
    print(df.head())
    print(df.tail())
    df.to_csv("btc_usdt_1d.csv")
    print("Guardado en btc_usdt_1d.csv")
