import os, json, sqlite3, math
from datetime import date, datetime
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='자산배분 리밸런싱 도우미', page_icon='📊', layout='wide', initial_sidebar_state='expanded')

# ---------- Config / persistence ----------
DEFAULT_ASSETS = pd.DataFrame([
 {'strategy':'LAA','account':'과세 연금저축','ticker':'133690','name':'TIGER 미국나스닥100','market':'KR','role':'NASDAQ','target_pct':12.5,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'LAA','account':'과세 연금저축','ticker':'245350','name':'TIGER 유로스탁스배당30','market':'KR','role':'EuroStoxx','target_pct':12.5,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'LAA','account':'과세 연금저축','ticker':'360750','name':'TIGER 미국S&P500','market':'KR','role':'S&P500','target_pct':12.5,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'LAA','account':'과세 연금저축','ticker':'251350','name':'KODEX 선진국MSCI World','market':'KR','role':'MSCI World','target_pct':15.5,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'LAA','account':'과세 연금저축','ticker':'132030','name':'KODEX 골드선물(H)','market':'KR','role':'Gold','target_pct':25.0,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'LAA','account':'과세 연금저축','ticker':'148070','name':'KIWOOM 국고채10년','market':'KR','role':'Bond','target_pct':25.0,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'GSM','account':'비과세 연금저축','ticker':'360750','name':'TIGER 미국S&P500','market':'KR','role':'GSM 후보','target_pct':0.0,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'GSM','account':'비과세 연금저축','ticker':'251350','name':'KODEX 선진국MSCI World','market':'KR','role':'GSM 후보','target_pct':0.0,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'GSM','account':'비과세 연금저축','ticker':'133690','name':'TIGER 미국나스닥100','market':'KR','role':'GSM 후보','target_pct':0.0,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'GSM','account':'비과세 연금저축','ticker':'245350','name':'TIGER 유로스탁스배당30','market':'KR','role':'GSM 후보','target_pct':0.0,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'ISA','account':'ISA','ticker':'133690','name':'TIGER 미국나스닥100','market':'KR','role':'-10% 트리거','target_pct':0.0,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'SSO','account':'일반계좌 2','ticker':'360750','name':'TIGER 미국S&P500','market':'KR','role':'S&P500 기준','target_pct':70.0,'shares':0.0,'current_amount':0.0,'close':0.0},
 {'strategy':'SSO','account':'일반계좌 2','ticker':'153130','name':'KODEX 단기채권','market':'KR','role':'현금','target_pct':30.0,'shares':0.0,'current_amount':0.0,'close':0.0},
])

KRX_SOURCES = {'krx':'KRX Open API','data_go':'공공데이터포털 금융위원회 주식시세정보'}

def db_path(): return st.secrets.get('SQLITE_PATH', 'portfolio.db')
def init_db():
    con=sqlite3.connect(db_path()); c=con.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)')
    for k,v in [('assets',DEFAULT_ASSETS.to_json(orient='records',force_ascii=False)),('history','[]'),('equity','[]'),('cashflows','[]'),('benchmarks','[]')]:
        c.execute('INSERT OR IGNORE INTO kv(k,v) VALUES(?,?)',(k,v))
    con.commit(); con.close()
def get_state(k):
    init_db(); con=sqlite3.connect(db_path()); row=con.execute('SELECT v FROM kv WHERE k=?',(k,)).fetchone(); con.close(); return json.loads(row[0])
def put_state(k,v):
    init_db(); con=sqlite3.connect(db_path()); con.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)',(k,json.dumps(v,ensure_ascii=False,default=str))); con.commit(); con.close()

# ---------- Market data ----------
def configured(name):
    try: return st.secrets[name]
    except Exception: return ''

def normalize_payload(payload, ticker, selected_date):
    if isinstance(payload,dict):
        rows=payload.get('data', payload.get('OutBlock_1', payload.get('response',{}).get('body',{}).get('items',{}).get('item',payload)))
    else:
        rows=payload
    if isinstance(rows,dict): rows=[rows]
    out=[]
    for x in rows or []:
        if not isinstance(x,dict): continue
        symbol=str(x.get('symbol',x.get('ISU_SRT_CD',x.get('ticker',ticker)))).replace('.KS','')
        d=x.get('date',x.get('basDd',x.get('stck_bsop_date',selected_date)))
        close=x.get('close',x.get('TDD_CLSPRC',x.get('stck_clpr',x.get('price'))))
        if close is None: continue
        try: close=float(str(close).replace(',',''))
        except: continue
        out.append({'ticker':symbol,'date':str(d).replace('-',''),'close':close})
    return pd.DataFrame(out)

