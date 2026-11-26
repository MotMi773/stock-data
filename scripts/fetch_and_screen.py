"""Daily: fetch prices for my holdings + US/KR top-100 by market cap, run chart-method signals, rank picks.

Outputs (committed by the GitHub Action):
  data/prices/<SYMBOL>.csv   timestamp,high,low,close,volume (newest first, ~260 rows = 52 weeks)
  data/universe.csv          market,symbol,name,rank
  data/signals.json          8-method signals for every symbol fetched
  data/picks.json            top 10 not-held names per market (US, KR) + ALT: top 2 per category (gold, base metals, short bonds)
                             + US_DIP / KR_DIP: 10 names per market with healthy trend but price well off the 52-week high (덜 오른 후보)
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
START = (dt.date.today() - dt.timedelta(days=400)).isoformat()
cfg = json.load(open(os.path.join(ROOT, 'holdings.json'), encoding='utf-8'))  # tickers only, no amounts


def save(symbol, df):
    df = df.rename(columns=str.lower)[['high', 'low', 'close', 'volume']].dropna()
    df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d')
    df = df.sort_index(ascending=False).head(260)
    df.index.name = 'timestamp'
    df.to_csv(os.path.join(PX, f'{symbol}.csv'))
    return len(df)


# ---------- universe ----------
def us_top100():
    import yfinance as yf
    try:
        # US-listed only: OTC tickers (e.g. CICHY, CICHF) and foreign duplicates are excluded by exchange code
        exch = ['NMS', 'NYQ', 'NGM', 'NCM', 'ASE']
        q = yf.EquityQuery('and', [yf.EquityQuery('eq', ['region', 'us']), yf.EquityQuery('is-in', ['exchange'] + exch),
                                   yf.EquityQuery('gt', ['intradaymarketcap', 5e10])])
        r = yf.screen(q, sortField='intradaymarketcap', sortAsc=False, size=150)
        rows, seen = [], set()
        for x in r.get('quotes', []):
            nm = x.get('longName') or x.get('shortName') or x['symbol']
            if x.get('quoteType') != 'EQUITY' or x.get('exchange') not in exch or '.' in x['symbol'] or '-' in x['symbol'] or nm in seen:
                continue  # one listing per company (e.g. GOOGL/GOOG share one name)
            seen.add(nm); rows.append((x['symbol'], nm))
        if len(rows) >= 80:
            return rows[:100]
        status['errors'].append(f'us screener returned {len(rows)}')
    except Exception as e:  # fall back to the static list in the repo
        status['errors'].append(f'us screener: {e}')
    return [tuple(x) for x in json.load(open(os.path.join(ROOT, 'us_top100_fallback.json'), encoding='utf-8'))]


def kr_top100():
    """KR top-100 by market cap. Each source must pass a sanity check (Samsung Electronics 005930 in the list),
    because a listing whose market-cap column is empty silently sorts alphabetically."""
    def ok(rows):
        return len(rows) >= 80 and any(r[0] == '005930' for r in rows)
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing('KRX')
        cap = 'Marcap' if 'Marcap' in df.columns else 'MarCap'
        df[cap] = pd.to_numeric(df[cap], errors='coerce')
        df = df[df['Market'].isin(['KOSPI', 'KOSDAQ', 'KOSDAQ GLOBAL']) & (df[cap] > 0)].sort_values(cap, ascending=False)
        df = df[~df['Name'].str.contains('스팩|리츠|우$|우B$|우C$', regex=True)]
        rows = [(r.Code, r.Name, r.Market) for r in df.head(100).itertuples()]
        if ok(rows):
            status['kr_universe_source'] = 'fdr'
            return rows
        status['errors'].append(f'kr listing fdr: market cap missing ({len(rows)} rows)')
    except Exception as e:
        status['errors'].append(f'kr listing fdr: {e}')
    try:
        from pykrx import stock
        d = dt.date.today()
        for _ in range(7):
            ds = d.strftime('%Y%m%d')
            cap = stock.get_market_cap(ds, market='ALL')
            if len(cap) and cap['시가총액'].sum() > 0:
                cap = cap.sort_values('시가총액', ascending=False).head(130)
                rows = [(c, stock.get_market_ticker_name(c), '') for c in cap.index]
                rows = [r for r in rows if not any(k in r[1] for k in ('스팩', '리츠')) and not r[1].endswith(('우', '우B', '우C'))][:100]
                if ok(rows):
                    status['kr_universe_source'] = 'pykrx'
                    return rows
            d -= dt.timedelta(days=1)
    except Exception as e:
        status['errors'].append(f'kr listing pykrx: {e}')
    try:
        import yfinance as yf
        q = yf.EquityQuery('and', [yf.EquityQuery('eq', ['region', 'kr']), yf.EquityQuery('is-in', ['exchange', 'KSC', 'KOE'])])
        r = yf.screen(q, sortField='intradaymarketcap', sortAsc=False, size=130)
        rows = []
        for it in r.get('quotes', []):
            sym = it.get('symbol', ''); code = sym.split('.')[0]
            name = it.get('longName') or it.get('shortName') or code
            if not code.isdigit() or code[-1] != '0':   # common shares end in 0; preferred end in 5/7/9
                continue
            rows.append((code, name, 'KOSDAQ' if sym.endswith('.KQ') else 'KOSPI'))
        rows = rows[:100]
        if ok(rows):
            status['kr_universe_source'] = 'yfinance'
            return rows
        status['errors'].append(f'kr listing yfinance: sanity check failed ({len(rows)} rows)')
    except Exception as e:
        status['errors'].append(f'kr listing yfinance: {e}')
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


def price_zone(sym):
    """Where the price sits vs its own history: 52w high/low, 200-day MA, RSI(14)."""
    df = pd.read_csv(os.path.join(PX, f'{sym}.csv')).sort_values('timestamp')
    df = df[(df['close'] > 0) & (df['high'] > 0) & (df['low'] > 0)]
    if len(df) < 60:
        raise ValueError('not enough history')
    c = df['close'].astype(float); last = float(c.iloc[-1])
    w = df.tail(252); hi = float(w['high'].max()); lo = float(w['low'].min())
    n = len(c); ma_n = 200 if n >= 200 else max(n // 2, 20)
    ma = c.rolling(ma_n).mean(); ma_last = float(ma.iloc[-1])
    ma_prev = float(ma.iloc[-21]) if n > ma_n + 20 else ma_last
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14).mean()
    rsi = float(100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1])) if dn.iloc[-1] > 0 else 100.0
    return {"high52": round(hi, 2), "low52": round(lo, 2),
            "off_high": round(1 - last / hi, 4),                       # 52주 고점 대비 하락률
            "range_pos": round((last - lo) / (hi - lo), 4) if hi > lo else 1.0,  # 52주 범위 내 위치 0=저점 1=고점
            "vs_ma200": round(last / ma_last - 1, 4), "ma200_slope": round(ma_last / ma_prev - 1, 4),
            "ma_days": ma_n, "rsi14": round(rsi, 1), "history_days": n}


def dip_score(sc, z):
    """덜 오른 후보 점수: 할인폭 + 범위 하단 + 장기추세 건강 + 200일선 근접 + 기존 신호 점수 보조."""
    disc = min(z['off_high'], 0.35) / 0.35                 # 0~1, 고점 대비 35% 이상 하락은 가산 멈춤
    s = 2 * disc + (1 - z['range_pos'])
    s += 1 if z['ma200_slope'] > 0 else 0                  # 장기선이 아직 우상향 = 추세 속 눌림
    s += 1 if -0.08 <= z['vs_ma200'] <= 0.10 else 0        # 장기 지지선 근처
    s += 0.5 if 35 <= z['rsi14'] <= 55 else 0              # 과열도 과매도 붕괴도 아님
    return round(s + max(sc, 0) / 4, 2)


def dip_type(z):
    if z['ma200_slope'] > 0 and z['vs_ma200'] >= -0.05:
        return '상승추세 속 눌림목'
    if abs(z['ma200_slope']) <= 0.01:
        return '박스권 하단'
    return '낙폭과대 반등 시도'


zones = {}
for s in signals:
    try:
        zones[s] = price_zone(s)
    except Exception as e:
        status['errors'].append(f'zone {s}: {e}')

picks = {}
for mk, uni in (('US', [s for s, _ in us]), ('KR', [c for c, _, _ in kr])):
    cands = []
    for s in uni:
        if s in held or s not in signals:
            continue
        sc, pos, neg = score(signals[s])
        cands.append({"symbol": s, "name": signals[s]['name'], "score": round(sc, 2), "positive": pos, "negative": neg,
                      "asof": signals[s]['asof'], "close": signals[s]['close'], "methods": signals[s]['methods'],
                      "zone": zones.get(s)})
    cands.sort(key=lambda x: (-x['score'], -x['positive']))
    picks[mk] = cands[:10]
    # 덜 오른 후보: 신호가 무너지지 않았고(score>=0, 부정<=3) 52주 고점보다 10~45% 낮고 RSI<=60
    dips = []
    for x in cands:
        z = x['zone']
        if not z or x['score'] < 0 or x['negative'] > 3:
            continue
        if not (0.10 <= z['off_high'] <= 0.45) or z['rsi14'] > 60:
            continue
        dips.append(dict(x, dip_score=dip_score(x['score'], z), dip_type=dip_type(z)))
    dips.sort(key=lambda x: (-x['dip_score'], -x['score']))
    picks[mk + '_DIP'] = dips[:10]

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
                      "close": signals[s]['close'], "methods": signals[s]['methods'], "zone": zones.get(s)})
    cands.sort(key=lambda x: (-x['score'], -x['positive']))
    alt_picks += cands[:alt.get('picks_per_category', 2)]
picks['ALT'] = alt_picks
status['alt_missing'] = sorted(set(alt_us + alt_kr) - set(fetched))
json.dump(picks, open(os.path.join(DATA, 'picks.json'), 'w', encoding='utf-8'), ensure_ascii=False)
json.dump(status, open(os.path.join(DATA, 'status.json'), 'w'), ensure_ascii=False, indent=1)
print(json.dumps({k: v for k, v in status.items() if k != 'errors'}, ensure_ascii=False), len(status['errors']), 'errors')
