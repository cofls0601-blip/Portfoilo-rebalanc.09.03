import json, sqlite3, re
from datetime import date
from pathlib import Path
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='자산배분 리밸런싱 도우미', page_icon='📊', layout='wide')
ROOT = Path(__file__).parent

def secret(name, default=''):
    """st.secrets.get()은 secrets.toml이 아예 없으면 기본값을 반환하지 않고 예외를 던진다(스트림릿 특유의 함정).
    그래서 항상 이 안전한 getter를 통해서만 시크릿에 접근한다."""
    try:
        return st.secrets[name]
    except Exception:
        return default

# app.py를 새로 받을 때마다 저장 위치(폴더)가 달라지면 그 옆의 portfolio.db도 매번 새로 생겨
# "데이터가 사라진 것처럼" 보인다. 그래서 스크립트 위치와 무관하게 항상 같은 사용자 홈 디렉터리 하위에
# DB를 둔다. SQLITE_PATH를 secrets에 직접 지정하면 그 값이 우선한다(예: 클라우드 배포 시 영구볼륨 경로).
try:
    _default_db_dir = Path.home() / '.asset_allocation_app'
    _default_db_dir.mkdir(parents=True, exist_ok=True)
    _default_db_path = str(_default_db_dir / 'portfolio.db')
except Exception:
    _default_db_path = str(ROOT / 'portfolio.db')
DB_PATH = secret('SQLITE_PATH', _default_db_path)

CATEGORY_OPTIONS = ['현금', '금', '선진국 주식', '신흥국 주식', '선진국 채권', '신흥국 채권', '기타']
YAHOO_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# ---------- Safe data normalization ----------
def safe_prices(value, fallback=None):
    if value is None:
        return list(fallback or [])
    try:
        if pd.isna(value):
            return list(fallback or [])
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        try: value = json.loads(value)
        except Exception: value = value.split(',')
    raw = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for x in raw:
        try:
            if x is not None and not pd.isna(x): out.append(float(x))
        except (TypeError, ValueError): pass
    return out

def n(v, default=0.0):
    try:
        if v is None or pd.isna(v): return default
        return float(v)
    except (TypeError, ValueError): return default

def clean_records(df):
    df = df.copy()
    for c in ['target_pct', 'shares', 'close']:
        if c not in df: df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0.0)
    if 'prices' not in df: df['prices'] = [[] for _ in range(len(df))]
    df['prices'] = df['prices'].apply(safe_prices)
    for c in ['strategy', 'ticker', 'name', 'market', 'role', 'signal_ticker', 'category']:
        if c not in df: df[c] = ''
        df[c] = df[c].fillna('').astype(str)
    df['signal_ticker'] = df.apply(lambda r: r['signal_ticker'] or r['ticker'], axis=1)
    df['market'] = df['market'].apply(lambda m: m if m in ('KR', 'US') else 'KR')
    df['category'] = df['category'].apply(lambda c: c if c in CATEGORY_OPTIONS else '기타')
    if 'id' not in df or df['id'].isna().any() or (df['id'] == '').any():
        df['id'] = [str(i) for i in range(len(df))]
    return df.reset_index(drop=True)

# ---------- 전략/자산 기본값 ----------
# LAA 자산 목표비중 합이 원래 매뉴얼(12.5+12.5+12.5+15.5+25+25=103%)대로면 100%를 넘어 저장이 안 됩니다.
# 국채 비중을 22%로 맞춰 정확히 100%가 되도록 보정했습니다 — 실제 원하시는 배분과 다르면 전략 구성에서 조정하세요.
DEFAULT_STRATEGIES = [
    {'code': 'LAA', 'account': '과세 연금저축', 'description': '변형 LAA — 나스닥/유로스탁스만 10개월 SMA 필터, 이탈 시 현금화. 목표비중 복원은 분기 말에만.', 'dynamic': False},
    {'code': 'GSM', 'account': '비과세 연금저축', 'description': '글로벌 단순 모멘텀 — SMA 통과 후보 중 12개월 수익률 1위에 80% 투자, 20% 현금. 월 1회 리밸런싱.', 'dynamic': True},
    {'code': 'ISA', 'account': 'ISA', 'description': '나스닥 레버리지 트리거 — 나스닥100 고점대비 -10% 하락 시 분할매수.', 'dynamic': False},
    {'code': 'SSO', 'account': '일반계좌 2', 'description': 'S&P500 ETF + 현금성 자산. S&P500 고점대비 -15% 하락 시 현금 절반 투입.', 'dynamic': False},
    {'code': 'EM', 'account': '일반계좌 1', 'description': '신흥국 분산 장기보유. 리밸런싱은 연 1회 정도만.', 'dynamic': False},
]
KNOWN_STRATEGIES = {'LAA', 'GSM', 'ISA', 'SSO', 'EM'}  # 전용 리밸런싱 규칙이 있는 전략(하드코딩된 룰)

DEFAULT_ROWS = [
    # strategy, ticker, name, market, role, target_pct, category
    ('LAA', '133690', 'TIGER 미국나스닥100', 'KR', 'NASDAQ', 12.5, '선진국 주식'),
    ('LAA', '245350', 'TIGER 유로스탁스배당30', 'KR', 'EuroStoxx', 12.5, '선진국 주식'),
    ('LAA', '360750', 'TIGER 미국S&P500', 'KR', 'S&P500', 12.5, '선진국 주식'),
    ('LAA', '251350', 'KODEX 선진국MSCI World', 'KR', 'MSCI World', 15.5, '선진국 주식'),
    ('LAA', '132030', 'KODEX 골드선물(H)', 'KR', 'Gold', 25.0, '금'),
    ('LAA', '148070', 'KIWOOM 국고채10년', 'KR', 'Bond', 22.0, '선진국 채권'),
    ('LAA', 'CASH', '현금', 'KR', '필터이탈 대기현금', 0.0, '현금'),
    ('GSM', '360750', 'TIGER 미국S&P500', 'KR', 'GSM 후보', 0.0, '선진국 주식'),
    ('GSM', '251350', 'KODEX 선진국MSCI World', 'KR', 'GSM 후보', 0.0, '선진국 주식'),
    ('GSM', '133690', 'TIGER 미국나스닥100', 'KR', 'GSM 후보', 0.0, '선진국 주식'),
    ('GSM', '245350', 'TIGER 유로스탁스배당30', 'KR', 'GSM 후보', 0.0, '선진국 주식'),
    ('GSM', 'CASH', '현금', 'KR', '대기현금', 20.0, '현금'),
    ('ISA', '418660', 'TIGER 미국나스닥100레버리지(합성)', 'KR', '-10% 트리거', 0.0, '선진국 주식'),
    ('ISA', 'CASH', '현금', 'KR', '대기현금', 100.0, '현금'),
    ('SSO', '360750', 'TIGER 미국S&P500', 'KR', 'S&P500 기준', 70.0, '선진국 주식'),
    ('SSO', '153130', 'KODEX 단기채권', 'KR', '현금성', 30.0, '현금'),
    ('EM', '069500', 'KODEX 200', 'KR', '한국', 25.0, '신흥국 주식'),
    ('EM', '', '중국 ETF 입력', 'KR', '중국', 25.0, '신흥국 주식'),
    ('EM', '', '인도 ETF 입력', 'KR', '인도', 25.0, '신흥국 주식'),
    ('EM', '', '베트남 ETF 입력', 'KR', '베트남', 25.0, '신흥국 주식'),
]
# ISA는 실제로 레버리지 상품(418660)을 매매하지만, 트리거 판단은 원지수 성격의 나스닥100(133690) 고점대비 하락률로 해야
# 레버리지 자체의 변동성에 낚이지 않는다. signal_ticker가 신호 판단용 티커, ticker는 실제 매매 티커.
SIGNAL_TICKER_OVERRIDE = {'418660': '133690'}

def default_assets():
    rows = []
    for i, (strat, ticker, nm, mkt, role, tgt, cat) in enumerate(DEFAULT_ROWS):
        rows.append({
            'id': str(i), 'strategy': strat, 'ticker': ticker, 'name': nm, 'market': mkt, 'role': role,
            'target_pct': tgt, 'shares': 0.0, 'close': (1.0 if ticker == 'CASH' else 0.0), 'prices': [],
            'signal_ticker': SIGNAL_TICKER_OVERRIDE.get(ticker, ticker), 'category': cat,
        })
    return pd.DataFrame(rows)

# ---------- SQLite persistence ----------
def init_db():
    con = sqlite3.connect(DB_PATH); con.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS price_cache(ticker TEXT, date TEXT, close REAL, PRIMARY KEY(ticker, date))')
    for k, v in [
        ('assets', default_assets().to_json(orient='records', force_ascii=False)),
        ('history', '[]'), ('equity', '[]'), ('cashflows', '[]'), ('benchmarks', '[]'),
        ('strategies', json.dumps(DEFAULT_STRATEGIES, ensure_ascii=False)),
        ('category_targets', json.dumps({c: 0.0 for c in CATEGORY_OPTIONS}, ensure_ascii=False)),
    ]:
        con.execute('INSERT OR IGNORE INTO kv(k,v) VALUES(?,?)', (k, v))
    con.commit(); con.close()

def get_state(k):
    init_db(); con = sqlite3.connect(DB_PATH); r = con.execute('SELECT v FROM kv WHERE k=?', (k,)).fetchone(); con.close()
    return json.loads(r[0])

def put_state(k, v):
    init_db(); con = sqlite3.connect(DB_PATH)
    con.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)', (k, json.dumps(v, ensure_ascii=False, default=str)))
    con.commit(); con.close()

