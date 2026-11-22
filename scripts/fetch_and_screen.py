"""Daily: fetch prices for my holdings + US/KR top-100 by market cap, run chart-method signals, rank picks.

Outputs (committed by the GitHub Action):
  data/prices/<SYMBOL>.csv   timestamp,high,low,close,volume (newest first, ~150 rows)
  data/universe.csv          market,symbol,name,rank
  data/signals.json          8-method signals for every symbol fetched
  data/picks.json            top 5 not-held names per market (US, KR) + ALT: top 2 per category (gold, base metals, short bonds)
  data/status.json           what worked / failed this run
"""
import json, os, sys, time, datetime as dt
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from indicators import analyze  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), '..')
DATA = os.path.join(ROOT, 'data'); PX = os.path.join(DATA, 'prices')
os.makedirs(PX, exist_ok=True)
status = {"run_at": dt.datetime.utcnow().isoformat() + "Z", "errors": []}
START = (dt.date.today() - dt.timedelta(days=240)).isoformat()
cfg = json.load(open(os.path.join(ROOT, 'holdings.json'), encoding='utf-8'))  # tickers only, no amounts


def save(symbol, df):
    df = df.rename(columns=str.lower)[['high', 'low', 'close', 'volume']].dropna()
    df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d')
    df = df.sort_index(ascending=False).head(150)
    df.index.name = 'timestamp'
    df.to_csv(os.path.join(PX, f'{symbol}.csv'))
    return len(df)


# ---------- universe ----------
def us_top100():
    import yfinance as yf
    try:
        q = yf.EquityQuery('and', [yf.EquityQuery('eq', ['region', 'us']), yf.EquityQuery('gt', ['intradaymarketcap', 5e10])])
        r = yf.screen(q, sortField='intradaymarketcap', sortAsc=False, size=120)
        rows = [(x['symbol'], x.get('longName') or x.get('shortName') or x['symbol']) for x in r.get('quotes', [])
                if x.get('quoteType') == 'EQUITY' and '.' not in x['symbol'] and '-' not in x['symbol']]
        if len(rows) >= 80:
            return rows[:100]
        status['errors'].append(f'us screener returned {len(rows)}')
    except Exception as e:  # fall back to the static list in the repo
        status['errors'].append(f'us screener: {e}')
    return [tuple(x) for x in json.load(open(os.path.join(ROOT, 'us_top100_fallback.json'), encoding='utf-8'))]


def kr_top100():
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing('KRX')
        cap = 'Marcap' if 'Marcap' in df.columns else 'MarCap'
        df = df[df['Market'].isin(['KOSPI', 'KOSDAQ', 'KOSDAQ GLOBAL'])].sort_values(cap, ascending=False)
        df = df[~df['Name'].str.contains('스팩|리츠|우$|우B$|우C$', regex=True)]
        return [(r.Code, r.Name, r.Market) for r in df.head(100).itertuples()]
    except Exception as e:
        status['errors'].append(f'kr listing fdr: {e}')
    try:
        from pykrx import stock
        d = dt.date.today()
        for _ in range(7):
            ds = d.strftime('%Y%m%d')
            cap = stock.get_market_cap(ds, market='ALL')
            if len(cap):
                cap = cap.sort_values('시가총액', ascending=False).head(110)
                return [(c, stock.get_market_ticker_name(c), '') for c in cap.index][:100]
            d -= dt.timedelta(days=1)
    except Exception as e:
        status['errors'].append(f'kr listing pykrx: {e}')
    return []


def fetch_kr(code, market_hint=''):
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(code, START)
        if len(df) > 60:
            return df
    except Exception as e:
        status['errors'].append(f'fdr {code}: {e}')
    try:
        import yfinance as yf
        for suf in (['.KQ', '.KS'] if 'KOSDAQ' in market_hint else ['.KS', '.KQ']):
            df = yf.download(code + suf, start=START, progress=False, auto_adjust=False)
            if len(df) > 60:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df
    except Exception as e:
        status['errors'].append(f'yf {code}: {e}')
    return None


def fetch_us_many(symbols):
    import yfinance as yf
    out = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        try:
            df = yf.download(chunk, start=START, progress=False, auto_adjust=False, group_by='ticker', threads=True)
            for s in chunk:
                try:
                    sub = df[s] if len(chunk) > 1 else df
                    if len(sub.dropna()) > 60:
                        out[s] = sub
                except Exception:
                    pass
        except Exception as e:
            status['errors'].append(f'yf batch: {e}')
        time.sleep(2)
    return out


