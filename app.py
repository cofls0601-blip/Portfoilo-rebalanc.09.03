import json, sqlite3
from datetime import date
from pathlib import Path
import math
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='자산배분 리밸런싱 도우미', page_icon='📊', layout='wide')
ROOT = Path(__file__).parent
DB_PATH = st.secrets.get('SQLITE_PATH', str(ROOT / 'portfolio.db'))

# ---------- Safe data normalization ----------
def safe_prices(value, fallback=None):
    """Normalize list/string/NaN/scalar into a clean numeric list."""
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
    if isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = [value]
    out=[]
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
    df=df.copy()
    for c in ['target_pct','shares','current_amount','close']:
        if c not in df: df[c]=0.0
        df[c]=pd.to_numeric(df[c],errors='coerce').fillna(0.0)
    if 'prices' not in df: df['prices']=[[] for _ in range(len(df))]
    df['prices']=df['prices'].apply(safe_prices)
    for c in ['strategy','account','ticker','name','market','role','signal_ticker']:
        if c not in df: df[c]=''
        df[c]=df[c].fillna('').astype(str)
    df['signal_ticker']=df.apply(lambda r: r['signal_ticker'] or r['ticker'], axis=1)
    return df

DEFAULT_ROWS=[
 ('LAA','과세 연금저축','133690','TIGER 미국나스닥100','NASDAQ',12.5),('LAA','과세 연금저축','245350','TIGER 유로스탁스배당30','EuroStoxx',12.5),('LAA','과세 연금저축','360750','TIGER 미국S&P500','S&P500',12.5),('LAA','과세 연금저축','251350','KODEX 선진국MSCI World','MSCI World',15.5),('LAA','과세 연금저축','132030','KODEX 골드선물(H)','Gold',25),('LAA','과세 연금저축','148070','KIWOOM 국고채10년','Bond',25),
 ('GSM','비과세 연금저축','360750','TIGER 미국S&P500','GSM 후보',0),('GSM','비과세 연금저축','251350','KODEX 선진국MSCI World','GSM 후보',0),('GSM','비과세 연금저축','133690','TIGER 미국나스닥100','GSM 후보',0),('GSM','비과세 연금저축','245350','TIGER 유로스탁스배당30','GSM 후보',0),('ISA','ISA','418660','TIGER 미국나스닥100레버리지(합성)','-10% 트리거',0),('SSO','일반계좌 2','360750','TIGER 미국S&P500','S&P500 기준',70),('SSO','일반계좌 2','153130','KODEX 단기채권','현금',30),
 ('EM','일반계좌 1','069500','KODEX 200','한국',25),('EM','일반계좌 1','','중국 ETF 입력','중국',25),('EM','일반계좌 1','','인도 ETF 입력','인도',25),('EM','일반계좌 1','','베트남 ETF 입력','베트남',25)]
# ISA는 실제로 레버리지 상품(418660)을 매매하지만, 트리거 판단은 원지수 성격의 나스닥100(133690) 고점대비 하락률로 해야
# 레버리지 자체의 변동성에 낚이지 않는다. signal_ticker가 신호 판단용 티커, ticker는 실제 매매 티커.
SIGNAL_TICKER_OVERRIDE = {'418660': '133690'}
def default_assets():
    rows=[{'id':str(i),'strategy':a,'account':b,'ticker':c,'name':d,'market':'KR','role':e,'target_pct':f,
           'shares':0.0,'current_amount':0.0,'close':0.0,'prices':[],
           'signal_ticker':SIGNAL_TICKER_OVERRIDE.get(c,c)} for i,(a,b,c,d,e,f) in enumerate(DEFAULT_ROWS)]
    return pd.DataFrame(rows)

DEFAULT_STRATEGIES = [
    {'code': 'LAA', 'account': '과세 연금저축', 'dynamic': False, 'cash_pct': 0.0},
    {'code': 'GSM', 'account': '비과세 연금저축', 'dynamic': True, 'cash_pct': 20.0},
    {'code': 'ISA', 'account': 'ISA', 'dynamic': False, 'cash_pct': 100.0},
    {'code': 'SSO', 'account': '일반계좌 2', 'dynamic': False, 'cash_pct': 0.0},
    {'code': 'EM', 'account': '일반계좌 1', 'dynamic': False, 'cash_pct': 0.0},
]
KNOWN_STRATEGIES = {'LAA', 'GSM', 'ISA', 'SSO', 'EM'}  # 전용 리밸런싱 규칙이 있는 전략(하드코딩된 룰)

# ---------- SQLite persistence ----------
def init_db():
    con=sqlite3.connect(DB_PATH);con.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT NOT NULL)')
    for k,v in [('assets',default_assets().to_json(orient='records',force_ascii=False)),('history','[]'),('equity','[]'),('cashflows','[]'),('benchmarks','[]'),('account_cash',json.dumps({'LAA':0.0,'GSM':0.0,'ISA':0.0})),('strategies',json.dumps(DEFAULT_STRATEGIES,ensure_ascii=False))]: con.execute('INSERT OR IGNORE INTO kv(k,v) VALUES(?,?)',(k,v))
    con.commit();con.close()
