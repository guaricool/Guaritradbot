import yfinance as yf
import pandas as pd
import os

def download_data(ticker, interval, start_date=None, end_date=None, period="60d"):
    """
    Downloads historical data from Yahoo Finance.
    Intervals can be '15m', '1h', '4h', '1d', etc.
    Yahoo Finance limits 15m to 60 days, 1h to 730 days.
    """
    print(f"Downloading {ticker} at {interval} interval...")
    df = yf.download(ticker, interval=interval, period=period, start=start_date, end=end_date)
    
    if df.empty:
        print(f"Warning: No data found for {ticker} at {interval}.")
        return df

    # Flatten multi-index columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]
        
    df.reset_index(inplace=True)
    df.rename(columns={'index': 'Datetime', 'Date': 'Datetime'}, inplace=True)
    
    # Save to CSV
    os.makedirs('data_store', exist_ok=True)
    file_path = f"data_store/{ticker}_{interval}.csv"
    df.to_csv(file_path, index=False)
    print(f"Saved {ticker} data to {file_path}")
    return df

if __name__ == "__main__":
    # Test downloading the required assets
    assets = {
        'SPY': '15m',
        'QQQ': '15m',
        'BTC-USD': '1h',
        'GLD': '1d', # Yahoo API limits 4h? Actually '1h' can be resampled to 4h, or '1d'. Let's use 1h for now and resample later if needed. '1h' works for 730 days. '1d' works max.
        'USO': '1d'
    }
    for symbol, tf in assets.items():
        download_data(symbol, interval=tf, period="60d")
