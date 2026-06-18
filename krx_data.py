"""
krx_data.py
KRX Open API를 이용해 코스피/코스닥 전종목 일별시세를 가져오는 모듈.
기존 main.py의 네이버 스크래핑 함수(get_market_database, build_database)를
이 모듈의 함수들로 교체해서 사용합니다.
"""

import streamlit as st
import requests
import datetime
import pandas as pd

KRX_ENDPOINTS = {
    "KOSPI": "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
    "KOSDAQ": "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd",
}


def get_krx_auth_key():
    """Streamlit secrets에서 KRX 인증키를 읽어온다."""
    try:
        return st.secrets["KRX_AUTH_KEY"]
    except Exception:
        st.error("🚨 KRX_AUTH_KEY가 설정되지 않았습니다. Streamlit Secrets에 등록해주세요.")
        return None


def get_recent_business_day(offset=1):
    """오늘 기준으로 가장 가까운 과거 영업일(평일)을 YYYYMMDD로 반환."""
    today = datetime.date.today()
    candidate = today - datetime.timedelta(days=offset)
    while candidate.weekday() >= 5:  # 5=토, 6=일
        offset += 1
        candidate = today - datetime.timedelta(days=offset)
    return candidate.strftime("%Y%m%d")


def _fetch_one_market(market_name, url, auth_key, bas_dd):
    headers = {"AUTH_KEY": auth_key}
    params = {"basDd": bas_dd}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=20)
        res.raise_for_status()
        data = res.json()
        return data.get("OutBlock_1", [])
    except Exception as e:
        st.warning(f"⚠️ [{market_name}] KRX 데이터 수신 실패: {e}")
        return []


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_krx_all_market(bas_dd=None, max_retry_days=5):
    """
    코스피+코스닥 전종목 일별시세를 받아서 하나의 DataFrame으로 반환.
    bas_dd를 지정하지 않으면 가장 최근 영업일부터 최대 max_retry_days일 전까지
    거슬러 올라가며 데이터가 있는 날짜를 찾는다 (휴장일/이벤트 대비).
    """
    auth_key = get_krx_auth_key()
    if not auth_key:
        return pd.DataFrame()

    offset = 1
    for _ in range(max_retry_days):
        target_dd = bas_dd if bas_dd else get_recent_business_day(offset)

        all_rows = []
        for market_name, url in KRX_ENDPOINTS.items():
            rows = _fetch_one_market(market_name, url, auth_key, target_dd)
            all_rows.extend(rows)

        if all_rows:
            df = pd.DataFrame(all_rows)
            df["BAS_DD"] = target_dd
            return df

        if bas_dd:  # 특정 날짜를 지정했는데 데이터가 없으면 재시도하지 않고 종료
            break
        offset += 1

    st.warning("⚠️ 최근 영업일 데이터를 찾지 못했습니다.")
    return pd.DataFrame()


def to_numeric_safe(series):
    return pd.to_numeric(series.astype(str).str.replace(",", ""), errors="coerce")


@st.cache_data(ttl=86400, show_spinner=False)
def get_market_database_krx(market_type="한국"):
    """
    기존 get_market_database()의 한국시장 대체 함수.
    반환: {"005930.KS": "삼성전자", "035720.KQ": "카카오", ...} 형태의 딕셔너리
    """
    df = fetch_krx_all_market()
    if df.empty:
        return {}

    # 🔥 1. 시가총액(MKTCAP)을 숫자로 변환 후 내림차순 정렬 (시총 1위부터 정렬)
    df["MKTCAP"] = pd.to_numeric(df["MKTCAP"], errors="coerce").fillna(0)
    df = df.sort_values(by="MKTCAP", ascending=False)

    ticker_map = {}
    for _, row in df.iterrows():
        code = str(row["ISU_CD"]).zfill(6)
        name = row["ISU_NM"]
        mkt = row["MKT_NM"]  # "KOSPI" or "KOSDAQ"
        suffix = ".KS" if mkt == "KOSPI" else ".KQ"

        # 스팩/우선주 제외 (기존 로직과 동일한 필터)
        if any(x in name for x in ["스팩", "우B"]):
            continue

        ticker_map[f"{code}{suffix}"] = name
        
        # 🔥 2. 스팩/우선주를 제외하고 정확히 500개가 채워지면 반복문 종료
        if len(ticker_map) >= 500:
            break

    return ticker_map


@st.cache_data(ttl=86400, show_spinner=False)
def get_krx_price_table():
    """
    화면 표시/필터링에 바로 쓸 수 있게 정제된 가격 테이블을 반환.
    컬럼: Ticker, Name, Market, Close, Open, High, Low, Volume, TrdVal, Mktcap
    """
    df = fetch_krx_all_market()
    if df.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["Ticker"] = df["ISU_CD"].astype(str).str.zfill(6) + df["MKT_NM"].map(
        {"KOSPI": ".KS", "KOSDAQ": ".KQ"}
    )
    out["Name"] = df["ISU_NM"]
    out["Market"] = df["MKT_NM"]
    out["Close"] = to_numeric_safe(df["TDD_CLSPRC"])
    out["Open"] = to_numeric_safe(df["TDD_OPNPRC"])
    out["High"] = to_numeric_safe(df["TDD_HGPRC"])
    out["Low"] = to_numeric_safe(df["TDD_LWPRC"])
    out["Volume"] = to_numeric_safe(df["ACC_TRDVOL"])
    out["TrdVal"] = to_numeric_safe(df["ACC_TRDVAL"])
    out["Mktcap"] = to_numeric_safe(df["MKTCAP"])
    out["BasDd"] = df["BAS_DD"]

    # 스팩/우선주(B) 제외 - 기존 로직과 동일
    out = out[~out["Name"].str.contains("스팩|우B", na=False)]
    
    # 🔥 3. 데이터프레임 자체도 시총 기준 상위 500개만 남기도록 정렬 후 자르기
    out = out.sort_values(by="Mktcap", ascending=False).head(500)

    return out.reset_index(drop=True)