def get_state(k):
    init_db();con=sqlite3.connect(DB_PATH);r=con.execute('SELECT v FROM kv WHERE k=?',(k,)).fetchone();con.close();return json.loads(r[0])
def put_state(k,v):
    init_db();con=sqlite3.connect(DB_PATH);con.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)',(k,json.dumps(v,ensure_ascii=False,default=str)));con.commit();con.close()

# ---------- KRX ETF catalog: runtime lookup + bundled fallback ----------
@st.cache_data(ttl=86400, show_spinner=False)
def load_krx_etfs(asof):
    try:
        from pykrx import stock
        tickers=stock.get_etf_ticker_list(asof.replace('-',''))
        rows=[{'ticker':str(t),'name':str(stock.get_etf_ticker_name(t)),'market':'KR'} for t in tickers]
        if rows: return pd.DataFrame(rows)
    except Exception: pass
    p=ROOT/'krx_etf_fallback.csv'
    if p.exists(): return pd.read_csv(p,dtype=str).fillna('')
    return pd.DataFrame(columns=['ticker','name','market'])

# ---------- KRX / data.go price adapters ----------
def secret(name):
    try:return st.secrets[name]
    except Exception:return ''
def normalize_payload(payload, requested=''):
    if isinstance(payload,dict):
        rows=payload.get('data',payload.get('OutBlock_1',payload.get('response',{}).get('body',{}).get('items',{}).get('item',payload)))
    else: rows=payload
    if isinstance(rows,dict): rows=[rows]
    out=[]
    for x in rows or []:
        if not isinstance(x,dict):continue
        ticker=str(x.get('symbol',x.get('ISU_SRT_CD',x.get('isu_srt_cd',x.get('ticker',requested))))).replace('.KS','').strip()
        d=str(x.get('date',x.get('basDd',x.get('stck_bsop_date','')))).replace('-','')
        close=x.get('close',x.get('TDD_CLSPRC',x.get('stck_clpr',x.get('price'))))
        if close is None:continue
        try: out.append({'ticker':ticker,'date':d,'close':float(str(close).replace(',',''))})
        except (ValueError,TypeError):pass
    return pd.DataFrame(out)
def fetch_day(source,ticker,day):
    if source=='krx':
        url,key=secret('KRX_BASE_URL'),secret('KRX_AUTH_KEY')
        if not url or not key:raise RuntimeError('KRX_BASE_URL/KRX_AUTH_KEY 미설정')
        r=requests.get(url,headers={'AUTH_KEY':key},params={'basDd':day.replace('-','')},timeout=30)
    else:
        url,key=secret('DATA_GO_URL'),secret('DATA_GO_SERVICE_KEY')
        if not url or not key:raise RuntimeError('DATA_GO_URL/DATA_GO_SERVICE_KEY 미설정')
        r=requests.get(url,params={'serviceKey':key,'resultType':'json','numOfRows':1000,'pageNo':1,'basDt':day.replace('-',''),'itmsNm':ticker},timeout=30)
    r.raise_for_status();df=normalize_payload(r.json(),ticker)
    if df.empty:raise RuntimeError(f'{ticker}: 종가 없음')
    hit=df[df['ticker'].eq(str(ticker))]
    return hit if not hit.empty else df
@st.cache_data(ttl=3600,show_spinner=False)
def fetch_monthly(source,ticker,day):
    # One request per month-end; cached to avoid duplicate KRX calls.
    dates=pd.date_range(end=pd.Timestamp(day),periods=13,freq='ME'); rows=[]
    for d in dates:
        try:
            x=fetch_day(source,str(ticker),d.strftime('%Y-%m-%d'));rows.append(x.iloc[-1].to_dict())
        except Exception: pass
    return pd.DataFrame(rows)

@st.cache_data(ttl=3600,show_spinner=False)
def fetch_daily_recent(source,ticker,day,days=120):
    # 고점대비 하락률(ISA -10%, SSO -15~-20% 트리거) 계산용 최근 영업일 종가 시리즈.
    # 월말 데이터(fetch_monthly)만으로는 월중 고점을 놓치므로 별도로 조회한다.
    end=pd.Timestamp(day);dates=[end-pd.Timedelta(days=i) for i in range(days,-1,-1)]
    dates=[d for d in dates if d.weekday()<5];rows=[]
    for d in dates:
        try:
            x=fetch_day(source,str(ticker),d.strftime('%Y-%m-%d'));rows.append(x.iloc[-1].to_dict())
        except Exception: pass
    return pd.DataFrame(rows)

def drawdown_from_peak(closes):
    closes=[c for c in closes if n(c)>0]
    if not closes:return None
    peak=max(closes);cur=closes[-1]
    if peak<=0:return None
    return cur/peak-1