# ---------- run ----------
us = us_top100(); kr = kr_top100()
pd.DataFrame([("US", s, n, i + 1) for i, (s, n) in enumerate(us)] + [("KR", c, n, i + 1) for i, (c, n, _) in enumerate(kr)],
             columns=['market', 'symbol', 'name', 'rank']).to_csv(os.path.join(DATA, 'universe.csv'), index=False)

held_us = [h['symbol'] for h in cfg['holdings'] if h['market'] == 'US']
held_kr = [h['symbol'] for h in cfg['holdings'] if h['market'] == 'KR']
alt_path = os.path.join(ROOT, 'alt_universe.json')
alt = json.load(open(alt_path, encoding='utf-8')) if os.path.exists(alt_path) else {'categories': {}, 'picks_per_category': 2}
alt_items = [dict(it, category=cat) for cat, items in alt['categories'].items() for it in items]
alt_us = [it['symbol'] for it in alt_items if it['market'] == 'US']
alt_kr = [it['symbol'] for it in alt_items if it['market'] == 'KR']
fetched = {}
for s, df in fetch_us_many(sorted(set([x for x, _ in us] + held_us + cfg.get('extra_us', []) + alt_us))).items():
    fetched[s] = save(s, df)
kr_hint = {c: m for c, _, m in kr}
for c in sorted(set([c for c, _, _ in kr] + held_kr + alt_kr)):
    df = fetch_kr(c, kr_hint.get(c, ''))
    if df is not None:
        fetched[c] = save(c, df)
    time.sleep(0.3)
status['fetched'] = len(fetched)
status['missing'] = sorted(set([x for x, _ in us] + [c for c, _, _ in kr] + held_us + held_kr) - set(fetched))

# ---------- signals & picks ----------
names = {s: n for s, n in us} | {c: n for c, n, _ in kr} | {it['symbol']: it['name'] for it in alt_items}
signals = {}
for s in fetched:
    try:
        o, date, last = analyze(pd.read_csv(os.path.join(PX, f'{s}.csv')).sort_values('timestamp').reset_index(drop=True))
        signals[s] = {"name": names.get(s, s), "asof": date, "close": last,
                      "methods": {k: {"signal": a, "note": b} for k, (a, b) in o.items()}}
    except Exception as e:
        status['errors'].append(f'analyze {s}: {e}')
json.dump(signals, open(os.path.join(DATA, 'signals.json'), 'w', encoding='utf-8'), ensure_ascii=False)

held = set(held_us + held_kr + cfg.get('exclude', []))


def score(sig):
    m = sig['methods']; pos = sum(v['signal'] == '긍정' for v in m.values()); neg = sum(v['signal'] == '부정' for v in m.values())
    trend_ok = m['이동평균']['signal'] == '긍정' and m['다우 이론']['signal'] != '부정'
    overheated = m['RSI']['signal'] == '부정'  # RSI >= 70
    return pos - 0.5 * neg + (1 if trend_ok else 0) - (1 if overheated else 0), pos, neg


picks = {}
for mk, uni in (('US', [s for s, _ in us]), ('KR', [c for c, _, _ in kr])):
    cands = []
    for s in uni:
        if s in held or s not in signals:
            continue
        sc, pos, neg = score(signals[s])
        cands.append({"symbol": s, "name": signals[s]['name'], "score": round(sc, 2), "positive": pos, "negative": neg,
                      "asof": signals[s]['asof'], "close": signals[s]['close'], "methods": signals[s]['methods']})
    cands.sort(key=lambda x: (-x['score'], -x['positive']))
    picks[mk] = cands[:5]

# Gold / base metals / short-term bonds: best N per category (held ones included, marked)
alt_picks = []
for cat in alt['categories']:
    cands = []
    for it in [x for x in alt_items if x['category'] == cat]:
        s = it['symbol']
        if s not in signals:
            continue
        sc, pos, neg = score(signals[s])
        cands.append({"symbol": s, "name": it['name'], "category": cat, "listing": it['market'], "held": s in held,
                      "score": round(sc, 2), "positive": pos, "negative": neg, "asof": signals[s]['asof'],
                      "close": signals[s]['close'], "methods": signals[s]['methods']})
    cands.sort(key=lambda x: (-x['score'], -x['positive']))
    alt_picks += cands[:alt.get('picks_per_category', 2)]
picks['ALT'] = alt_picks
status['alt_missing'] = sorted(set(alt_us + alt_kr) - set(fetched))
json.dump(picks, open(os.path.join(DATA, 'picks.json'), 'w', encoding='utf-8'), ensure_ascii=False)
json.dump(status, open(os.path.join(DATA, 'status.json'), 'w'), ensure_ascii=False, indent=1)
print(json.dumps({k: v for k, v in status.items() if k != 'errors'}, ensure_ascii=False), len(status['errors']), 'errors')