def fetch_price(source,ticker,selected_date,need_history=True):
    if source=='krx':
        url=configured('KRX_BASE_URL')
        key=configured('KRX_AUTH_KEY')
        if not url or not key: raise RuntimeError('KRX_BASE_URL 또는 KRX_AUTH_KEY가 없습니다.')
        r=requests.get(url,headers={'AUTH_KEY':key},params={'basDd':selected_date.replace('-','')},timeout=25); r.raise_for_status(); payload=r.json()
    else:
        url=configured('DATA_GO_URL'); key=configured('DATA_GO_SERVICE_KEY')
        if not url or not key: raise RuntimeError('DATA_GO_URL 또는 DATA_GO_SERVICE_KEY가 없습니다.')
        r=requests.get(url,params={'serviceKey':key,'resultType':'json','numOfRows':1000,'pageNo':1,'basDt':selected_date.replace('-',''),'itmsNm':ticker},timeout=25); r.raise_for_status(); payload=r.json()
    df=normalize_payload(payload,ticker,selected_date)
    if df.empty: raise RuntimeError(f'{ticker}: 응답에서 종가를 찾지 못했습니다.')
    if source=='krx':
        matched=df[df['ticker'].astype(str).str.replace('.KS','')==str(ticker)]
        if not matched.empty: return matched
    return df

@st.cache_data(ttl=3600, show_spinner=False)
def cached_fetch(source,ticker,selected_date): return fetch_price(source,ticker,selected_date)

@st.cache_data(ttl=3600, show_spinner=False)
def cached_history(source,ticker,selected_date):
    dates=pd.date_range(end=pd.Timestamp(selected_date),periods=13,freq='ME')
    out=[]
    for d in dates:
        try:
            x=fetch_price(source,ticker,d.strftime('%Y-%m-%d'))
            x=x[x['ticker'].astype(str).str.replace('.KS','')==str(ticker)] if 'ticker' in x else x
            if not x.empty: out.append(x.iloc[-1].to_dict())
        except Exception: pass
    return pd.DataFrame(out)

# ---------- Calculations ----------
def xirr(flows):
    if len(flows)<2: return None
    flows=sorted(flows,key=lambda x:x['date']); d0=pd.Timestamp(flows[0]['date'])
    def f(r): return sum(x['amount']/((1+r)**((pd.Timestamp(x['date'])-d0).days/365.0)) for x in flows)
    lo,hi=-.9999,10
    for _ in range(120):
        mid=(lo+hi)/2
        if f(mid)>0: lo=mid
        else: hi=mid
    return (lo+hi)/2

def perf(rows):
    if len(rows)<2:return None
    x=sorted([r for r in rows if float(r['value'])>0],key=lambda r:r['date'])
    if len(x)<2:return None
    days=(pd.Timestamp(x[-1]['date'])-pd.Timestamp(x[0]['date'])).days
    cagr=(x[-1]['value']/x[0]['value'])**(365/days)-1 if days else None
    peak=0;mdd=0
    for r in x: peak=max(peak,r['value']);mdd=min(mdd,r['value']/peak-1)
    return cagr,mdd

def money(x): return f'{float(x):,.0f}원'
def pct(x): return f'{x*100:.2f}%'

# ---------- UI ----------
if 'assets' not in st.session_state: st.session_state.assets=pd.DataFrame(get_state('assets'))
assets=st.session_state.assets
for c in ['target_pct','shares','current_amount','close']: assets[c]=pd.to_numeric(assets.get(c,0),errors='coerce').fillna(0)
with st.sidebar:
    st.markdown('## 📊 리밸런싱 도우미')
    page=st.radio('메뉴',['Action Plan','ETF 구성','성과·비교','히스토리·백업'])
    st.caption('자동주문 없음 · 지정일 실행만 저장')

st.title('자산배분 리밸런싱 도우미')
st.caption('한국 상장 ETF · 10개월 SMA · 12개월 모멘텀 · CAGR/MDD/IRR')