# ---------- calculations ----------
def calc_prices(a):
    p=safe_prices(a.get('prices',[]),[a.get('close',0)] if n(a.get('close')) else [])
    close=n(a.get('close')) or (p[-1] if p else 0);sma=sum(p[-10:])/10 if len(p)>=10 else 0;mom=p[-1]/p[-13]-1 if len(p)>=13 and p[-13] else 0
    return close,sma,mom

def asset_value(a):return n(a.get('current_amount')) if n(a.get('current_amount'))>0 else n(a.get('shares'))*n(a.get('close'))
def portfolio_perf(rows):
    x=sorted([{'date':str(r['date']),'value':n(r['value'])} for r in rows if n(r.get('value'))>0],key=lambda z:z['date'])
    if len(x)<2:return None
    days=max(1,(pd.Timestamp(x[-1]['date'])-pd.Timestamp(x[0]['date'])).days);cagr=(x[-1]['value']/x[0]['value'])**(365/days)-1;peak=0;mdd=0
    for r in x:peak=max(peak,r['value']);mdd=min(mdd,r['value']/peak-1)
    return cagr,mdd

def calc_xirr(equity,cashflows):
    if not equity or not cashflows:return None
    last=sorted(equity,key=lambda x:x['date'])[-1]; flows=[{'date':x['date'],'amount':-n(x['amount'])} for x in cashflows]+[{'date':last['date'],'amount':n(last['value'])}];flows.sort(key=lambda x:x['date'])
    d0=pd.Timestamp(flows[0]['date'])
    def f(r):return sum(x['amount']/(1+r)**((pd.Timestamp(x['date'])-d0).days/365) for x in flows)
    lo,hi=-.9999,10
    for _ in range(120):
        mid=(lo+hi)/2
        if f(mid)>0:lo=mid
        else:hi=mid
    return (lo+hi)/2

def w(x):return f'{n(x):,.0f}원'
def p(x):return f'{x*100:.2f}%'

# ---------- 전략 레지스트리 ----------
def get_strategies():
    try:
        s=get_state('strategies')
        return s if s else DEFAULT_STRATEGIES
    except Exception:
        return DEFAULT_STRATEGIES

def strategy_codes():
    return [c['code'] for c in get_strategies()]

def validate_strategy_weights(rows_df, cfg):
    """자산 목표비중 합 + 현금비중 = 100%인지 검사. dynamic 전략(GSM 등)은 검사하지 않음."""
    if cfg.get('dynamic'):
        return True, None
    asset_sum = pd.to_numeric(rows_df['target_pct'], errors='coerce').fillna(0.0).sum()
    total = asset_sum + n(cfg.get('cash_pct', 0))
    if abs(total - 100) > 0.05:
        return False, f"자산 목표비중 합({asset_sum:.1f}%) + 현금비중({n(cfg.get('cash_pct',0)):.1f}%) = {total:.1f}% — 100%가 되어야 저장할 수 있습니다."
    return True, None

def compute_portfolio_snapshot(assets_df):
    """전략별 현재 보유금액(자산+현금)을 합산해 총자산 스냅샷을 만든다. 수동 총자산 입력을 대체."""
    cfgs=get_strategies();account_cash=get_state('account_cash');rows=[];grand=0.0
    for cfg in cfgs:
        code=cfg['code'];sub=assets_df[assets_df['strategy'].eq(code)]
        cash=n(account_cash.get(code,0))
        asset_sum=sub.apply(asset_value,axis=1).sum() if not sub.empty else 0.0
        total=asset_sum+cash;grand+=total
        for _,r in sub.iterrows():
            val=asset_value(r)
            rows.append({'전략':code,'계좌':cfg.get('account',code),'ETF':r['name'] or r['ticker'] or '-','현재금액':val,
                         '현재비중':(val/total*100 if total>0 else 0.0),'목표비중':n(r['target_pct'])})
        rows.append({'전략':code,'계좌':cfg.get('account',code),'ETF':'현금','현재금액':cash,
                     '현재비중':(cash/total*100 if total>0 else 0.0),'목표비중':n(cfg.get('cash_pct',0))})
    return grand, pd.DataFrame(rows), cfgs

# ---------- app ----------
if 'assets' not in st.session_state:st.session_state.assets=clean_records(pd.DataFrame(get_state('assets')))
assets=clean_records(st.session_state.assets)
with st.sidebar:
    st.markdown('## 📊 자산배분 도우미');page=st.radio('메뉴',['Action Plan','포트폴리오 대시보드','전략 구성','성과 비교','리밸런싱 히스토리']);st.caption('자동주문 없음 · 지정일 실행만 저장')
st.title('자산배분 리밸런싱 도우미');st.caption('한국 상장 ETF · 10개월 SMA · 12개월 모멘텀 · CAGR/MDD/IRR')

