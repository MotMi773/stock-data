"""On demand: long daily history (~12y) for given symbols -> data/history/<SYMBOL>.csv (oldest first).
Usage: python scripts/history.py 000270 AAPL ...  (6-digit = KRX via FinanceDataReader, else yfinance)"""
import os, sys, datetime as dt
import pandas as pd
OUT = os.path.join(os.path.dirname(__file__), '..', 'data', 'history'); os.makedirs(OUT, exist_ok=True)
start = (dt.date.today() - dt.timedelta(days=365 * 12)).isoformat()
for s in sys.argv[1:]:
    try:
        if s.isdigit():
            import FinanceDataReader as fdr
            df = fdr.DataReader(s, start)
        else:
            import yfinance as yf
            df = yf.download(s, start=start, progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[['open', 'high', 'low', 'close', 'volume']].dropna()
        df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d'); df.index.name = 'timestamp'
        df.to_csv(os.path.join(OUT, f'{s}.csv')); print(s, len(df))
    except Exception as e:
        print('fail', s, e)
