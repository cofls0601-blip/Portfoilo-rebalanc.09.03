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
STRATEGIES = ['LAA', 'GSM', 'ISA', 'SSO', 'EM']

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
    for c in ['strategy','account','ticker','name','market','role']:
        if c not in df: df[c]=''
        df[c]=df[c].fillna('').astype(str)
    return df

DEFAULT_ROWS=[
 ('LAA','과세 연금저축','133690','TIGER 미국나스닥100','NASDAQ',12.5),('LAA','과세 연금저축','245350','TIGER 유로스탁스배당30','EuroStoxx',12.5),('LAA','과세 연금저축','360750','TIGER 미국S&P500','S&P500',12.5),('LAA','과세 연금저축','251350','KODEX 선진국MSCI World','MSCI World',15.5),('LAA','과세 연금저축','132030','KODEX 골드선물(H)','Gold',25),('LAA','과세 연금저축','148070','KIWOOM 국고채10년','Bond',25),
 ('GSM','비과세 연금저축','360750','TIGER 미국S&P500','GSM 후보',0),('GSM','비과세 연금저축','251350','KODEX 선진국MSCI World','GSM 후보',0),('GSM','비과세 연금저축','133690','TIGER 미국나스닥100','GSM 후보',0),('GSM','비과세 연금저축','245350','TIGER 유로스탁스배당30','GSM 후보',0),('ISA','ISA','133690','TIGER 미국나스닥100','-10% 트리거',0),('SSO','일반계좌 2','360750','TIGER 미국S&P500','S&P500 기준',70),('SSO','일반계좌 2','153130','KODEX 단기채권','현금',30),
 ('EM','일반계좌 1','069500','KODEX 200','한국',25),('EM','일반계좌 1','','중국 ETF 입력','중국',25),('EM','일반계좌 1','','인도 ETF 입력','인도',25),('EM','일반계좌 1','','베트남 ETF 입력','베트남',25)]
def default_assets():
    return pd.DataFrame([{'id':str(i),'strategy':a,'account':b,'ticker':c,'name':d,'market':'KR','role':e,'target_pct':f,'shares':0.0,'current_amount':0.0,'close':0.0,'prices':[]} for i,(a,b,c,d,e,f) in enumerate(DEFAULT_ROWS)])

# ---------- SQLite persistence ----------
def init_db():
    con=sqlite3.connect(DB_PATH);con.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT NOT NULL)')
    for k,v in [('assets',default_assets().to_json(orient='records',force_ascii=False)),('history','[]'),('equity','[]'),('cashflows','[]'),('benchmarks','[]')]: con.execute('INSERT OR IGNORE INTO kv(k,v) VALUES(?,?)',(k,v))
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

# ---------- app ----------
if 'assets' not in st.session_state:st.session_state.assets=clean_records(pd.DataFrame(get_state('assets')))
assets=clean_records(st.session_state.assets)
with st.sidebar:
    st.markdown('## 📊 자산배분 도우미');page=st.radio('메뉴',['Action Plan','전략 구성','성과 비교','리밸런싱 히스토리']);st.caption('자동주문 없음 · 지정일 실행만 저장')
st.title('자산배분 리밸런싱 도우미');st.caption('한국 상장 ETF · 10개월 SMA · 12개월 모멘텀 · CAGR/MDD/IRR')