# ---------- 종목별 가격 캐시 (과거 확정 데이터는 다시 불러올 필요 없음) ----------
def cache_get_prices(ticker):
    init_db(); con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query('SELECT date, close FROM price_cache WHERE ticker=? ORDER BY date', con, params=(ticker,))
    con.close()
    return df

def cache_put_prices(ticker, rows):
    if not rows: return
    init_db(); con = sqlite3.connect(DB_PATH)
    con.executemany('INSERT OR REPLACE INTO price_cache(ticker,date,close) VALUES(?,?,?)',
                     [(ticker, r['date'], r['close']) for r in rows if r.get('date') and r.get('close') is not None])
    con.commit(); con.close()

def cache_clear_prices():
    init_db(); con = sqlite3.connect(DB_PATH); con.execute('DELETE FROM price_cache'); con.commit(); con.close()

# ---------- KRX 종목(ETF+개별주식) 카탈로그 ----------
@st.cache_data(ttl=86400, show_spinner=False)
def load_krx_universe(asof, source='krx'):
    """한국 상장 종목(ETF+개별주식) 검색용 카탈로그.
    이전에는 pykrx(비공식 스크래핑 라이브러리)에만 의존했는데, 설치가 안 돼 있거나 실패하면
    아무 안내 없이 빈 목록이 나오는 문제가 있었다. 이제는 가격 조회에 이미 쓰고 있는(즉 이미
    인증이 확인된) KRX Open API 응답에서 직접 ETF 전체 목록(티커+이름)을 뽑아 우선 사용하고,
    pykrx는 개별주식 보강용으로만 best-effort로 시도한다.
    반환값에 'error' 컬럼이 있으면 검색 UI에서 그 사유를 그대로 보여준다.
    """
    frames = []; note = ''
    url, key = secret('KRX_BASE_URL'), secret('KRX_AUTH_KEY')
    if url and key:
        d = pd.Timestamp(asof)
        got = False
        for _ in range(10):
            try:
                r = requests.get(url, headers={'AUTH_KEY': key}, params={'basDd': d.strftime('%Y%m%d')}, timeout=30)
                r.raise_for_status()
                rows = r.json().get('OutBlock_1', [])
                if rows:
                    etf_df = pd.DataFrame([
                        {'ticker': re.sub(r'\D', '', str(x.get('ISU_CD', ''))), 'name': str(x.get('ISU_NM', ''))}
                        for x in rows if x.get('ISU_CD')
                    ])
                    etf_df = etf_df[etf_df['ticker'] != '']
                    etf_df['type'] = 'ETF'
                    frames.append(etf_df); got = True
                    break
            except Exception as e:
                note = str(e)
            d -= pd.Timedelta(days=1)
        if not got and not note: note = 'KRX 응답이 비어 있음(휴장일 반복?)'
    else:
        note = 'KRX_AUTH_KEY/KRX_BASE_URL이 secrets에 설정되어 있지 않음'
    try:
        from pykrx import stock
        for mkt in ('KOSPI', 'KOSDAQ'):
            st_t = stock.get_market_ticker_list(pd.Timestamp(asof).strftime('%Y%m%d'), market=mkt)
            frames.append(pd.DataFrame([{'ticker': str(t), 'name': str(stock.get_market_ticker_name(t)), 'type': f'주식({mkt})'} for t in st_t]))
    except Exception:
        pass  # 개별주식 보강은 실패해도 ETF 검색엔 지장 없음(조용히 건너뜀)
    if frames:
        out = pd.concat(frames, ignore_index=True).drop_duplicates(subset=['ticker'])
        out['market'] = 'KR'
        if not out.empty: return out
    p = ROOT / 'krx_etf_fallback.csv'
    if p.exists():
        d2 = pd.read_csv(p, dtype=str).fillna(''); d2['type'] = 'ETF'
        return d2
    empty = pd.DataFrame(columns=['ticker', 'name', 'market', 'type'])
    empty.attrs['error'] = note or 'KRX 목록을 가져오지 못함'
    return empty

@st.cache_data(ttl=3600, show_spinner=False)
def search_us_symbols(query):
    if not query: return pd.DataFrame(columns=['ticker', 'name', 'exchange'])
    try:
        r = requests.get('https://query2.finance.yahoo.com/v1/finance/search',
                          params={'q': query, 'quotesCount': 15, 'newsCount': 0}, headers=YAHOO_HEADERS, timeout=15)
        r.raise_for_status(); quotes = r.json().get('quotes', [])
        rows = [{'ticker': q.get('symbol'), 'name': q.get('shortname') or q.get('longname') or q.get('symbol'), 'exchange': q.get('exchange', '')}
                for q in quotes if q.get('symbol') and q.get('quoteType') in ('EQUITY', 'ETF', None)]
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame(columns=['ticker', 'name', 'exchange'])

# ---------- KRX / data.go 가격 어댑터 ----------
def normalize_payload(payload, requested=''):
    if isinstance(payload, dict):
        rows = payload.get('data', payload.get('OutBlock_1', payload.get('response', {}).get('body', {}).get('items', {}).get('item', payload)))
    else:
        rows = payload
    if isinstance(rows, dict): rows = [rows]
    out = []
    for x in rows or []:
        if not isinstance(x, dict): continue
        # KRX Open API(ETF 일별매매정보)는 종목코드 필드가 ISU_CD, 기준일자가 BAS_DD로 온다.
        # 이 두 키가 빠져 있으면 모든 행의 ticker가 요청값(requested)으로 뭉개지고, 그 결과
        # 필터링이 항상 "전체 응답"을 반환해 마지막 행(임의의 한 종목) 가격이 모든 티커에 붙는 버그가 생긴다.
        raw_ticker = str(x.get('symbol', x.get('ISU_SRT_CD', x.get('ISU_CD', x.get('isu_srt_cd', x.get('ticker', requested))))))
        digits = re.sub(r'\D', '', raw_ticker)
        ticker = digits if digits else raw_ticker.replace('.KS', '').strip()
        d = str(x.get('date', x.get('basDd', x.get('BAS_DD', x.get('stck_bsop_date', ''))))).replace('-', '')
        close = x.get('close', x.get('TDD_CLSPRC', x.get('stck_clpr', x.get('price'))))
        if close is None or close == '-': continue
        try:
            out.append({'ticker': ticker, 'date': d, 'close': float(str(close).replace(',', ''))})
        except (ValueError, TypeError):
            pass
    return pd.DataFrame(out)

def fetch_day(source, ticker, day):
    ticker_norm = re.sub(r'\D', '', str(ticker)) or str(ticker)
    if source == 'krx':
        url, key = secret('KRX_BASE_URL'), secret('KRX_AUTH_KEY')
        if not url or not key: raise RuntimeError('KRX_BASE_URL/KRX_AUTH_KEY 미설정')
        r = requests.get(url, headers={'AUTH_KEY': key}, params={'basDd': day.replace('-', '')}, timeout=30)
    else:
        url, key = secret('DATA_GO_URL'), secret('DATA_GO_SERVICE_KEY')
        if not url or not key: raise RuntimeError('DATA_GO_URL/DATA_GO_SERVICE_KEY 미설정')
        r = requests.get(url, params={'serviceKey': key, 'resultType': 'json', 'numOfRows': 1000, 'pageNo': 1,
                                       'basDt': day.replace('-', ''), 'itmsNm': ticker}, timeout=30)
    r.raise_for_status(); df = normalize_payload(r.json(), ticker_norm)
    if df.empty: raise RuntimeError(f'{ticker}: 응답 없음(휴장일이거나 API 설정 확인 필요)')
    hit = df[df['ticker'].eq(ticker_norm)]
    if hit.empty: raise RuntimeError(f'{ticker}: 해당 일자 데이터에서 종목코드를 찾지 못함')
    return hit

def find_trading_day_price(source, ticker, target_date, max_back=10):
    """target_date가 휴장일(주말·공휴일)이면 하루씩 앞으로 물러나며 실제 거래일 종가를 찾는다."""
    d = pd.Timestamp(target_date)
    for _ in range(max_back):
        try:
            x = fetch_day(source, str(ticker), d.strftime('%Y-%m-%d'))
            return x.iloc[-1].to_dict()
        except Exception:
            d -= pd.Timedelta(days=1)
    return None

def fetch_monthly(source, ticker, day):
    # 월말 날짜가 정확히 휴장일이면(전체 달의 ~30%가 주말) 그 달을 통째로 건너뛰어 prices가
    # 10개월 미만으로 남고 SMA가 0이 되는 버그가 있었다 — find_trading_day_price로 이미 해결.
    # 여기서는 "이미 지나간 달"의 데이터는 DB 캐시에서 재사용하고, 아직 진행 중인 이번 달만 새로 조회한다.
    ticker_norm = re.sub(r'\D', '', str(ticker)) or str(ticker)
    dates = pd.date_range(end=pd.Timestamp(day), periods=13, freq='ME')
    cur_month = pd.Timestamp(day).strftime('%Y%m')
    cached = cache_get_prices(ticker_norm)
    cached_by_month = {}
    if not cached.empty:
        tmp = cached.copy(); tmp['month'] = tmp['date'].str[:6]
        cached_by_month = {m: g.sort_values('date').iloc[-1].to_dict() for m, g in tmp.groupby('month')}
    rows = []; new_rows = []
    for d in dates:
        m = d.strftime('%Y%m')
        if m != cur_month and m in cached_by_month:
            rows.append(cached_by_month[m])
        else:
            row = find_trading_day_price(source, ticker, d)
            if row:
                rows.append(row); new_rows.append(row)
    if new_rows: cache_put_prices(ticker_norm, new_rows)
    return pd.DataFrame(rows)