if page=='Action Plan':
    c1,c2=st.columns([1,1]); run_date=c1.date_input('리밸런싱 기준일',date.today()); source=c2.selectbox('시장 데이터 소스',list(KRX_SOURCES),format_func=lambda x:KRX_SOURCES[x])
    st.info('기준일을 지정하고 종가를 불러온 뒤, 아래 저장 버튼을 눌러야만 Action Plan과 히스토리가 생성됩니다.')
    if st.button('선택일 한국 ETF 종가 불러오기',type='primary'):
        ok=0; errors=[]
        for i,a in assets.iterrows():
            if not str(a['ticker']).strip(): continue
            try:
                ticker=str(a['ticker']).strip()
                df=cached_fetch(source,ticker,run_date.isoformat())
                exact=df[df['date'].str.replace('-','')==run_date.strftime('%Y%m%d')]
                row=exact.iloc[-1] if not exact.empty else df.iloc[-1]
                assets.at[i,'close']=float(row['close'])
                hist=cached_history(source,ticker,run_date.isoformat())
                assets.at[i,'prices']=hist.sort_values('date')['close'].tolist()[-60:] if not hist.empty else [float(row['close'])]
                ok+=1
            except Exception as e: errors.append(str(e))
        st.session_state.assets=assets;put_state('assets',assets.to_dict('records'));st.success(f'{ok}개 종목 가격 반영');
        if errors: st.warning(' / '.join(errors[:3]))
    st.subheader('자산별 신호')
    view=[]
    for i,a in assets.iterrows():
        p=a.get('prices',[]) or ([a['close']] if a['close'] else []); sma=sum(p[-10:])/10 if len(p)>=10 else 0; mom=p[-1]/p[-13]-1 if len(p)>=13 and p[-13] else 0
        view.append({'idx':i,'전략':a['strategy'],'티커':a['ticker'],'ETF':a['name'],'종가':a['close'],'SMA10':sma,'SMA 위': 'YES' if sma and a['close']>sma else 'NO','12M':mom,'현재금액':a['current_amount'] or a['shares']*a['close'],'목표%':a['target_pct']})
    vdf=pd.DataFrame(view);st.dataframe(vdf.drop(columns=['idx']),use_container_width=True,hide_index=True)
    gsm=vdf[(vdf['전략']=='GSM')&(vdf['SMA 위']=='YES')].sort_values('12M',ascending=False)
    st.subheader('이번 달 Action Plan')
    lines=[]
    if not gsm.empty:
        winner=gsm.iloc[0]; total_gsm=vdf[vdf['전략']=='GSM']['현재금액'].sum();lines.append(f"**GSM:** {winner['티커']} {winner['ETF']} 편입 80% ({money(total_gsm*.8)}), 현금 20% ({money(total_gsm*.2)}). 12M {pct(winner['12M'])}")
    else: lines.append('**GSM:** SMA10 위 후보 없음 → 100% 현금')
    laa=vdf[vdf['전략']=='LAA'];
    if not laa.empty:
        def state(role):
            r=laa[laa['전략'].eq('LAA') & laa['티커'].notna()]
            return '확인 필요'
        lines.append('**LAA:** NASDAQ·EuroStoxx SMA 필터 확인. 분기말에만 목표비중 복원.')
    lines.append('**ISA:** NASDAQ 고점 대비 -10% 트리거를 별도 확인 후 분할매수.')
    lines.append('**SSO:** S&P500 기준 -15%~-20% 하락 시에만 현금 절반 투입.')
    lines.append('**신흥국·금:** 원래 규칙에 따라 지정된 조정 시점 외에는 변경하지 않음.')
    st.markdown('\n'.join('- '+x for x in lines))
    if st.button('Action Plan 생성·히스토리 저장'):
        selected=st.selectbox('저장할 전략', ['ALL','GSM','LAA','ISA','SSO','EM'],key='save_strategy') if False else 'ALL'
        total=float(vdf['현재금액'].sum()); plan=' | '.join(x.replace('**','') for x in lines)
        h=get_state('history');h.insert(0,{'date':run_date.isoformat(),'strategy':selected,'value':total,'plan':plan,'assets':assets.to_dict('records')});put_state('history',h);st.success('Action Plan을 히스토리에 저장했습니다.')