if page=='Action Plan':
    codes=strategy_codes();cfgs=get_strategies()
    c1,c2,c3=st.columns(3);run_date=c1.date_input('리밸런싱 기준일',date.today());source=c2.selectbox('가격 소스',['krx','data_go'],format_func=lambda x:'KRX Open API' if x=='krx' else '공공데이터포털');strategy=c3.selectbox('저장 전략',['ALL']+codes)
    st.info('종가를 불러온 뒤 저장 버튼을 눌렀을 때만 선택일 기준 Action Plan과 히스토리가 생성됩니다.')

    try: account_cash=get_state('account_cash')
    except Exception: account_cash={}
    with st.expander('전략별 보유 현금 입력 (매수/매도 금액 계산에 사용)',expanded=False):
        st.caption('이미 계좌 내 단기채권 등 별도 ETF로 현금성 자산을 잡아둔 전략(SSO 등)은 0으로 두세요.')
        new_cash={}
        cols=st.columns(min(3,len(cfgs)) or 1)
        for i,cfg in enumerate(cfgs):
            code=cfg['code']
            with cols[i%len(cols)]:
                new_cash[code]=st.number_input(f'{code} 현금(원)',min_value=0.0,step=10000.0,value=n(account_cash.get(code,0)),key=f'cash_{code}')
        if st.button('현금 잔액 저장'):
            put_state('account_cash',new_cash);st.success('저장했습니다.');st.rerun()
    account_cash={**account_cash,**new_cash}

    if st.button('선택일 종가·13개월 월말 데이터 불러오기',type='primary'):
        ok=0;errors=[]
        for i,a in assets.iterrows():
            t=str(a['ticker']).strip()
            if not t:continue
            try:
                daydf=fetch_day(source,t,run_date.isoformat());exact=daydf[daydf['date'].eq(run_date.strftime('%Y%m%d'))];row=exact.iloc[-1] if not exact.empty else daydf.iloc[-1];assets.at[i,'close']=row['close']
                hist=fetch_monthly(source,t,run_date.isoformat());assets.at[i,'prices']=hist.sort_values('date')['close'].tolist() if not hist.empty else [row['close']];ok+=1
            except Exception as e:errors.append(f'{t}: {e}')
        # ISA(-10%)·SSO(-15~-20%) 트리거 판정용 최근 영업일 고점대비 하락률 (월말 데이터만으론 월중 고점을 놓침)
        trigger_dd={}
        signal_rows=assets[(assets['strategy'].eq('ISA'))|((assets['strategy'].eq('SSO'))&(assets['role'].eq('S&P500 기준')))]
        for strat,st_ticker in zip(signal_rows['strategy'],signal_rows['signal_ticker']):
            if not st_ticker:continue
            try:
                d=fetch_daily_recent(source,st_ticker,run_date.isoformat(),120)
                trigger_dd[strat]=drawdown_from_peak(d.sort_values('date')['close'].tolist()) if not d.empty else None
            except Exception as e:
                errors.append(f'{st_ticker}(트리거): {e}');trigger_dd[strat]=None
        st.session_state.trigger_dd=trigger_dd
        st.session_state.assets=assets;put_state('assets',assets.to_dict('records'));st.success(f'{ok}개 종목 반영')
        if errors:st.warning(' / '.join(errors[:5]))

    trigger_dd=st.session_state.get('trigger_dd',{})
    rows=[]
    for i,a in assets.iterrows():
        close,sma,mom=calc_prices(a);rows.append({'idx':i,'전략':a['strategy'],'티커':a['ticker'],'ETF':a['name'],'role':a['role'],'종가':close,'SMA10':sma,'SMA 위':'YES' if sma and close>sma else 'NO','12M':mom,'현재금액':asset_value(a),'목표%':a['target_pct']})
    vdf=pd.DataFrame(rows);st.dataframe(vdf.drop(columns=['idx']),use_container_width=True,hide_index=True)

    QUARTER_END=run_date.month in (3,6,9,12)
    plan_rows=[]

    # ---- LAA: 나스닥/유로스탁스만 SMA 필터, 필터 이탈분은 현금. 목표비중 복원은 분기말에만 ----
    laa=vdf[vdf['전략']=='LAA']
    if not laa.empty:
        total=laa['현재금액'].sum()+n(account_cash.get('LAA',0));cash_pct=0.0
        for _,r in laa.iterrows():
            filtered=r['티커'] in ('133690','245350');breached=filtered and r['SMA 위']=='NO'
            if breached:cash_pct+=r['목표%']
            if QUARTER_END or breached:
                tgt=0.0 if breached else total*r['목표%']/100
                note='SMA 이탈 → 현금화' if breached else ('목표비중 복원(분기말)' if QUARTER_END else '유지')
                plan_rows.append({'전략':'LAA','티커':r['티커'],'ETF':r['ETF'],'현재금액':r['현재금액'],'목표금액':tgt,'매매액(+매수/-매도)':tgt-r['현재금액'],'비고':note})
            else:
                plan_rows.append({'전략':'LAA','티커':r['티커'],'ETF':r['ETF'],'현재금액':r['현재금액'],'목표금액':r['현재금액'],'매매액(+매수/-매도)':0.0,'비고':'유지(분기중)'})
        cash_tgt=total*cash_pct/100
        plan_rows.append({'전략':'LAA','티커':'CASH','ETF':'현금','현재금액':n(account_cash.get('LAA',0)),'목표금액':cash_tgt,'매매액(+매수/-매도)':cash_tgt-n(account_cash.get('LAA',0)),'비고':'필터 이탈 자산 보관'})

    # ---- GSM: SMA 통과 후보 중 12M 1위 80%, 현금 20% (없으면 100% 현금) ----
    gsm=vdf[vdf['전략']=='GSM']
    if not gsm.empty:
        total=gsm['현재금액'].sum()+n(account_cash.get('GSM',0))
        passing=gsm[gsm['SMA 위']=='YES'].sort_values('12M',ascending=False)
        winner=passing.iloc[0] if not passing.empty else None
        for _,r in gsm.iterrows():
            is_winner=winner is not None and r['티커']==winner['티커']
            tgt=total*0.8 if is_winner else 0.0
            note='선정(80%)' if is_winner else ('SMA 이탈' if r['SMA 위']=='NO' else '미선정(순위 밀림)')
            plan_rows.append({'전략':'GSM','티커':r['티커'],'ETF':r['ETF'],'현재금액':r['현재금액'],'목표금액':tgt,'매매액(+매수/-매도)':tgt-r['현재금액'],'비고':note})
        cash_tgt=total*(0.2 if winner is not None else 1.0)
        plan_rows.append({'전략':'GSM','티커':'CASH','ETF':'현금','현재금액':n(account_cash.get('GSM',0)),'목표금액':cash_tgt,'매매액(+매수/-매도)':cash_tgt-n(account_cash.get('GSM',0)),'비고':'전략 대기현금' if winner is not None else '전 후보 SMA 이탈'})

    # ---- ISA: 나스닥100(신호) 고점대비 -10% → 레버리지(418660) 분할매수 ----
    isa=vdf[vdf['전략']=='ISA']
    if not isa.empty:
        r=isa.iloc[0];dd=trigger_dd.get('ISA');triggered=dd is not None and dd<=-0.10;cash=n(account_cash.get('ISA',0))
        buy=cash/2 if triggered else 0.0
        note=f'트리거 발동(신호 고점대비 {p(dd)}) → 현금 절반 분할매수' if triggered else f'대기(신호 고점대비 {p(dd) if dd is not None else "데이터 없음"})'
        plan_rows.append({'전략':'ISA','티커':r['티커'],'ETF':r['ETF'],'현재금액':r['현재금액'],'목표금액':r['현재금액']+buy,'매매액(+매수/-매도)':buy,'비고':note})
        plan_rows.append({'전략':'ISA','티커':'CASH','ETF':'현금','현재금액':cash,'목표금액':cash-buy,'매매액(+매수/-매도)':-buy,'비고':'매수 재원'})

    # ---- SSO: S&P500(360750) 자체 고점대비 -15%↓ → 현금(153130) 절반을 주식으로 ----
    sso=vdf[vdf['전략']=='SSO']
    if not sso.empty:
        total=sso['현재금액'].sum();dd=trigger_dd.get('SSO');triggered=dd is not None and dd<=-0.15
        stock_pct=85.0 if triggered else 70.0
        for _,r in sso.iterrows():
            is_stock=r['티커']=='360750';tgt=total*(stock_pct if is_stock else 100-stock_pct)/100
            note=(f'트리거 발동(고점대비 {p(dd)}) → 현금 절반 투입' if triggered else f'평시 유지(고점대비 {p(dd) if dd is not None else "데이터 없음"})') if is_stock else ('트리거 발동 → 현금 축소' if triggered else '평시 유지')
            plan_rows.append({'전략':'SSO','티커':r['티커'],'ETF':r['ETF'],'현재금액':r['현재금액'],'목표금액':tgt,'매매액(+매수/-매도)':tgt-r['현재금액'],'비고':note})

    # ---- EM/금/별도현금: 리밸런싱 대상 아님 ----
    em=vdf[vdf['전략']=='EM']
    if not em.empty:
        for _,r in em.iterrows():
            plan_rows.append({'전략':'EM','티커':r['티커'] or '-','ETF':r['ETF'],'현재금액':r['현재금액'],'목표금액':r['현재금액'],'매매액(+매수/-매도)':0.0,'비고':'매매 없음(연 1회만 허용)'})

    # ---- 사용자 추가 전략: 전용 규칙이 없으므로 목표비중(자산+현금) 그대로 복원하는 정적 리밸런싱 적용 ----
    for cfg in cfgs:
        code=cfg['code']
        if code in KNOWN_STRATEGIES: continue
        sub=vdf[vdf['전략']==code]
        if sub.empty: continue
        cash=n(account_cash.get(code,0));total=sub['현재금액'].sum()+cash
        for _,r in sub.iterrows():
            tgt=total*n(r['목표%'])/100
            plan_rows.append({'전략':code,'티커':r['티커'],'ETF':r['ETF'],'현재금액':r['현재금액'],'목표금액':tgt,'매매액(+매수/-매도)':tgt-r['현재금액'],'비고':'목표비중 리밸런싱'})
        cash_tgt=total*n(cfg.get('cash_pct',0))/100
        plan_rows.append({'전략':code,'티커':'CASH','ETF':'현금','현재금액':cash,'목표금액':cash_tgt,'매매액(+매수/-매도)':cash_tgt-cash,'비고':'현금 목표비중'})

    plan_df=pd.DataFrame(plan_rows)
    st.subheader('이번 달 Action Plan (매수/매도 금액)')
    if plan_df.empty:
        st.warning('종목 데이터를 먼저 불러오세요.')
    else:
        show=plan_df.copy()
        for c in ['현재금액','목표금액','매매액(+매수/-매도)']:show[c]=show[c].map(w)
        st.dataframe(show,use_container_width=True,hide_index=True)
        for strat,g in plan_df.groupby('전략'):
            buys=g[g['매매액(+매수/-매도)']>1000];sells=g[g['매매액(+매수/-매도)']<-1000]
            parts=[]
            if not buys.empty:parts.append('매수: '+', '.join(f"{x.ETF} {w(x['매매액(+매수/-매도)'])}" for _,x in buys.iterrows()))
            if not sells.empty:parts.append('매도: '+', '.join(f"{x.ETF} {w(-x['매매액(+매수/-매도)'])}" for _,x in sells.iterrows()))
            st.markdown(f"**{strat}** — "+(' · '.join(parts) if parts else '거래 없음'))

    if st.button('Action Plan 생성·히스토리 저장'):
        value=float(vdf[vdf['전략'].eq(strategy) if strategy!='ALL' else vdf['전략'].notna()]['현재금액'].sum());eq=get_state('equity');cf=get_state('cashflows');m=portfolio_perf(eq);irr=calc_xirr(eq,cf);h=get_state('history')
        plan_text=' | '.join(f"{r['전략']} {r['ETF']}: {w(r['매매액(+매수/-매도)'])} ({r['비고']})" for _,r in plan_df.iterrows() if abs(r['매매액(+매수/-매도)'])>1000) if not plan_df.empty else ''
        h.insert(0,{'date':run_date.isoformat(),'strategy':strategy,'value':value,'plan':plan_text,'CAGR':m[0] if m else None,'MDD':m[1] if m else None,'IRR':irr});put_state('history',h);st.success('저장했습니다.')