def fetch_daily_recent(source, ticker, day, days=120):
    # 이미 캐시된 날짜는 건너뛰고, 캐시에 없는(주로 지난번 조회 이후 새로 생긴) 거래일만 조회한다.
    # 첫 조회는 예전과 동일하게 느리지만, 두 번째 조회부터는 신규 거래일 수십 개 정도만 불러오면 된다.
    ticker_norm = re.sub(r'\D', '', str(ticker)) or str(ticker)
    end = pd.Timestamp(day); all_dates = [end - pd.Timedelta(days=i) for i in range(days, -1, -1)]
    all_dates = [d for d in all_dates if d.weekday() < 5]
    cached = cache_get_prices(ticker_norm)
    have_dates = set(cached['date']) if not cached.empty else set()
    new_rows = []
    for d in all_dates:
        if d.strftime('%Y%m%d') in have_dates: continue
        try:
            x = fetch_day(source, str(ticker), d.strftime('%Y-%m-%d')); new_rows.append(x.iloc[-1].to_dict())
        except Exception:
            pass
    if new_rows: cache_put_prices(ticker_norm, new_rows)
    start_str = (end - pd.Timedelta(days=days)).strftime('%Y%m%d'); end_str = end.strftime('%Y%m%d')
    combined = pd.concat([cached, pd.DataFrame(new_rows)], ignore_index=True) if new_rows else cached
    if combined.empty: return combined
    combined = combined.drop_duplicates('date').sort_values('date')
    return combined[(combined['date'] >= start_str) & (combined['date'] <= end_str)]