elif page=='ETF 구성':
    st.subheader('전략·ETF 구성 변경')
    edited=st.data_editor(assets, num_rows='dynamic', use_container_width=True, hide_index=True, column_config={'target_pct':st.column_config.NumberColumn('목표%',min_value=0,max_value=100,step=0.1),'shares':st.column_config.NumberColumn('보유수량',step=0.0001),'current_amount':st.column_config.NumberColumn('현재금액',step=1000),'close':st.column_config.NumberColumn('종가',step=0.01)})
    if st.button('구성표 저장',type='primary'):
        st.session_state.assets=edited;put_state('assets',edited.to_dict('records'));st.success('저장했습니다.')
    st.caption('GSM 후보는 target_pct를 0으로 두고 규칙이 80% 선택·20% 현금을 계산합니다. ETF는 자유롭게 추가할 수 있습니다.')

elif page=='성과·비교':
    st.subheader('월말 총자산 기록')
    d=st.date_input('월말 기준일',date.today(),key='eqd');v=st.number_input('총자산(원)',min_value=0.0,step=100000.0,key='eqv')
    if st.button('월말 자산 저장',type='primary'):
        e=[x for x in get_state('equity') if x['date']!=d.isoformat()];e.append({'date':d.isoformat(),'value':v});put_state('equity',e);st.success('월말 자산을 저장했습니다.')
    e=get_state('equity');m=perf(e);cf=get_state('cashflows');irr=xirr([{'date':x['date'],'amount':-x['amount']} for x in cf]+([{'date':sorted(e,key=lambda x:x['date'])[-1]['date'],'amount':sorted(e,key=lambda x:x['date'])[-1]['value']}] if e else [])) if e and cf else None
    a,b,c=st.columns(3);a.metric('CAGR',pct(m[0]) if m else '—');b.metric('MDD',pct(m[1]) if m else '—');c.metric('IRR/XIRR',pct(irr) if irr is not None else '—')
    if e: st.bar_chart(pd.DataFrame(e).set_index('date'))
    st.divider();st.subheader('QQQ·SPY·KOSPI200 벤치마크')
    bn=st.selectbox('벤치마크',['QQQ','SPY','KOSPI200']);bd=st.date_input('벤치마크 기준일',date.today(),key='bd');bv=st.number_input('벤치마크 값',min_value=0.0,step=0.01,key='bv')
    if st.button('벤치마크 저장'):
        bs=[x for x in get_state('benchmarks') if not(x['name']==bn and x['date']==bd.isoformat())];bs.append({'name':bn,'date':bd.isoformat(),'value':bv});put_state('benchmarks',bs);st.success('저장했습니다.')
    for name in ['QQQ','SPY','KOSPI200']:
        mm=perf([{'date':x['date'],'value':x['value']/sorted([z for z in get_state('benchmarks') if z['name']==name],key=lambda q:q['date'])[0]['value']*100} for x in get_state('benchmarks') if x['name']==name])
        st.write(f'**{name}** — CAGR {pct(mm[0]) if mm else "—"} · MDD {pct(mm[1]) if mm else "—"}')
    st.divider();st.subheader('입출금 원장')
    cd=st.date_input('거래일',date.today(),key='cd');ca=st.number_input('금액(입금 + / 출금 -)',step=100000.0,key='ca');cm=st.text_input('메모',key='cm')
    if st.button('입출금 저장'):
        x=get_state('cashflows');x.append({'date':cd.isoformat(),'amount':ca,'memo':cm});put_state('cashflows',x);st.success('저장했습니다.')
    st.dataframe(pd.DataFrame(get_state('cashflows')),use_container_width=True,hide_index=True)

else:
    st.subheader('리밸런싱 히스토리')
    h=get_state('history');st.dataframe(pd.DataFrame(h)[['date','strategy','value','plan']] if h else pd.DataFrame(),use_container_width=True,hide_index=True)
    st.download_button('JSON 백업',json.dumps({k:get_state(k) for k in ['assets','history','equity','cashflows','benchmarks']},ensure_ascii=False,indent=2),file_name='portfolio-backup.json',mime='application/json')
    st.download_button('CSV 히스토리',pd.DataFrame(h).to_csv(index=False),file_name='rebalance-history.csv',mime='text/csv')

st.divider();st.caption('가격 데이터 API가 실패하면 오류를 표시하고 기존 데이터·수동 입력을 유지합니다. 투자 판단과 주문 실행은 사용자가 합니다.')