elif page=='포트폴리오 대시보드':
    st.subheader('포트폴리오 대시보드');st.caption('전략(계좌)별 목표비중 대비 현재비중을 한눈에 봅니다 · Snowball72 스타일 참고')
    grand_total, snap_df, cfgs = compute_portfolio_snapshot(assets)
    st.metric('전체 총자산 (모든 전략 합계)', w(grand_total))
    if snap_df.empty:
        st.info('전략과 ETF를 먼저 구성하세요.')
    else:
        for cfg in cfgs:
            code=cfg['code'];g=snap_df[snap_df['전략']==code]
            if g.empty:continue
            strat_total=g['현재금액'].sum()
            badge=' · 동적(모멘텀)' if cfg.get('dynamic') else ''
            st.markdown(f"#### {code} · {cfg.get('account',code)}{badge} — {w(strat_total)}")
            show=g[['ETF','현재금액','현재비중','목표비중']].copy()
            st.dataframe(show,use_container_width=True,hide_index=True,column_config={
                '현재금액':st.column_config.NumberColumn('현재금액',format='%d원'),
                '현재비중':st.column_config.ProgressColumn('현재비중',format='%.1f%%',min_value=0,max_value=100),
                '목표비중':st.column_config.NumberColumn('목표비중(%)',format='%.1f%%'),
            })
        st.divider();st.markdown('#### 전략별 비중 (전체 자산 대비)')
        by_strategy=snap_df.groupby('전략')['현재금액'].sum()
        if grand_total>0:
            st.bar_chart((by_strategy/grand_total*100).rename('비중(%)'))