# ---------- Yahoo Finance 가격 어댑터 (미국 상장 종목 + 벤치마크) ----------
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_yahoo_range(symbol, period1, period2, interval='1d'):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
    r = requests.get(url, params={'period1': int(period1), 'period2': int(period2), 'interval': interval}, headers=YAHOO_HEADERS, timeout=20)
    r.raise_for_status(); result = (r.json().get('chart') or {}).get('result')
    if not result: raise RuntimeError(f'{symbol}: 야후 응답 없음')
    result = result[0]; ts = result.get('timestamp') or []
    closes = ((result.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
    rows = [{'ticker': symbol, 'date': pd.Timestamp(t, unit='s').strftime('%Y%m%d'), 'close': float(c)} for t, c in zip(ts, closes) if c is not None]
    return pd.DataFrame(rows)

def fetch_yahoo_day(symbol, day):
    end = pd.Timestamp(day) + pd.Timedelta(days=2); start = end - pd.Timedelta(days=12)
    df = fetch_yahoo_range(symbol, start.timestamp(), end.timestamp(), '1d')
    if df.empty: raise RuntimeError(f'{symbol}: 종가 없음')
    target = pd.Timestamp(day).strftime('%Y%m%d'); before = df[df['date'] <= target]
    return before.iloc[[-1]] if not before.empty else df.iloc[[-1]]

def fetch_yahoo_monthly(symbol, day):
    end = pd.Timestamp(day) + pd.Timedelta(days=2); start = end - pd.Timedelta(days=430)
    return fetch_yahoo_range(symbol, start.timestamp(), end.timestamp(), '1mo')

def fetch_yahoo_daily_recent(symbol, day, days=120):
    end = pd.Timestamp(day) + pd.Timedelta(days=2); start = end - pd.Timedelta(days=days + 10)
    return fetch_yahoo_range(symbol, start.timestamp(), end.timestamp(), '1d')

# ---------- 시장 라우팅 (KR -> KRX/공공데이터, US -> Yahoo) ----------
def fetch_price_day(market, source, ticker, day):
    return fetch_yahoo_day(ticker, day) if market == 'US' else fetch_day(source, ticker, day)

def fetch_price_monthly(market, source, ticker, day):
    return fetch_yahoo_monthly(ticker, day) if market == 'US' else fetch_monthly(source, ticker, day)

def fetch_price_daily_recent(market, source, ticker, day, days=120):
    return fetch_yahoo_daily_recent(ticker, day, days) if market == 'US' else fetch_daily_recent(source, ticker, day, days)

def drawdown_from_peak(closes):
    closes = [c for c in closes if n(c) > 0]
    if not closes: return None
    peak = max(closes); cur = closes[-1]
    if peak <= 0: return None
    return cur / peak - 1

# ---------- calculations ----------
def calc_prices(a):
    prices = safe_prices(a.get('prices', []), [a.get('close', 0)] if n(a.get('close')) else [])
    close = n(a.get('close')) or (prices[-1] if prices else 0)
    # sma/mom을 0으로 반환하면 "0은 falsy"라 'sma and close > sma' 같은 검사가 늘 False가 되어
    # 데이터 부족 상황이 "SMA 하회"로 둔갑하는 버그가 생긴다. 계산 불가 시 None을 반환해 명확히 구분한다.
    sma = sum(prices[-10:]) / 10 if len(prices) >= 10 else None
    mom = (prices[-1] / prices[-13] - 1) if len(prices) >= 13 and prices[-13] else None
    return close, sma, mom

def asset_value(a):
    """현금(CASH) 행은 '보유수량'을 원화 금액 그 자체로 취급한다(종가=1)."""
    shares = n(a.get('shares'))
    if str(a.get('ticker')) == 'CASH': return shares
    return shares * n(a.get('close'))

def portfolio_perf(rows):
    x = sorted([{'date': str(r['date']), 'value': n(r['value'])} for r in rows if n(r.get('value')) > 0], key=lambda z: z['date'])
    if len(x) < 2: return None
    days = max(1, (pd.Timestamp(x[-1]['date']) - pd.Timestamp(x[0]['date'])).days)
    cagr = (x[-1]['value'] / x[0]['value']) ** (365 / days) - 1
    peak = 0; mdd = 0
    for r in x:
        peak = max(peak, r['value']); mdd = min(mdd, r['value'] / peak - 1)
    return cagr, mdd

def calc_xirr(equity, cashflows):
    if not equity or not cashflows: return None
    last = sorted(equity, key=lambda x: x['date'])[-1]
    flows = [{'date': x['date'], 'amount': -n(x['amount'])} for x in cashflows] + [{'date': last['date'], 'amount': n(last['value'])}]
    flows.sort(key=lambda x: x['date']); d0 = pd.Timestamp(flows[0]['date'])
    def f(r): return sum(x['amount'] / (1 + r) ** ((pd.Timestamp(x['date']) - d0).days / 365) for x in flows)
    lo, hi = -0.9999, 10
    for _ in range(120):
        mid = (lo + hi) / 2
        if f(mid) > 0: lo = mid
        else: hi = mid
    return (lo + hi) / 2

def w(x): return f'{n(x):,.0f}원'
def p(x): return f'{x * 100:.2f}%'

# ---------- 전략 레지스트리 ----------
def get_strategies():
    try:
        s = get_state('strategies')
        return s if s else DEFAULT_STRATEGIES
    except Exception:
        return DEFAULT_STRATEGIES

def strategy_codes():
    return [c['code'] for c in get_strategies()]

def compute_portfolio_snapshot(assets_df):
    """전략별 현재 보유금액(자산+현금 행 포함)을 합산한 스냅샷. 현금도 그냥 category='현금'인 자산 행이라 별도 처리 불필요."""
    cfgs = get_strategies(); rows = []; grand = 0.0
    for cfg in cfgs:
        code = cfg['code']; sub = assets_df[assets_df['strategy'].eq(code)]
        total = sub.apply(asset_value, axis=1).sum() if not sub.empty else 0.0
        grand += total
        for _, r in sub.iterrows():
            val = asset_value(r)
            rows.append({
                '전략': code, '계좌': cfg.get('account', code), '티커': r['ticker'], 'ETF': r['name'] or r['ticker'] or '-',
                '분류': r.get('category') or '기타', '현재금액': val,
                '현재비중': (val / total * 100 if total > 0 else 0.0), '목표비중': n(r['target_pct']),
            })
    return grand, pd.DataFrame(rows), cfgs

def compute_category_breakdown(assets_df):
    if assets_df.empty: return pd.DataFrame(columns=['분류', '금액', '비중'])
    tmp = pd.DataFrame({'분류': assets_df['category'].apply(lambda c: c or '기타'), '금액': assets_df.apply(asset_value, axis=1)})
    out = tmp.groupby('분류')['금액'].sum().reset_index().sort_values('금액', ascending=False)
    total = out['금액'].sum()
    out['비중'] = out['금액'] / total * 100 if total > 0 else 0.0
    return out

def save_history_snapshot(assets_df, run_date, plan_text=None):
    """전체 전략의 구성·총액·분류별 총액을 한 번에 히스토리+총자산 시계열에 저장한다."""
    grand, snap_df, _ = compute_portfolio_snapshot(assets_df)
    cat_df = compute_category_breakdown(assets_df)
    ds = run_date.isoformat()
    h = [x for x in get_state('history') if x.get('date') != ds]  # 같은 날짜 재저장 시 덮어쓰기
    h.insert(0, {
        'date': ds,
        'total': grand,
        'by_strategy': snap_df.groupby('전략')['현재금액'].sum().to_dict() if not snap_df.empty else {},
        'by_category': cat_df.set_index('분류')['금액'].to_dict() if not cat_df.empty else {},
        'composition': snap_df.to_dict('records') if not snap_df.empty else [],
        'plan': plan_text,
    })
    put_state('history', h)
    e = [x for x in get_state('equity') if x['date'] != ds]; e.append({'date': ds, 'value': grand})
    put_state('equity', e)
    return grand

def get_category_targets():
    try:
        t = get_state('category_targets')
        return {**{c: 0.0 for c in CATEGORY_OPTIONS}, **t}
    except Exception:
        return {c: 0.0 for c in CATEGORY_OPTIONS}

def last_snapshot_info():
    """가장 최근 저장된 히스토리 날짜와 오늘까지 경과일수."""
    h = get_state('history')
    if not h: return None
    last_date = sorted(h, key=lambda x: x['date'])[-1]['date']
    days = (date.today() - pd.Timestamp(last_date).date()).days
    return last_date, days

def compute_mom_delta():
    """가장 최근 두 번의 히스토리 저장을 비교해 총자산/전략별/분류별 증감을 계산."""
    h = get_state('history')
    if len(h) < 2: return None
    hs = sorted(h, key=lambda x: x['date'])
    cur, prev = hs[-1], hs[-2]
    def diff_rows(a, b):
        keys = sorted(set(a.keys()) | set(b.keys()))
        return [{'항목': k, '이번': n(a.get(k, 0)), '저번': n(b.get(k, 0)), '증감': n(a.get(k, 0)) - n(b.get(k, 0))} for k in keys]
    return {
        'cur_date': cur['date'], 'prev_date': prev['date'],
        'total_cur': n(cur.get('total')), 'total_prev': n(prev.get('total')),
        'total_delta': n(cur.get('total')) - n(prev.get('total')),
        'by_strategy': diff_rows(cur.get('by_strategy') or {}, prev.get('by_strategy') or {}),
        'by_category': diff_rows(cur.get('by_category') or {}, prev.get('by_category') or {}),
    }

def render_diff_table(rows):
    if not rows: return
    df = pd.DataFrame(rows)
    show = df.copy()
    for c in ['이번', '저번', '증감']: show[c] = show[c].map(w)
    st.dataframe(show, use_container_width=True, hide_index=True)

def ensure_cash_rows(assets_df):
    """구버전 DB(계좌별 현금을 별도 kv로 관리하던 시절) 호환: 전략에 CASH 행이 없으면 만들어준다."""
    cfgs = get_strategies()
    try: legacy_cash = get_state('account_cash')
    except Exception: legacy_cash = {}
    changed = False
    for cfg in cfgs:
        code = cfg['code']; sub = assets_df[assets_df['strategy'].eq(code)]
        if not sub.empty and (sub['ticker'] == 'CASH').any(): continue
        remain = max(0.0, 100.0 - pd.to_numeric(sub['target_pct'], errors='coerce').fillna(0).sum()) if not sub.empty else 100.0
        new_row = {'id': str(len(assets_df) + 1), 'strategy': code, 'ticker': 'CASH', 'name': '현금', 'market': 'KR',
                   'role': '대기현금', 'target_pct': remain, 'shares': n(legacy_cash.get(code, 0)), 'close': 1.0,
                   'prices': [], 'signal_ticker': 'CASH', 'category': '현금'}
        assets_df = pd.concat([assets_df, pd.DataFrame([new_row])], ignore_index=True); changed = True
    if changed:
        assets_df = clean_records(assets_df)
        st.session_state.assets = assets_df; put_state('assets', assets_df.to_dict('records'))
    return assets_df

# ---------- app ----------
if 'assets' not in st.session_state: st.session_state.assets = clean_records(pd.DataFrame(get_state('assets')))
assets = ensure_cash_rows(clean_records(st.session_state.assets))

with st.sidebar:
    st.markdown('## 📊 자산배분 도우미')
    page = st.radio('메뉴', ['Action Plan', '포트폴리오 대시보드', '전략 구성', '리밸런싱 히스토리', '성과 비교'])
    st.divider()
    device_mode = st.radio('화면 모드', ['자동(반응형)', '💻 PC', '📱 모바일'], index=0, key='device_mode', horizontal=True)
    st.caption('자동주문 없음 · 지정일 실행만 저장')

# '자동' 모드도 CSS 미디어쿼리로 실제 폰 브라우저 폭에서는 반응형으로 줄어든다.
# '📱 모바일'을 명시적으로 고르면 PC 화면에서도 강제로 모바일 레이아웃(카드형 목록 등)을 미리 볼 수 있다.
MOBILE = device_mode == '📱 모바일'
st.markdown("""
<style>
.block-container{padding-top:1.2rem;padding-bottom:2rem;}
@media (max-width: 640px){
  .block-container{padding:0.6rem 0.7rem 2rem !important;}
  div[data-testid="stMetricValue"]{font-size:1.3rem !important;}
  div[data-testid="stMetricLabel"]{font-size:0.8rem !important;}
  .stButton button{font-size:1rem !important;padding:0.55rem 0.9rem !important;width:100%;}
  div[data-testid="stDataFrame"]{font-size:0.78rem;}
  h1{font-size:1.35rem !important;} h3{font-size:1.05rem !important;} h4{font-size:0.95rem !important;}
}
</style>
""", unsafe_allow_html=True)
if MOBILE:
    st.markdown("""
    <style>
    div[data-testid="stMetricValue"]{font-size:1.3rem !important;}
    .stButton button{font-size:1rem !important;padding:0.55rem 0.9rem !important;width:100%;}
    </style>
    """, unsafe_allow_html=True)

def mobile_card(title, lines, tone=None):
    """모바일 레이아웃에서 넓은 표 대신 쓰는 세로 카드 한 장."""
    border = {'pos': '#4FB286', 'neg': '#D6553F'}.get(tone, '#3A3F47')
    body = '<br>'.join(lines)
    st.markdown(
        f'<div style="border-left:4px solid {border};background:rgba(127,127,127,0.06);'
        f'border-radius:6px;padding:10px 12px;margin-bottom:8px;">'
        f'<div style="font-weight:600;margin-bottom:4px;">{title}</div>'
        f'<div style="font-size:0.88rem;line-height:1.5;">{body}</div></div>',
        unsafe_allow_html=True,
    )

st.title('자산배분 리밸런싱 도우미'); st.caption('한국/미국 상장 종목 · 10개월 SMA · 12개월 모멘텀 · CAGR/MDD/IRR')

if page == 'Action Plan':
    info = last_snapshot_info()
    if info:
        last_date, days = info
        if days >= 25:
            st.warning(f'마지막 히스토리 저장: {last_date} ({days}일 전) — 이번 달 리밸런싱을 아직 안 하신 것 같아요.')
        else:
            st.caption(f'마지막 히스토리 저장: {last_date} ({days}일 전)')
    else:
        st.caption('아직 저장된 히스토리가 없습니다. 이번 리밸런싱 후 아래에서 저장해보세요.')
    codes = strategy_codes(); cfgs = get_strategies()
    c1, c2 = st.columns(2)
    run_date = c1.date_input('리밸런싱 기준일', date.today())
    source = c2.selectbox('국내 종목 가격 소스', ['krx', 'data_go'], format_func=lambda x: 'KRX Open API' if x == 'krx' else '공공데이터포털')
    st.info('종가를 불러온 뒤 저장 버튼을 눌러야 히스토리(모든 전략 구성 스냅샷)가 저장됩니다. 미국 상장 종목은 야후 파이낸스로 자동 조회합니다.')

    if st.button('선택일 종가·13개월 월별 데이터 불러오기', type='primary'):
        ok = 0; errors = []
        for i, a in assets.iterrows():
            t = str(a['ticker']).strip()
            if not t or t == 'CASH': continue
            mkt = a['market'] or 'KR'
            try:
                daydf = fetch_price_day(mkt, source, t, run_date.isoformat()); row = daydf.iloc[-1]; assets.at[i, 'close'] = row['close']
                hist = fetch_price_monthly(mkt, source, t, run_date.isoformat())
                assets.at[i, 'prices'] = hist.sort_values('date')['close'].tolist() if not hist.empty else [row['close']]
                ok += 1
            except Exception as e:
                errors.append(f'{t}: {e}')
        # ISA(-10%)·SSO(-15% 이상) 트리거 판정용 최근 영업일 고점대비 하락률 (월말 데이터만으론 월중 고점을 놓침)
        trigger_dd = {}
        signal_rows = assets[(assets['strategy'].eq('ISA')) | ((assets['strategy'].eq('SSO')) & (assets['role'].eq('S&P500 기준')))]
        for strat, st_ticker, mkt in zip(signal_rows['strategy'], signal_rows['signal_ticker'], signal_rows['market']):
            if not st_ticker or st_ticker == 'CASH': continue
            try:
                d = fetch_price_daily_recent(mkt, source, st_ticker, run_date.isoformat(), 120)
                trigger_dd[strat] = drawdown_from_peak(d.sort_values('date')['close'].tolist()) if not d.empty else None
            except Exception as e:
                errors.append(f'{st_ticker}(트리거): {e}'); trigger_dd[strat] = None
        st.session_state.trigger_dd = trigger_dd
        st.session_state.assets = assets; put_state('assets', assets.to_dict('records'))
        st.success(f'{ok}개 종목 반영')
        if errors: st.warning(' / '.join(errors[:8]))

    trigger_dd = st.session_state.get('trigger_dd', {})
    rows = []
    for i, a in assets.iterrows():
        if a['ticker'] == 'CASH':
            rows.append({'idx': i, '전략': a['strategy'], '티커': 'CASH', 'ETF': '현금', 'role': a['role'], '종가': 1.0,
                         'SMA10': None, 'SMA 위': '—', '12M': None, '현재금액': asset_value(a), '목표%': a['target_pct']})
            continue
        close, sma, mom = calc_prices(a)
        sma_flag = ('YES' if close > sma else 'NO') if sma is not None else '데이터부족'
        rows.append({'idx': i, '전략': a['strategy'], '티커': a['ticker'], 'ETF': a['name'], 'role': a['role'], '종가': close,
                     'SMA10': sma, 'SMA 위': sma_flag, '12M': mom,
                     '현재금액': asset_value(a), '목표%': a['target_pct']})
    vdf = pd.DataFrame(rows)
    if not vdf.empty:
        if MOBILE:
            for strat, g in vdf.groupby('전략'):
                st.markdown(f'##### {strat}')
                for _, r in g.iterrows():
                    if r['티커'] == 'CASH':
                        mobile_card(r['ETF'], [f"보유 {w(r['현재금액'])}"])
                        continue
                    tone = 'pos' if r['SMA 위'] == 'YES' else ('neg' if r['SMA 위'] == 'NO' else None)
                    lines = [f"종가 {r['종가']:,.0f} · SMA10 {r['SMA10']:,.0f}" if r['SMA10'] is not None else f"종가 {r['종가']:,.0f} · SMA10 데이터부족",
                             f"{r['티커']} · SMA {r['SMA 위']} · 12M {p(r['12M']) if r['12M'] is not None else '—'}"]
                    mobile_card(r['ETF'], lines, tone=tone)
        else:
            st.dataframe(vdf.drop(columns=['idx']), use_container_width=True, hide_index=True)

    QUARTER_END = run_date.month in (3, 6, 9, 12)
    plan_rows = []

    # ---- LAA: 나스닥/유로스탁스만 SMA 필터, 필터 이탈분은 현금. 목표비중 복원은 분기말에만 ----
    laa_all = vdf[vdf['전략'] == 'LAA'] if not vdf.empty else vdf
    if not laa_all.empty:
        laa = laa_all[laa_all['티커'] != 'CASH']; cash_row = laa_all[laa_all['티커'] == 'CASH']
        cash_cur = n(cash_row['현재금액'].sum()); total = laa['현재금액'].sum() + cash_cur
        cash_pct = n(cash_row['목표%'].sum())
        for _, r in laa.iterrows():
            filtered = r['티커'] in ('133690', '245350'); breached = filtered and r['SMA 위'] == 'NO'
            if breached: cash_pct += r['목표%']
            if QUARTER_END or breached:
                tgt = 0.0 if breached else total * r['목표%'] / 100
                note = 'SMA 이탈 → 현금화' if breached else ('목표비중 복원(분기말)' if QUARTER_END else '유지')
                plan_rows.append({'전략': 'LAA', '티커': r['티커'], 'ETF': r['ETF'], '현재금액': r['현재금액'], '목표금액': tgt, '매매액(+매수/-매도)': tgt - r['현재금액'], '비고': note})
            else:
                plan_rows.append({'전략': 'LAA', '티커': r['티커'], 'ETF': r['ETF'], '현재금액': r['현재금액'], '목표금액': r['현재금액'], '매매액(+매수/-매도)': 0.0, '비고': '유지(분기중)'})
        cash_tgt = total * cash_pct / 100
        plan_rows.append({'전략': 'LAA', '티커': 'CASH', 'ETF': '현금', '현재금액': cash_cur, '목표금액': cash_tgt, '매매액(+매수/-매도)': cash_tgt - cash_cur, '비고': '필터 이탈 자산 보관'})

    # ---- GSM: SMA 통과 후보 중 12M 1위 80%, 현금 20% (없으면 100% 현금) ----
    gsm_all = vdf[vdf['전략'] == 'GSM'] if not vdf.empty else vdf
    if not gsm_all.empty:
        gsm = gsm_all[gsm_all['티커'] != 'CASH']; cash_row = gsm_all[gsm_all['티커'] == 'CASH']
        cash_cur = n(cash_row['현재금액'].sum()); total = gsm['현재금액'].sum() + cash_cur
        passing = gsm[gsm['SMA 위'] == 'YES'].sort_values('12M', ascending=False)
        winner = passing.iloc[0] if not passing.empty else None
        for _, r in gsm.iterrows():
            is_winner = winner is not None and r['티커'] == winner['티커']
            tgt = total * 0.8 if is_winner else 0.0
            note = '선정(80%)' if is_winner else ('SMA 이탈' if r['SMA 위'] == 'NO' else ('데이터부족' if r['SMA 위'] == '데이터부족' else '미선정(순위 밀림)'))
            plan_rows.append({'전략': 'GSM', '티커': r['티커'], 'ETF': r['ETF'], '현재금액': r['현재금액'], '목표금액': tgt, '매매액(+매수/-매도)': tgt - r['현재금액'], '비고': note})
        cash_tgt = total * (0.2 if winner is not None else 1.0)
        plan_rows.append({'전략': 'GSM', '티커': 'CASH', 'ETF': '현금', '현재금액': cash_cur, '목표금액': cash_tgt, '매매액(+매수/-매도)': cash_tgt - cash_cur, '비고': '전략 대기현금' if winner is not None else '전 후보 SMA 이탈'})

    # ---- ISA: 나스닥100(신호) 고점대비 -10% → 레버리지(418660) 분할매수 ----
    isa_all = vdf[vdf['전략'] == 'ISA'] if not vdf.empty else vdf
    if not isa_all.empty:
        isa = isa_all[isa_all['티커'] != 'CASH']; cash_row = isa_all[isa_all['티커'] == 'CASH']
        if not isa.empty:
            r = isa.iloc[0]; dd = trigger_dd.get('ISA'); triggered = dd is not None and dd <= -0.10
            cash = n(cash_row['현재금액'].sum()); buy = cash / 2 if triggered else 0.0
            note = f'트리거 발동(신호 고점대비 {p(dd)}) → 현금 절반 분할매수' if triggered else f'대기(신호 고점대비 {p(dd) if dd is not None else "데이터 없음"})'
            plan_rows.append({'전략': 'ISA', '티커': r['티커'], 'ETF': r['ETF'], '현재금액': r['현재금액'], '목표금액': r['현재금액'] + buy, '매매액(+매수/-매도)': buy, '비고': note})
            plan_rows.append({'전략': 'ISA', '티커': 'CASH', 'ETF': '현금', '현재금액': cash, '목표금액': cash - buy, '매매액(+매수/-매도)': -buy, '비고': '매수 재원'})

    # ---- SSO: S&P500(360750) 자체 고점대비 -15%↓ → 현금성 자산(153130) 절반을 주식으로 ----
    sso = vdf[vdf['전략'] == 'SSO'] if not vdf.empty else vdf
    if not sso.empty:
        total = sso['현재금액'].sum(); dd = trigger_dd.get('SSO'); triggered = dd is not None and dd <= -0.15
        stock_pct = 85.0 if triggered else 70.0
        for _, r in sso.iterrows():
            is_stock = r['티커'] == '360750'; tgt = total * (stock_pct if is_stock else 100 - stock_pct) / 100
            note = (f'트리거 발동(고점대비 {p(dd)}) → 현금 절반 투입' if triggered else f'평시 유지(고점대비 {p(dd) if dd is not None else "데이터 없음"})') if is_stock else ('트리거 발동 → 현금 축소' if triggered else '평시 유지')
            plan_rows.append({'전략': 'SSO', '티커': r['티커'], 'ETF': r['ETF'], '현재금액': r['현재금액'], '목표금액': tgt, '매매액(+매수/-매도)': tgt - r['현재금액'], '비고': note})

    # ---- EM/금/별도현금: 리밸런싱 대상 아님 ----
    em = vdf[vdf['전략'] == 'EM'] if not vdf.empty else vdf
    if not em.empty:
        for _, r in em.iterrows():
            plan_rows.append({'전략': 'EM', '티커': r['티커'] or '-', 'ETF': r['ETF'], '현재금액': r['현재금액'], '목표금액': r['현재금액'], '매매액(+매수/-매도)': 0.0, '비고': '매매 없음(연 1회만 허용)'})

    # ---- 사용자 추가 전략: 전용 규칙이 없으므로 목표비중(자산+현금 포함) 그대로 복원하는 정적 리밸런싱 적용 ----
    for cfg in cfgs:
        code = cfg['code']
        if code in KNOWN_STRATEGIES or vdf.empty: continue
        sub_all = vdf[vdf['전략'] == code]
        if sub_all.empty: continue
        sub = sub_all[sub_all['티커'] != 'CASH']; cash_row = sub_all[sub_all['티커'] == 'CASH']
        cash = n(cash_row['현재금액'].sum()); total = sub['현재금액'].sum() + cash
        for _, r in sub.iterrows():
            tgt = total * n(r['목표%']) / 100
            plan_rows.append({'전략': code, '티커': r['티커'], 'ETF': r['ETF'], '현재금액': r['현재금액'], '목표금액': tgt, '매매액(+매수/-매도)': tgt - r['현재금액'], '비고': '목표비중 리밸런싱'})
        if not cash_row.empty:
            cash_tgt = total * n(cash_row['목표%'].sum()) / 100
            plan_rows.append({'전략': code, '티커': 'CASH', 'ETF': '현금', '현재금액': cash, '목표금액': cash_tgt, '매매액(+매수/-매도)': cash_tgt - cash, '비고': '현금 목표비중'})

    plan_df = pd.DataFrame(plan_rows)
    st.subheader('이번 달 Action Plan (매수/매도 금액)')
    if plan_df.empty:
        st.warning('종목 데이터를 먼저 불러오세요.')
    else:
        if MOBILE:
            for strat, g in plan_df.groupby('전략'):
                st.markdown(f'##### {strat}')
                for _, r in g.iterrows():
                    amt = r['매매액(+매수/-매도)']
                    tone = 'pos' if amt > 1000 else ('neg' if amt < -1000 else None)
                    action = f"+{w(amt)} 매수" if amt > 1000 else (f"{w(amt)} 매도" if amt < -1000 else '변동 없음')
                    mobile_card(r['ETF'], [f"현재 {w(r['현재금액'])} → 목표 {w(r['목표금액'])}", f"<b>{action}</b>", r['비고']], tone=tone)
        else:
            show = plan_df.copy()
            for c in ['현재금액', '목표금액', '매매액(+매수/-매도)']: show[c] = show[c].map(w)
            st.dataframe(show, use_container_width=True, hide_index=True)
        for strat, g in plan_df.groupby('전략'):
            buys = g[g['매매액(+매수/-매도)'] > 1000]; sells = g[g['매매액(+매수/-매도)'] < -1000]
            parts = []
            if not buys.empty: parts.append('매수: ' + ', '.join(f"{x.ETF} {w(x['매매액(+매수/-매도)'])}" for _, x in buys.iterrows()))
            if not sells.empty: parts.append('매도: ' + ', '.join(f"{x.ETF} {w(-x['매매액(+매수/-매도)'])}" for _, x in sells.iterrows()))
            st.markdown(f"**{strat}** — " + (' · '.join(parts) if parts else '거래 없음'))

    if st.button('Action Plan + 전체 스냅샷을 히스토리에 저장'):
        plan_text = ' | '.join(f"{r['전략']} {r['ETF']}: {w(r['매매액(+매수/-매도)'])} ({r['비고']})" for _, r in plan_df.iterrows() if abs(r['매매액(+매수/-매도)']) > 1000) if not plan_df.empty else ''
        save_history_snapshot(assets, run_date, plan_text=plan_text)
        st.success('저장했습니다.')

elif page == '포트폴리오 대시보드':
    st.subheader('포트폴리오 대시보드'); st.caption('전략(계좌)별 목표비중 대비 현재비중과, 전체 전략의 자산분류별 분포를 한눈에 봅니다 · Snowball72 스타일 참고')
    grand_total, snap_df, cfgs = compute_portfolio_snapshot(assets)
    st.metric('전체 총자산 (모든 전략 합계)', w(grand_total))
    if snap_df.empty:
        st.info('전략과 종목을 먼저 구성하세요.')
    else:
        for cfg in cfgs:
            code = cfg['code']; g = snap_df[snap_df['전략'] == code]
            if g.empty: continue
            strat_total = g['현재금액'].sum(); badge = ' · 동적(모멘텀)' if cfg.get('dynamic') else ''
            st.markdown(f"#### {code} · {cfg.get('account', code)}{badge} — {w(strat_total)}")
            if MOBILE:
                for _, r in g.iterrows():
                    st.caption(f"{r['ETF']} — {w(r['현재금액'])} (목표 {r['목표비중']:.1f}%)")
                    st.progress(min(1.0, max(0.0, r['현재비중'] / 100)), text=f"{r['현재비중']:.1f}%")
            else:
                show = g[['ETF', '현재금액', '현재비중', '목표비중']].copy()
                st.dataframe(show, use_container_width=True, hide_index=True, column_config={
                    '현재금액': st.column_config.NumberColumn('현재금액', format='%d원'),
                    '현재비중': st.column_config.ProgressColumn('현재비중', format='%.1f%%', min_value=0, max_value=100),
                    '목표비중': st.column_config.NumberColumn('목표비중(%)', format='%.1f%%'),
                })
        st.divider(); st.markdown('#### 전략별 비중 (전체 자산 대비)')
        by_strategy = snap_df.groupby('전략')['현재금액'].sum()
        if grand_total > 0: st.bar_chart((by_strategy / grand_total * 100).rename('비중(%)'))

        st.divider(); st.markdown('#### 전체 전략 합산 · 자산분류별 분포')
        cat_df = compute_category_breakdown(assets)
        targets = get_category_targets()
        with st.expander('자산군 목표비중 설정', expanded=False):
            st.caption('전체 포트폴리오 기준 목표비중입니다. 합계가 100%가 아니어도 저장은 되지만, 아래 괴리는 100% 기준으로 계산됩니다.')
            new_targets = {}
            tcols = st.columns(3)
            for i, cat in enumerate(CATEGORY_OPTIONS):
                with tcols[i % 3]:
                    new_targets[cat] = st.number_input(cat, min_value=0.0, max_value=100.0, step=1.0, value=n(targets.get(cat, 0)), key=f'cat_tgt_{cat}')
            tgt_sum = sum(new_targets.values())
            st.caption(f'목표비중 합계: {tgt_sum:.1f}%' + ('' if abs(tgt_sum - 100) < 0.5 else ' — 100%가 되도록 맞춰보세요.'))
            if st.button('자산군 목표비중 저장'):
                put_state('category_targets', new_targets); st.success('저장했습니다.'); st.rerun()
        if not cat_df.empty:
            show = cat_df.copy()
            show['목표비중'] = show['분류'].map(lambda c: n(targets.get(c, 0)))
            show['괴리(%p)'] = show['비중'] - show['목표비중']
            disp_cat = show.copy()
            disp_cat['금액'] = disp_cat['금액'].map(w)
            disp_cat['비중'] = disp_cat['비중'].map(lambda x: f'{x:.1f}%')
            disp_cat['목표비중'] = disp_cat['목표비중'].map(lambda x: f'{x:.1f}%')
            disp_cat['괴리(%p)'] = disp_cat['괴리(%p)'].map(lambda x: f'{x:+.1f}')
            st.dataframe(disp_cat, use_container_width=True, hide_index=True)
            st.bar_chart(cat_df.set_index('분류')['비중'])
            worst = show.reindex(show['괴리(%p)'].abs().sort_values(ascending=False).index).head(3)
            flagged = worst[worst['괴리(%p)'].abs() >= 3]
            if not flagged.empty:
                st.warning('목표비중과 3%p 이상 벌어진 자산군: ' + ', '.join(f"{r['분류']} ({r['괴리(%p)']:+.1f}%p)" for _, r in flagged.iterrows()))

elif page == '전략 구성':
    st.subheader('전략 구성'); st.caption('전략별 계좌 정보와 후보 자산(현금 포함)을 관리합니다.')
    cfgs = get_strategies(); codes = [c['code'] for c in cfgs]

    with st.expander('➕ 새 전략 추가'):
        nc1, nc2 = st.columns(2)
        new_code = nc1.text_input('전략 코드', placeholder='예: CORE2')
        new_account = nc2.text_input('계좌 별명', placeholder='예: 개인연금')
        new_desc = st.text_area('전략 설명', placeholder='이 전략의 규칙을 간단히 적어두세요', height=70)
        new_dynamic = st.checkbox('동적(모멘텀 선택형)', value=False, help='GSM처럼 매달 후보 중 하나만 골라 투자하는 방식이면 체크 — 목표비중 100% 합계 검사를 하지 않습니다.')
        if st.button('전략 추가'):
            code_clean = new_code.strip().upper()
            if not code_clean:
                st.error('전략 코드를 입력하세요.')
            elif code_clean in codes:
                st.error('이미 존재하는 전략 코드입니다.')
            else:
                cfgs.append({'code': code_clean, 'account': new_account.strip() or code_clean, 'description': new_desc.strip(), 'dynamic': new_dynamic})
                put_state('strategies', cfgs)
                assets.loc[len(assets)] = {'id': str(len(assets) + 1), 'strategy': code_clean, 'ticker': 'CASH', 'name': '현금',
                                            'market': 'KR', 'role': '대기현금', 'target_pct': 100.0, 'shares': 0.0, 'close': 1.0,
                                            'prices': [], 'signal_ticker': 'CASH', 'category': '현금'}
                st.session_state.assets = assets; put_state('assets', assets.to_dict('records'))
                st.success(f'{code_clean} 전략을 추가했습니다.'); st.rerun()

    chosen = st.selectbox('전략 선택', codes)
    chosen_cfg = next((c for c in cfgs if c['code'] == chosen), {'code': chosen, 'account': chosen, 'description': '', 'dynamic': False})
    subset = assets[assets['strategy'].eq(chosen)].copy()
    strat_total = subset.apply(asset_value, axis=1).sum() if not subset.empty else 0.0

    cc1, cc2 = st.columns([1, 2])
    with cc1:
        edit_account = st.text_input('계좌 별명', value=chosen_cfg.get('account', chosen), key=f'acct_{chosen}')
        edit_dynamic = st.checkbox('동적(모멘텀 선택형)', value=chosen_cfg.get('dynamic', False), key=f'dyn_{chosen}')
        st.metric('전략 총액 (후보 자산 현재평가액 합)', w(strat_total))
    with cc2:
        edit_desc = st.text_area('전략 설명', value=chosen_cfg.get('description', ''), key=f'desc_{chosen}', height=100)

    disp = pd.DataFrame({
        '티커': subset['ticker'], '상품명': subset['name'], '시장': subset['market'],
        '목표비중': pd.to_numeric(subset['target_pct'], errors='coerce').fillna(0.0),
        '현재비중': subset.apply(lambda r: (asset_value(r) / strat_total * 100 if strat_total > 0 else 0.0), axis=1),
        '보유수량': subset['shares'], '종가': subset['close'],
        '현재평가액': subset.apply(asset_value, axis=1), '분류': subset['category'],
    })
    disp['괴리(%p)'] = disp['현재비중'] - disp['목표비중']
    disp = disp[['티커', '상품명', '시장', '목표비중', '현재비중', '괴리(%p)', '보유수량', '종가', '현재평가액', '분류']]

    st.caption('티커를 직접 수정하면 내부 신호 매핑(ISA/SSO 트리거용)이 끊길 수 있습니다. 종목을 바꾸려면 행을 삭제하고 아래 검색으로 다시 추가하세요. 현금 행은 "보유수량"에 원화 금액을 직접 입력하세요(종가=1).')

    if MOBILE:
        for _, r in disp.iterrows():
            tone = 'pos' if r['괴리(%p)'] > 3 else ('neg' if r['괴리(%p)'] < -3 else None)
            mobile_card(f"{r['상품명']} ({r['시장']})", [
                f"{r['티커']} · {r['분류']}",
                f"현재 {r['현재비중']:.1f}% / 목표 {r['목표비중']:.1f}% (괴리 {r['괴리(%p)']:+.1f}%p)",
                f"평가액 {w(r['현재평가액'])} · 종가 {r['종가']:,.2f} · 보유수량 {r['보유수량']:,.4f}",
            ], tone=tone)
        editor_ctx = st.expander('표로 편집하기 (보유수량·목표비중·분류 수정)', expanded=False)
    else:
        editor_ctx = st.container()

    with editor_ctx:
        edited = st.data_editor(
            disp, num_rows='dynamic', use_container_width=True, hide_index=True,
            disabled=['현재비중', '괴리(%p)', '종가', '현재평가액'],
            column_config={
                '목표비중': st.column_config.NumberColumn('목표비중(%)', min_value=0, max_value=100, step=0.1),
                '현재비중': st.column_config.ProgressColumn('현재비중', format='%.1f%%', min_value=0, max_value=100),
                '괴리(%p)': st.column_config.NumberColumn('괴리(%p)', format='%.1f'),
                '보유수량': st.column_config.NumberColumn('보유수량(현금은 원화금액)', step=0.0001),
                '종가': st.column_config.NumberColumn('종가(API)', step=0.01),
                '현재평가액': st.column_config.NumberColumn('현재평가액', format='%d원'),
                '분류': st.column_config.SelectboxColumn('분류', options=CATEGORY_OPTIONS, required=True),
            },
        )

    asset_sum = pd.to_numeric(edited['목표비중'], errors='coerce').fillna(0.0).sum()
    if edit_dynamic:
        st.caption(f'동적 전략: 목표비중 합계 검사를 하지 않습니다 (현재 합계 {asset_sum:.1f}%).')
        weights_ok = True
    else:
        weights_ok = abs(asset_sum - 100) <= 0.05
        if weights_ok: st.success('목표비중 합계 100% ✓ (현금 행 포함)')
        else: st.error(f'목표비중 합계 {asset_sum:.1f}% — 현금 행을 포함해 정확히 100%가 되어야 저장됩니다.')

    st.markdown('### 종목 검색·추가 (ETF + 개별주식, 한국/미국)')
    mkt_choice = st.radio('시장', ['한국(KRX)', '미국(Yahoo)'], horizontal=True, key='mkt_choice')
    if mkt_choice == '한국(KRX)':
        q = st.text_input('티커 또는 종목명 일부 입력', key='kr_q')
        catalog = load_krx_universe(date.today().isoformat())
        if catalog.empty:
            err = catalog.attrs.get('error', '알 수 없는 이유로 목록을 가져오지 못했습니다.')
            st.warning(f'KRX 종목 목록을 가져오지 못했습니다: {err}\n\nsecrets.toml에 KRX_BASE_URL / KRX_AUTH_KEY가 설정돼 있는지 확인해주세요 (가격 조회에 쓰는 것과 같은 키를 그대로 재사용합니다).')
        filtered = catalog[catalog['ticker'].str.contains(q, case=False, na=False) | catalog['name'].str.contains(q, case=False, na=False)] if q else catalog.head(100)
        filtered = filtered.sort_values(['name', 'ticker'])
        opts = ['선택 안 함'] + [f"{r['name']} · {r['ticker']} ({r['type']})" for _, r in filtered.head(200).iterrows()]
        picked = st.selectbox('검색 결과', opts, key='kr_pick')
        if st.button('선택 종목을 전략에 추가', key='kr_add') and picked != '선택 안 함':
            nm, rest = picked.split(' · ', 1); t = rest.rsplit(' (', 1)[0]
            assets.loc[len(assets)] = {'id': str(len(assets) + 1), 'strategy': chosen, 'ticker': t, 'name': nm, 'market': 'KR',
                                        'role': '사용자 추가', 'target_pct': 0.0, 'shares': 0.0, 'close': 0.0, 'prices': [],
                                        'signal_ticker': t, 'category': '기타'}
            st.session_state.assets = assets; put_state('assets', assets.to_dict('records'))
            st.success(f'{t} {nm} 추가'); st.rerun()
        st.caption(f'KRX 목록 {len(catalog):,}개 (ETF는 이미 설정된 KRX_AUTH_KEY로 조회, 개별주식은 pykrx 보강 시도)')
    else:
        q = st.text_input('종목명 또는 티커 입력 (예: Apple, AAPL)', key='us_q')
        if st.button('검색', key='us_search') and q:
            st.session_state.us_results = search_us_symbols(q)
        results = st.session_state.get('us_results', pd.DataFrame(columns=['ticker', 'name', 'exchange']))
        if not results.empty:
            results = results.sort_values(['name', 'ticker'])
            opts = ['선택 안 함'] + [f"{r['name']} · {r['ticker']} ({r['exchange']})" for _, r in results.iterrows()]
            picked = st.selectbox('검색 결과', opts, key='us_pick')
            if st.button('선택 종목을 전략에 추가', key='us_add') and picked != '선택 안 함':
                nm, rest = picked.split(' · ', 1); t = rest.rsplit(' (', 1)[0]
                assets.loc[len(assets)] = {'id': str(len(assets) + 1), 'strategy': chosen, 'ticker': t, 'name': nm, 'market': 'US',
                                            'role': '사용자 추가', 'target_pct': 0.0, 'shares': 0.0, 'close': 0.0, 'prices': [],
                                            'signal_ticker': t, 'category': '기타'}
                st.session_state.assets = assets; put_state('assets', assets.to_dict('records'))
                st.success(f'{t} {nm} 추가'); st.rerun()
        st.caption('야후 파이낸스 검색 API 사용')

    if st.button('선택 전략 저장', type='primary'):
        if not weights_ok:
            st.error('목표비중 합계를 100%로 맞춘 뒤 저장하세요.')
        else:
            rebuilt = edited.rename(columns={'티커': 'ticker', '상품명': 'name', '시장': 'market', '목표비중': 'target_pct', '보유수량': 'shares', '종가': 'close', '분류': 'category'})
            rebuilt = rebuilt[['ticker', 'name', 'market', 'target_pct', 'shares', 'close', 'category']].copy()
            rebuilt = rebuilt[~((rebuilt['ticker'].fillna('') == '') & (rebuilt['name'].fillna('') == ''))]
            rebuilt['strategy'] = chosen
            old_meta = subset.drop_duplicates('ticker').set_index('ticker')[['role', 'prices', 'signal_ticker']]
            def carry(row):
                if row['ticker'] in old_meta.index:
                    m = old_meta.loc[row['ticker']]
                    return pd.Series({'role': m['role'], 'prices': m['prices'], 'signal_ticker': m['signal_ticker'] or row['ticker']})
                return pd.Series({'role': '사용자 추가', 'prices': [], 'signal_ticker': row['ticker']})
            meta = rebuilt.apply(carry, axis=1)
            rebuilt = pd.concat([rebuilt.reset_index(drop=True), meta.reset_index(drop=True)], axis=1)
            rebuilt['id'] = [str(i) for i in range(len(rebuilt))]
            assets2 = assets[~assets['strategy'].eq(chosen)].copy()
            assets2 = pd.concat([assets2, clean_records(rebuilt)], ignore_index=True)
            st.session_state.assets = assets2; put_state('assets', assets2.to_dict('records'))
            new_cfg = {'code': chosen, 'account': edit_account.strip() or chosen, 'description': edit_desc.strip(), 'dynamic': edit_dynamic}
            new_cfgs = [new_cfg if c['code'] == chosen else c for c in cfgs]
            if chosen not in [c['code'] for c in cfgs]: new_cfgs.append(new_cfg)
            put_state('strategies', new_cfgs)
            st.success('저장했습니다.'); st.rerun()

    st.divider(); st.markdown('### 히스토리 저장')
    st.caption('현재 모든 전략의 구성·비중·분류를 한 번에 히스토리와 총자산 시계열에 저장합니다.')
    hist_date = st.date_input('저장할 날짜', date.today(), key='hist_save_date')
    if st.button('오늘 날짜로 전체 스냅샷을 히스토리에 저장', type='primary', key='save_snapshot_btn'):
        total_saved = save_history_snapshot(assets, hist_date)
        st.success(f'히스토리에 저장했습니다. (총자산 {w(total_saved)})')
        mom = compute_mom_delta()
        if mom:
            arrow = '▲' if mom['total_delta'] >= 0 else '▼'
            st.info(f"직전 저장({mom['prev_date']}) 대비 총자산 {arrow} {w(abs(mom['total_delta']))} ({mom['prev_date']} → {mom['cur_date']})")

elif page == '리밸런싱 히스토리':
    st.subheader('리밸런싱 히스토리')
    h = get_state('history')
    if not h:
        st.info('아직 저장된 히스토리가 없습니다. 전략 구성 페이지 하단 또는 Action Plan 페이지에서 저장하세요.')
    else:
        hdf = pd.DataFrame(h).sort_values('date')
        tab1, tab2, tab3, tab4 = st.tabs(['전체', '전략별 총액', '분류별 총액', '세부 내역'])
        with tab1:
            mom = compute_mom_delta()
            if mom:
                st.markdown(f"#### 직전 저장 대비 변화 ({mom['prev_date']} → {mom['cur_date']})")
                arrow = '▲' if mom['total_delta'] >= 0 else '▼'
                st.metric('총자산', w(mom['total_cur']), delta=f"{arrow} {w(abs(mom['total_delta']))}")
                dc1, dc2 = st.columns(2)
                with dc1: st.caption('전략별 증감'); render_diff_table(mom['by_strategy'])
                with dc2: st.caption('분류별 증감'); render_diff_table(mom['by_category'])
                st.divider()
            chart_df = hdf[['date', 'total']].assign(date=lambda x: pd.to_datetime(x.date)).set_index('date')
            st.line_chart(chart_df['total'])
            show = hdf[['date', 'total', 'plan']].copy(); show['total'] = show['total'].map(w)
            st.dataframe(show.rename(columns={'date': '날짜', 'total': '총자산', 'plan': '액션플랜 메모'}), use_container_width=True, hide_index=True)
        with tab2:
            by_strat = pd.DataFrame([{**{'date': r['date']}, **(r.get('by_strategy') or {})} for _, r in hdf.iterrows()])
            if not by_strat.empty:
                by_strat = by_strat.set_index('date'); by_strat.index = pd.to_datetime(by_strat.index)
                st.line_chart(by_strat.fillna(0))
                show = by_strat.reset_index().rename(columns={'date': '날짜'})
                for c in show.columns:
                    if c != '날짜': show[c] = show[c].map(lambda x: w(x) if pd.notna(x) else '—')
                if MOBILE:
                    with st.expander('전략별 총액 표로 보기'): st.dataframe(show, use_container_width=True, hide_index=True)
                else:
                    st.dataframe(show, use_container_width=True, hide_index=True)
        with tab3:
            by_cat = pd.DataFrame([{**{'date': r['date']}, **(r.get('by_category') or {})} for _, r in hdf.iterrows()])
            if not by_cat.empty:
                by_cat = by_cat.set_index('date'); by_cat.index = pd.to_datetime(by_cat.index)
                st.line_chart(by_cat.fillna(0))
                show = by_cat.reset_index().rename(columns={'date': '날짜'})
                for c in show.columns:
                    if c != '날짜': show[c] = show[c].map(lambda x: w(x) if pd.notna(x) else '—')
                if MOBILE:
                    with st.expander('분류별 총액 표로 보기'): st.dataframe(show, use_container_width=True, hide_index=True)
                else:
                    st.dataframe(show, use_container_width=True, hide_index=True)
        with tab4:
            options = [r['date'] for r in h]
            pick = st.selectbox('세부 내역을 볼 날짜', options)
            rec = next((r for r in h if r['date'] == pick), None)
            if rec:
                comp = rec.get('composition') or []
                if comp:
                    cdf = pd.DataFrame(comp)
                    if MOBILE:
                        for strat, g in cdf.groupby('전략'):
                            st.markdown(f'##### {strat}')
                            for _, r in g.iterrows():
                                mobile_card(r['ETF'], [f"{r['티커']} · {r['분류']}", f"{w(r['현재금액'])} ({r['현재비중']:.1f}% / 목표 {r['목표비중']:.1f}%)"])
                    else:
                        show = cdf.copy(); show['현재금액'] = show['현재금액'].map(w)
                        show['현재비중'] = show['현재비중'].map(lambda x: f'{x:.1f}%'); show['목표비중'] = show['목표비중'].map(lambda x: f'{x:.1f}%')
                        st.dataframe(show, use_container_width=True, hide_index=True)
                else:
                    st.info('이 기록은 구성 스냅샷이 없습니다(이전 버전 저장분).')
                if rec.get('plan'): st.markdown('**저장 시점 Action Plan 메모**'); st.write(rec['plan'])
    st.divider()
    st.markdown('### 백업 · 복원')
    st.caption(f'현재 DB 파일 위치: `{DB_PATH}` — app.py를 다른 폴더로 옮겨도 이 경로는 바뀌지 않습니다.')
    bc1, bc2 = st.columns(2)
    with bc1:
        st.download_button('JSON 백업 다운로드', json.dumps({k: get_state(k) for k in ['assets', 'history', 'equity', 'cashflows', 'benchmarks', 'strategies']}, ensure_ascii=False, indent=2), file_name='portfolio-backup.json', mime='application/json')
        if h:
            st.download_button('CSV 히스토리(요약)', pd.DataFrame(h).drop(columns=['composition', 'by_strategy', 'by_category'], errors='ignore').to_csv(index=False), file_name='rebalance-history.csv', mime='text/csv')
    with bc2:
        up = st.file_uploader('JSON 백업 파일로 복원', type=['json'], key='restore_upload')
        if up is not None:
            st.warning('복원하면 현재 저장된 데이터를 덮어씁니다.')
            if st.button('이 백업으로 복원', type='primary', key='restore_btn'):
                try:
                    data = json.loads(up.getvalue().decode('utf-8'))
                    for k in ['assets', 'history', 'equity', 'cashflows', 'benchmarks', 'strategies']:
                        if k in data: put_state(k, data[k])
                    st.session_state.pop('assets', None)
                    st.success('복원했습니다.'); st.rerun()
                except Exception as e:
                    st.error(f'복원 실패: {e}')

    st.divider()
    st.markdown('### 가격 캐시')
    init_db(); _con = sqlite3.connect(DB_PATH)
    _n_cached = _con.execute('SELECT COUNT(*) FROM price_cache').fetchone()[0]
    _n_tickers = _con.execute('SELECT COUNT(DISTINCT ticker) FROM price_cache').fetchone()[0]
    _con.close()
    st.caption(f'캐시된 가격 데이터: 종목 {_n_tickers}개 · {_n_cached:,}개 날짜. 지나간 달/과거 거래일은 캐시에서 재사용하고, 새로 생긴 날짜만 조회합니다.')
    if st.button('가격 캐시 전체 삭제(다음 조회부터 처음부터 다시 받음)'):
        cache_clear_prices(); st.success('캐시를 삭제했습니다.')

else:  # 성과 비교
    st.subheader('성과 비교')
    e = get_state('equity'); cf = get_state('cashflows')
    if not e:
        st.info('아직 총자산 히스토리가 없습니다. 전략 구성 페이지 하단에서 스냅샷을 저장하면 여기 반영됩니다.')
    m = portfolio_perf(e); irr = calc_xirr(e, cf)
    a, b, c = st.columns(3)
    a.metric('CAGR', p(m[0]) if m else '—'); b.metric('MDD', p(m[1]) if m else '—'); c.metric('IRR/XIRR', p(irr) if irr is not None else '—')
    if e: st.line_chart(pd.DataFrame(e).assign(date=lambda x: pd.to_datetime(x.date)).set_index('date')['value'])

    st.divider(); st.subheader('벤치마크 (자동 조회)'); st.caption('QQQ·SPY·KOSPI200 종가를 야후 파이낸스에서 직접 불러옵니다.')
    if st.button('히스토리 날짜 기준 벤치마크 자동 채우기', type='primary'):
        existing = get_state('benchmarks'); dates = sorted({x['date'] for x in e}); added = 0; errs = []
        BENCH_SYMBOLS = {'QQQ': 'QQQ', 'SPY': 'SPY', 'KOSPI200': '^KS200'}
        for dt in dates:
            for name, sym in BENCH_SYMBOLS.items():
                if any(x['name'] == name and x['date'] == dt for x in existing): continue
                try:
                    row = fetch_yahoo_day(sym, dt); existing.append({'name': name, 'date': dt, 'value': float(row.iloc[-1]['close'])}); added += 1
                except Exception as ex:
                    errs.append(f'{name} {dt}: {ex}')
        put_state('benchmarks', existing); st.success(f'{added}개 벤치마크 값을 채웠습니다.')
        if errs: st.warning(' / '.join(errs[:5]))
        st.rerun()

    series = {}
    if e:
        first = sorted(e, key=lambda x: x['date'])[0]['date']; base = sorted(e, key=lambda x: x['date'])[0]['value']
        if base > 0:
            series['내 포트폴리오'] = [{'date': x['date'], 'value': x['value'] / base * 100} for x in e]
            for name in ['QQQ', 'SPY', 'KOSPI200']:
                z = sorted([x for x in get_state('benchmarks') if x['name'] == name and x['date'] >= first], key=lambda x: x['date'])
                if z: series[name] = [{'date': x['date'], 'value': x['value'] / z[0]['value'] * 100} for x in z]
        else:
            st.caption('첫 저장된 총자산이 0원이라 벤치마크 대비 비교는 아직 계산할 수 없습니다. 보유수량·종가를 입력한 뒤 다시 저장해보세요.')
    if series:
        chart = pd.concat([pd.DataFrame(v).assign(date=lambda x: pd.to_datetime(x.date)).set_index('date').rename(columns={'value': k}) for k, v in series.items()], axis=1).sort_index()
        st.line_chart(chart)
        for name, vals in series.items():
            mm = portfolio_perf(vals); st.write(f'**{name}** — CAGR {p(mm[0]) if mm else "—"} · MDD {p(mm[1]) if mm else "—"}')

    st.divider(); st.subheader('입출금 원장')
    cd = st.date_input('거래일', date.today(), key='cd'); ca = st.number_input('금액(입금 + / 출금 -)', step=100000.0, key='ca'); cm = st.text_input('메모', key='cm')
    if st.button('입출금 저장'):
        x = get_state('cashflows'); x.append({'date': cd.isoformat(), 'amount': ca, 'memo': cm}); put_state('cashflows', x); st.success('저장했습니다.')
    st.dataframe(pd.DataFrame(get_state('cashflows')), use_container_width=True, hide_index=True)
