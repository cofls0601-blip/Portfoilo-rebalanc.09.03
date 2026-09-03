# 자산배분 리밸런싱 도우미

한국 상장 ETF를 기준으로 사용자가 지정한 날짜에만 종가를 조회하고, 10개월 SMA·12개월 모멘텀·전략별 규칙으로 Action Plan을 생성하는 Streamlit 앱입니다. 자동주문은 하지 않습니다.

## 포함 기능

- KRX Open API 또는 공공데이터포털 금융위원회 주식시세정보 API 선택
- 지정일 종가 조회 및 13개 월말 종가로 SMA10·12개월 수익률 계산
- GSM: SMA10 위 후보 중 12개월 수익률 1위, 80% 투자·20% 현금
- LAA: NASDAQ·EuroStoxx만 SMA 필터, 분기말 목표비중 복원
- ISA: NASDAQ 고점 대비 -10% 트리거 안내
- SSO: S&P500 기준 -15%~-20% 하락 시 현금 절반 투입 안내
- ETF 구성표 수정·추가·삭제
- 현재금액 또는 보유수량×종가 기반 주문금액 계산
- 리밸런싱 기준일과 Action Plan 히스토리 저장
- 월말 자산 기록, CAGR, MDD, XIRR
- QQQ·SPY·KOSPI200 벤치마크 CAGR·MDD 비교
- JSON/CSV 백업

## 1. 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
streamlit run app.py
```

## 2. KRX Open API 설정

1. [KRX Data Marketplace](https://openapi.krx.co.kr/) 회원가입 및 로그인
2. 인증키 신청
3. 서비스 목록에서 필요한 주식/증권상품 시세 서비스를 찾아 활용 신청
4. 관리자 승인 후 API 인증키 확인
5. 승인된 서비스 명세서의 실제 URL을 `KRX_BASE_URL`에 입력
6. 발급받은 키를 `KRX_AUTH_KEY`에 입력
7. 앱에서 `KRX Open API`를 선택

KRX 서비스는 요청 헤더의 `AUTH_KEY` 필드로 인증키를 전달합니다. 앱의 `fetch_price()`가 이 방식을 사용합니다. 서비스별 요청 파라미터와 응답 필드가 다를 수 있으므로, 승인된 서비스 명세서에 맞춰 `normalize_payload()`를 조정할 수 있습니다.

## 3. 공공데이터포털 대체 설정

1. [공공데이터포털 금융위원회_주식시세정보](https://www.data.go.kr/data/15094808/openapi.do)에서 활용 신청
2. 인증키 발급
3. 해당 API의 승인된 Endpoint를 `DATA_GO_URL`에 입력
4. `DATA_GO_SERVICE_KEY`에 서비스키 입력
5. 앱에서 `공공데이터포털 금융위원회 주식시세정보`를 선택

공공데이터포털 방식은 `serviceKey`, `resultType=json`, `numOfRows`, `pageNo`, 기준일자 및 종목 파라미터를 사용합니다. API 명세서의 실제 파라미터명이 다르면 `fetch_price()`의 params를 조정하십시오. 이 데이터셋은 공식 페이지에 일 1회 갱신 및 기준일자 기준 영업일 후 제공이라고 안내되어 있어, 당일 실시간 종가 목적보다는 확정된 일별 종가 기록에 적합합니다.

## 4. Streamlit Community Cloud 배포

1. 이 폴더를 GitHub 저장소에 업로드합니다. `secrets.toml`은 업로드하지 않습니다.
2. `requirements.txt`와 `app.py`가 저장소 루트에 있는지 확인합니다.
3. [share.streamlit.io](https://share.streamlit.io/) 또는 Streamlit Community Cloud에서 GitHub로 로그인합니다.
4. **Create app**을 선택합니다.
5. Repository, Branch, Main file path(`app.py`)를 지정하고 Deploy합니다.
6. 배포된 앱의 Settings → Secrets에서 다음 TOML을 입력합니다.

```toml
KRX_BASE_URL = "승인된 KRX 서비스 URL"
KRX_AUTH_KEY = "발급받은 KRX 인증키"
DATA_GO_URL = "승인된 data.go.kr Endpoint"
DATA_GO_SERVICE_KEY = "발급받은 공공데이터 서비스키"
SQLITE_PATH = "portfolio.db"
```

7. 저장 후 앱을 재실행합니다.

공개 링크 하나를 열면 PC·스마트폰 어디서든 같은 앱에 접속할 수 있습니다. 다만 Streamlit Community Cloud의 기본 로컬 파일은 재배포/재시작에 취약할 수 있으므로, 여러 기기에서 장기간 동일한 히스토리를 보존하려면 SQLite 대신 Supabase/Postgres 등 영속 DB 어댑터로 교체하는 것을 권장합니다. 현재 코드는 구조를 단순화한 SQLite 저장소이며, 개인 단일 앱의 초기 검증용입니다.

## 5. 보안

실제 키는 GitHub에 올리지 않습니다. 로컬에서는 `.streamlit/secrets.toml`, Cloud에서는 앱 Settings → Secrets를 사용합니다. Streamlit 공식 문서도 비밀정보를 저장소에 커밋하지 않고 Secrets 관리 기능에 저장하도록 안내합니다.

## 6. 데이터와 계산 주의

- 앱은 API 호출 실패 시 기존 데이터를 지우지 않고 오류를 표시합니다.
- 사용자가 지정한 날짜에만 Action Plan을 저장합니다.
- 휴장일을 자동으로 다음 거래일로 바꾸지 않습니다. 필요한 경우 사용자가 직전 거래일을 선택합니다.
- 액면분할·분배금·환율·괴리율 처리 여부는 선택한 API의 조정종가/시장가격 정의를 확인해야 합니다.
- 투자 판단과 주문 실행은 사용자 책임이며, 앱은 계산·기록 보조 도구입니다.