elif page=='전략 구성':
    st.subheader('전략별 ETF 구성');st.caption('전략을 활성화한 뒤 해당 전략의 ETF만 편집합니다. ETF 검색은 런타임 KRX 목록을 우선 사용하고 실패 시 번들 CSV를 사용합니다.')
    cfgs=get_strategies();codes=[c['code'] for c in cfgs]

    with st.expander('➕ 새 전략 추가'):
        nc1,nc2,nc3,nc4=st.columns([1,2,1,1])
        new_code=nc1.text_input('전략 코드',placeholder='예: CORE2')
        new_account=nc2.text_input('계좌명',placeholder='예: 개인연금')
        new_dynamic=nc3.checkbox('동적(모멘텀 선택형)',value=False,help='GSM처럼 매달 후보 중 하나를 골라 투자하는 방식이면 체크. 체크하면 100% 합계 검사를 하지 않습니다.')
        new_cash_pct=nc4.number_input('현금비중(%)',min_value=0.0,max_value=100.0,value=0.0,step=1.0)
        if st.button('전략 추가'):
            code_clean=new_code.strip().upper()
            if not code_clean:
                st.error('전략 코드를 입력하세요.')
            elif code_clean in codes:
                st.error('이미 존재하는 전략 코드입니다.')
            else:
                cfgs.append({'code':code_clean,'account':new_account.strip() or code_clean,'dynamic':new_dynamic,'cash_pct':new_cash_pct})
                put_state('strategies',cfgs);st.success(f'{code_clean} 전략을 추가했습니다.');st.rerun()

    active={s:st.checkbox(f'{s} 활성화',value=True,key=f'act_{s}') for s in codes};chosen=st.selectbox('편집할 전략',[s for s in codes if active[s]] or codes)
    chosen_cfg=next((c for c in cfgs if c['code']==chosen),{'code':chosen,'account':chosen,'dynamic':False,'cash_pct':0.0})

    cc1,cc2,cc3=st.columns(3)
    edit_account=cc1.text_input('계좌명',value=chosen_cfg.get('account',chosen))
    edit_dynamic=cc2.checkbox('동적(모멘텀 선택형)',value=chosen_cfg.get('dynamic',False),key=f'dyn_{chosen}')
    edit_cash_pct=cc3.number_input('현금 목표비중(%)',min_value=0.0,max_value=100.0,value=n(chosen_cfg.get('cash_pct',0)),step=0.5,key=f'cashpct_{chosen}')

    subset=assets[assets['strategy'].eq(chosen)].copy();edited=st.data_editor(subset, num_rows='dynamic',use_container_width=True,hide_index=True,column_config={'target_pct':st.column_config.NumberColumn('목표%',min_value=0,max_value=100,step=0.1),'shares':st.column_config.NumberColumn('보유수량',step=0.0001),'current_amount':st.column_config.NumberColumn('현재금액',step=1000),'close':st.column_config.NumberColumn('종가',step=0.01)})

    asset_sum=pd.to_numeric(edited['target_pct'],errors='coerce').fillna(0.0).sum()
    if edit_dynamic:
        st.caption(f'동적 전략: 자산 목표% 합계는 검사하지 않습니다 (현재 {asset_sum:.1f}%). 현금비중 {edit_cash_pct:.1f}%는 평시(트리거 미발동) 기준값으로만 사용됩니다.')
    else:
        total_check=asset_sum+edit_cash_pct
        if abs(total_check-100)>0.05:
            st.error(f'자산 목표비중 합({asset_sum:.1f}%) + 현금비중({edit_cash_pct:.1f}%) = {total_check:.1f}% — 100%가 되어야 저장됩니다.')
        else:
            st.success(f'자산 목표비중 합({asset_sum:.1f}%) + 현금비중({edit_cash_pct:.1f}%) = 100% ✓')

    st.markdown('### ETF 검색·추가');q=st.text_input('티커 또는 상품명 일부 입력');catalog=load_krx_etfs(date.today().isoformat());filtered=catalog[catalog['ticker'].str.contains(q,case=False,na=False)|catalog['name'].str.contains(q,case=False,na=False)] if q else catalog.head(100);opts=['선택 안 함']+[f'{r.ticker} · {r.name}' for _,r in filtered.head(200).iterrows()];picked=st.selectbox('KRX ETF 선택',opts)
    if st.button('선택 ETF를 전략에 추가') and picked!='선택 안 함':
        t,nm=picked.split(' · ',1);assets.loc[len(assets)]={'id':str(len(assets)+1),'strategy':chosen,'account':chosen,'ticker':t,'name':nm,'market':'KR','role':'사용자 추가','target_pct':0.0,'shares':0.0,'current_amount':0.0,'close':0.0,'prices':[],'signal_ticker':t};st.session_state.assets=assets;put_state('assets',assets.to_dict('records'));st.success(f'{t}를 {chosen}에 추가했습니다.');st.rerun()

    if st.button('선택 전략 저장',type='primary'):
        edited_clean=clean_records(edited)
        ok,msg=validate_strategy_weights(edited_clean,{'dynamic':edit_dynamic,'cash_pct':edit_cash_pct})
        if not ok:
            st.error(msg)
        else:
            assets2=assets[~assets['strategy'].eq(chosen)].copy();edited_clean['strategy']=chosen;assets2=pd.concat([assets2,edited_clean],ignore_index=True)
            st.session_state.assets=assets2;put_state('assets',assets2.to_dict('records'))
            new_cfgs=[c for c in cfgs if c['code']!=chosen]+[{'code':chosen,'account':edit_account.strip() or chosen,'dynamic':edit_dynamic,'cash_pct':edit_cash_pct}]
            put_state('strategies',new_cfgs)
            st.success('저장했습니다.');st.rerun()
    st.info(f'KRX 목록: {len(catalog):,}개 · 목록 출처: pykrx 런타임 조회, 실패 시 krx_etf_fallback.csv')

