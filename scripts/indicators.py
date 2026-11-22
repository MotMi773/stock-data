"""Chart-method signals from Alpha Vantage TIME_SERIES_DAILY (compact) CSV text.

Usage: python3 indicators.py <csv_dir> > signals.json
Each file <csv_dir>/<NAME>.csv is the raw CSV (timestamp,open,high,low,close,volume), newest first.
Prints one JSON object per holding with the 8 method readings (signal: 긍정/중립/부정, note).
"""
import csv, json, os, sys
import pandas as pd

def load(path):
    df = pd.read_csv(path)
    df = df.rename(columns=str.lower).sort_values('timestamp').reset_index(drop=True)
    return df

def analyze(df):
    c, h, l, v = df['close'], df['high'], df['low'], df['volume']
    last = c.iloc[-1]; out = {}
    # Dow theory: swing highs/lows over two 20-day windows
    a, b = df.iloc[-40:-20], df.iloc[-20:]
    hh, hl = b['high'].max() > a['high'].max(), b['low'].min() > a['low'].min()
    sig = '긍정' if hh and hl else '부정' if (not hh and not hl) else '중립'
    out['다우 이론'] = (sig, f"최근 20일 고점 {b['high'].max():.2f}/저점 {b['low'].min():.2f} vs 직전 20일 {a['high'].max():.2f}/{a['low'].min():.2f}")
    # Moving averages (20/50, plus 100 if available)
    ma20, ma50 = c.rolling(20).mean().iloc[-1], c.rolling(50).mean().iloc[-1]
    ma100 = c.rolling(100).mean().iloc[-1] if len(c) >= 100 else None
    above = sum(last > m for m in [ma20, ma50] + ([ma100] if ma100 else []))
    n = 3 if ma100 else 2
    sig = '긍정' if above == n and ma20 > ma50 else '부정' if above == 0 and ma20 < ma50 else '중립'
    out['이동평균'] = (sig, f"종가 {last:.2f}, 20일선 {ma20:.2f}, 50일선 {ma50:.2f}" + (f", 100일선 {ma100:.2f}" if ma100 else ""))
    # RSI 14 (Wilder)
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = (100 - 100 / (1 + up / dn)).iloc[-1]
    sig = '부정' if rsi >= 70 else '긍정' if rsi <= 30 else '중립'
    out['RSI'] = (sig, f"RSI(14) {rsi:.1f} — 70↑ 과열, 30↓ 과매도(반등 여지)")
    # MACD 12/26/9
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean(); sigl = macd.ewm(span=9, adjust=False).mean()
    hist = macd - sigl
    sig = '긍정' if hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2] else '부정' if hist.iloc[-1] < 0 and hist.iloc[-1] < hist.iloc[-2] else '중립'
    out['MACD'] = (sig, f"MACD {macd.iloc[-1]:.2f}, 시그널 {sigl.iloc[-1]:.2f}, 히스토그램 {hist.iloc[-1]:+.2f}(전일 {hist.iloc[-2]:+.2f})")
    # Bollinger 20, 2σ
    mid = c.rolling(20).mean(); sd = c.rolling(20).std()
    up_b, lo_b = (mid + 2 * sd).iloc[-1], (mid - 2 * sd).iloc[-1]
    pb = (last - lo_b) / (up_b - lo_b); width = (up_b - lo_b) / mid.iloc[-1] * 100
    sig = '긍정' if pb < 0.1 else '부정' if pb > 0.95 else '중립'
    out['볼린저'] = (sig, f"%B {pb:.2f}(하단 0·상단 1), 밴드폭 {width:.1f}%, 하단 {lo_b:.2f}/상단 {up_b:.2f}")
    # Ichimoku 9/26/52 (cloud at today = spans computed 26 days ago)
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2; kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    spanA = ((tenkan + kijun) / 2).shift(26); spanB = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    top, bot = max(spanA.iloc[-1], spanB.iloc[-1]), min(spanA.iloc[-1], spanB.iloc[-1])
    pos = '구름 위' if last > top else '구름 아래' if last < bot else '구름 안'
    sig = '긍정' if last > top and tenkan.iloc[-1] > kijun.iloc[-1] else '부정' if last < bot and tenkan.iloc[-1] < kijun.iloc[-1] else '중립'
    out['일목균형표'] = (sig, f"{pos}(구름 {bot:.2f}~{top:.2f}), 전환선 {tenkan.iloc[-1]:.2f} vs 기준선 {kijun.iloc[-1]:.2f}")
    # Elliott (heuristic): position within the 100-day range and leg count of 10% swings
    lo_i, hi_i = c.idxmin(), c.idxmax(); rng = (last - c.min()) / (c.max() - c.min())
    if hi_i < lo_i:
        sig, note = ('중립' if rng > 0.2 else '부정'), f"100일 고점({c.max():.2f}) 이후 하락 국면, 저점 대비 위치 {rng:.0%}. 조정(ABC) 진행 중으로 가정"
    else:
        sig, note = ('긍정' if rng > 0.6 else '중립'), f"100일 저점({c.min():.2f}) 이후 상승 국면, 범위 내 위치 {rng:.0%}. 상승 파동 진행으로 가정"
    out['엘리어트'] = (sig, note + " (주관성 큼)")
    # Wyckoff / volume: OBV trend vs price trend over 20 days, and volume surge
    obv = (v * c.diff().apply(lambda x: 1 if x > 0 else -1 if x < 0 else 0)).cumsum()
    obv_ch, px_ch = obv.iloc[-1] - obv.iloc[-21], c.iloc[-1] / c.iloc[-21] - 1
    vr = v.iloc[-5:].mean() / v.iloc[-50:].mean()
    if px_ch < 0 and obv_ch > 0:
        sig, ph = '긍정', '가격↓·OBV↑ — 매집 가능성'
    elif px_ch > 0 and obv_ch < 0:
        sig, ph = '부정', '가격↑·OBV↓ — 분산 가능성'
    elif px_ch < 0:
        sig, ph = '부정', '가격·OBV 동반 하락 — 하락 추세 확인'
    else:
        sig, ph = '긍정', '가격·OBV 동반 상승 — 상승 추세 확인'
    out['와이코프·거래량'] = (sig, f"{ph}. 20일 수익률 {px_ch:+.1%}, 최근 5일 거래량 50일 평균의 {vr:.1f}배")
    return out, df['timestamp'].iloc[-1], float(last)

if __name__ == '__main__':
    d = sys.argv[1]; res = []
    for f in sorted(os.listdir(d)):
        if f.endswith('.csv'):
            o, date, last = analyze(load(os.path.join(d, f)))
            res.append({"symbol": f[:-4], "asof": date, "close": last, "methods": {k: {"signal": s, "note": n} for k, (s, n) in o.items()}})
    print(json.dumps(res, ensure_ascii=False, indent=1))