if page=='Action Plan':
    c1,c2,c3=st.columns(3);run_date=c1.date_input('리밸런싱 기준일',date.today());source=c2.selectbox('가격 소스',['krx','data_go'],format_func=lambda x:'KRX Open API' if x=='krx' else '공공데이터포털');strategy=c3.selectbox('저장 전략',['ALL']+STRATEGIES)
    st.info('종가를 불러온 뒤 저장 버튼을 눌렀을 때만 선택일 기준 Action Plan과 히스토리가 생성됩니다.')
    if st.button('선택일 종가·13개월 월말 데이터 불러오기',type='primary'):
        ok=0;errors=[]
        for i,a in assets.iterrows():
            t=str(a['ticker']).strip()
            if not t:continue
            try:
                daydf=fetch_day(source,t,run_date.isoformat());exact=daydf[daydf['date'].eq(run_date.strftime('%Y%m%d'))];row=exact.iloc[-1] if not exact.empty else daydf.iloc[-1];assets.at[i,'close']=row['close']
                hist=fetch_monthly(source,t,run_date.isoformat());assets.at[i,'prices']=hist.sort_values('date')['close'].tolist() if not hist.empty else [row['close']];ok+=1
            except Exception as e:errors.append(f'{t}: {e}')
        st.session_state.assets=assets;put_state('assets',assets.to_dict('records'));st.success(f'{ok}개 종목 반영')
        if errors:st.warning(' / '.join(errors[:5]))
    rows=[]
    for i,a in assets.iterrows():
        close,sma,mom=calc_prices(a);rows.append({'idx':i,'전략':a['strategy'],'티커':a['ticker'],'ETF':a['name'],'종가':close,'SMA10':sma,'SMA 위':'YES' if sma and close>sma else 'NO','12M':mom,'현재금액':asset_value(a),'목표%':a['target_pct']})
    vdf=pd.DataFrame(rows);st.dataframe(vdf.drop(columns=['idx']),use_container_width=True,hide_index=True)
    gsm=vdf[(vdf['전략']=='GSM')&(vdf['SMA 위']=='YES')].sort_values('12M',ascending=False);lines=[]
    if not gsm.empty:
        g=gsm.iloc[0];tv=vdf[vdf['전략']=='GSM']['현재금액'].sum();lines.append(f'GSM: {g.티커} {g.ETF} 80%({w(tv*.8)}) · 현금 20%({w(tv*.2)}) · 12M {p(g["12M"])}')
    else:lines.append('GSM: SMA10 위 후보 없음 → 100% 현금')
    laa=vdf[vdf['전략']=='LAA'];
    if not laa.empty:
        nas=laa[laa['전략'].eq('LAA')&laa['티커'].eq('133690')];eur=laa[laa['전략'].eq('LAA')&laa['티커'].eq('245350')];lines.append(f'LAA: NASDAQ {"유지" if not nas.empty and nas.iloc[0]["SMA 위"]=="YES" else "필터 이탈"} · EuroStoxx {"유지" if not eur.empty and eur.iloc[0]["SMA 위"]=="YES" else "필터 이탈"} · 분기말에만 복원')
    lines += ['ISA: NASDAQ 고점 대비 -10% 트리거 확인 후 분할매수','SSO: S&P500 -15%~-20% 하락 시 현금 절반 투입','신흥국·금: 지정된 조정 시점 외 변경 없음']
    st.subheader('이번 달 Action Plan');st.markdown('\n'.join('- '+x for x in lines))
    if st.button('Action Plan 생성·히스토리 저장'):
        value=float(vdf[vdf['전략'].eq(strategy) if strategy!='ALL' else vdf['전략'].notna()]['현재금액'].sum());eq=get_state('equity');cf=get_state('cashflows');m=portfolio_perf(eq);irr=calc_xirr(eq,cf);h=get_state('history');h.insert(0,{'date':run_date.isoformat(),'strategy':strategy,'value':value,'plan':' | '.join(lines),'CAGR':m[0] if m else None,'MDD':m[1] if m else None,'IRR':irr});put_state('history',h);st.success('저장했습니다.')

elif page=='전략 구성':
    st.subheader('전략별 ETF 구성');st.caption('전략을 활성화한 뒤 해당 전략의 ETF만 편집합니다. ETF 검색은 런타임 KRX 목록을 우선 사용하고 실패 시 번들 CSV를 사용합니다.')
    active={s:st.checkbox(f'{s} 활성화',value=True,key=f'act_{s}') for s in STRATEGIES};chosen=st.selectbox('편집할 전략',[s for s in STRATEGIES if active[s]] or STRATEGIES)
    subset=assets[assets['strategy'].eq(chosen)].copy();edited=st.data_editor(subset, num_rows='dynamic',use_container_width=True,hide_index=True,column_config={'target_pct':st.column_config.NumberColumn('목표%',min_value=0,max_value=100,step=0.1),'shares':st.column_config.NumberColumn('보유수량',step=0.0001),'current_amount':st.column_config.NumberColumn('현재금액',step=1000),'close':st.column_config.NumberColumn('종가',step=0.01)})
    st.markdown('### ETF 검색·추가');q=st.text_input('티커 또는 상품명 일부 입력');catalog=load_krx_etfs(date.today().isoformat());filtered=catalog[catalog['ticker'].str.contains(q,case=False,na=False)|catalog['name'].str.contains(q,case=False,na=False)] if q else catalog.head(100);opts=['선택 안 함']+[f'{r.ticker} · {r.name}' for _,r in filtered.head(200).iterrows()];picked=st.selectbox('KRX ETF 선택',opts)
    if st.button('선택 ETF를 전략에 추가') and picked!='선택 안 함':
        t,nm=picked.split(' · ',1);assets.loc[len(assets)]={'id':str(len(assets)+1),'strategy':chosen,'account':chosen,'ticker':t,'name':nm,'market':'KR','role':'사용자 추가','target_pct':0.0,'shares':0.0,'current_amount':0.0,'close':0.0,'prices':[]};st.session_state.assets=assets;put_state('assets',assets.to_dict('records'));st.success(f'{t}를 {chosen}에 추가했습니다.');st.rerun()
    if st.button('선택 전략 저장',type='primary'):
        assets=assets[~assets['strategy'].eq(chosen)].copy();edited=clean_records(edited);edited['strategy']=chosen;assets=pd.concat([assets,edited],ignore_index=True);st.session_state.assets=assets;put_state('assets',assets.to_dict('records'));st.success('저장했습니다.')
    st.info(f'KRX 목록: {len(catalog):,}개 · 목록 출처: pykrx 런타임 조회, 실패 시 krx_etf_fallback.csv')

elif page=='성과 비교':
    st.subheader('월말 총자산·성과');d=st.date_input('월말 기준일',date.today(),key='eqd');v=st.number_input('총자산(원)',min_value=0.0,step=100000.0,key='eqv')
    if st.button('월말 자산 저장',type='primary'):
        e=[x for x in get_state('equity') if x['date']!=d.isoformat()];e.append({'date':d.isoformat(),'value':v});put_state('equity',e);st.success('저장했습니다.')
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