elif page=='성과 비교':
    st.subheader('월말 총자산 (전략별 자동 합산)');st.caption('각 전략의 현재금액+현금을 자동으로 합산합니다. 아래에서 확인 후 히스토리에 반영하세요.')
    grand_total, snap_df, _ = compute_portfolio_snapshot(assets)
    st.metric('현재 계산된 총자산', w(grand_total))
    with st.expander('전략별 세부 내역 보기'):
        if snap_df.empty:
            st.info('전략과 ETF를 먼저 구성하세요.')
        else:
            show=snap_df.copy();show['현재금액']=show['현재금액'].map(w);show['현재비중']=show['현재비중'].map(lambda x:f'{x:.1f}%');show['목표비중']=show['목표비중'].map(lambda x:f'{x:.1f}%')
            st.dataframe(show,use_container_width=True,hide_index=True)
    d=st.date_input('반영할 날짜',date.today(),key='eqd')
    if st.button('이번 총자산을 히스토리에 반영',type='primary'):
        e=[x for x in get_state('equity') if x['date']!=d.isoformat()];e.append({'date':d.isoformat(),'value':grand_total});put_state('equity',e);st.success('반영했습니다.')
    e=get_state('equity');cf=get_state('cashflows');m=portfolio_perf(e);irr=calc_xirr(e,cf);a,b,c=st.columns(3);a.metric('CAGR',p(m[0]) if m else '—');b.metric('MDD',p(m[1]) if m else '—');c.metric('IRR/XIRR',p(irr) if irr is not None else '—')
    if e:st.line_chart(pd.DataFrame(e).assign(date=lambda x:pd.to_datetime(x.date)).set_index('date')['value'])
    st.divider();st.subheader('벤치마크 입력·동일 기간 누적 비교');st.caption('내 첫 월말 자산 기록일을 시작점으로 100에 정규화합니다. QQQ·SPY·KOSPI200도 같은 기간만 사용합니다.')
    bn=st.selectbox('벤치마크',['QQQ','SPY','KOSPI200']);bd=st.date_input('벤치마크 기준일',date.today(),key='bd');bv=st.number_input('벤치마크 값',min_value=0.0,step=0.01,key='bv')
    if st.button('벤치마크 저장'):
        bs=[x for x in get_state('benchmarks') if not(x['name']==bn and x['date']==bd.isoformat())];bs.append({'name':bn,'date':bd.isoformat(),'value':bv});put_state('benchmarks',bs);st.success('저장했습니다.')
    series={}
    if e:
        first=sorted(e,key=lambda x:x['date'])[0]['date'];base=sorted(e,key=lambda x:x['date'])[0]['value'];series['내 포트폴리오']=[{'date':x['date'],'value':x['value']/base*100} for x in e]
        for name in ['QQQ','SPY','KOSPI200']:
            z=sorted([x for x in get_state('benchmarks') if x['name']==name and x['date']>=first],key=lambda x:x['date'])
            if z:series[name]=[{'date':x['date'],'value':x['value']/z[0]['value']*100} for x in z]
    if series:
        chart=pd.concat([pd.DataFrame(v).assign(date=lambda x:pd.to_datetime(x.date)).set_index('date').rename(columns={'value':k}) for k,v in series.items()],axis=1).sort_index();st.line_chart(chart)
        for name,vals in series.items():
            mm=portfolio_perf(vals);st.write(f'**{name}** — CAGR {p(mm[0]) if mm else "—"} · MDD {p(mm[1]) if mm else "—"}')
    st.divider();st.subheader('입출금 원장');cd=st.date_input('거래일',date.today(),key='cd');ca=st.number_input('금액(입금 + / 출금 -)',step=100000.0,key='ca');cm=st.text_input('메모',key='cm')
    if st.button('입출금 저장'):
        x=get_state('cashflows');x.append({'date':cd.isoformat(),'amount':ca,'memo':cm});put_state('cashflows',x);st.success('저장했습니다.')
    st.dataframe(pd.DataFrame(get_state('cashflows')),use_container_width=True,hide_index=True)

else:
    st.subheader('리밸런싱 히스토리');h=get_state('history');st.dataframe(pd.DataFrame(h) if h else pd.DataFrame(),use_container_width=True,hide_index=True);st.download_button('JSON 백업',json.dumps({k:get_state(k) for k in ['assets','history','equity','cashflows','benchmarks']},ensure_ascii=False,indent=2),file_name='portfolio-backup.json',mime='application/json');st.download_button('CSV 히스토리',pd.DataFrame(h).to_csv(index=False),file_name='rebalance-history.csv',mime='text/csv')
