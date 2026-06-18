import streamlit as st
import urllib.request, re, time, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import ta
import pandas as pd
import FinanceDataReader as fdr
import datetime
import pytz
import os
import sys
from io import StringIO
import xml.etree.ElementTree as ET
import difflib
import json
from krx_data import get_market_database_krx, get_krx_price_table, get_krx_full_search_map
from streamlit_gsheets import GSheetsConnection

# =======================================================================
# 🛡️ IP 차단 방지 글로벌 설정
# =======================================================================
USE_PROXY = False
PROXY_IP = "http://YOUR_PROXY_IP:PORT" 
PROXY_DICT = {"http": PROXY_IP, "https": PROXY_IP}

REQUEST_DELAY = 0.3 
MAX_WORKERS = 5

def get_urllib_opener():
    if USE_PROXY:
        proxy_handler = urllib.request.ProxyHandler(PROXY_DICT)
        return urllib.request.build_opener(proxy_handler)
    return urllib.request.build_opener()

# =======================================================================
# 💾 검색 필터 저장/불러오기 로직
# =======================================================================
FILTER_DB_FILE = "saved_filters.json"

def load_saved_filters():
    if os.path.exists(FILTER_DB_FILE):
        try:
            with open(FILTER_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_filter_preset(name, current_state_dict):
    filters = load_saved_filters()
    filters[name] = current_state_dict
    with open(FILTER_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(filters, f, ensure_ascii=False, indent=4)

def delete_filter_preset(name):
    filters = load_saved_filters()
    if name in filters:
        del filters[name]
        with open(FILTER_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(filters, f, ensure_ascii=False, indent=4)

# =======================================================================
# 💡 구글 시트 전용 관심종목 DB 관리 함수
# =======================================================================
def get_watchlist_df():
    """구글 시트에서 데이터를 읽어오는 공통 함수"""
    conn = st.connection("gsheets", type=GSheetsConnection)
    try:
        # ttl=0 이면 캐시를 무시하고 항상 최신 데이터를 가져옵니다.
        df = conn.read(worksheet="관심종목", ttl=0)
        # 빈 시트일 경우 기본 뼈대 반환
        if df.empty or "Ticker" not in df.columns:
            return pd.DataFrame(columns=["Ticker", "Name", "Target1", "Target2", "Date"])
        return df
    except Exception:
        # 시트가 없거나 오류 시 기본 뼈대 반환
        return pd.DataFrame(columns=["Ticker", "Name", "Target1", "Target2", "Date"])

def save_watchlist_df(df):
    """변경된 데이터를 구글 시트에 덮어쓰는 공통 함수"""
    conn = st.connection("gsheets", type=GSheetsConnection)
    conn.update(worksheet="관심종목", data=df)

def ensure_csv_format():
    pass

def save_to_watchlist_local(ticker, name, target1, target2):
    df = get_watchlist_df()
    df_new = pd.DataFrame({
        "Ticker": [ticker], "Name": [name], "Target1": [target1],
        "Target2": [target2], "Date": [datetime.datetime.now().strftime("%Y-%m-%d")]
    })
    # 기존에 같은 티커가 있으면 삭제하고 새로 추가 (중복 방지)
    df_final = pd.concat([df[df["Ticker"] != ticker], df_new])
    save_watchlist_df(df_final)
    st.success(f"✅ [{name}] 관심종목 구글 시트 저장 완료!")

def update_target_price(ticker, new_tg1, new_tg2):
    df = get_watchlist_df()
    if not df.empty and ticker in df["Ticker"].values:
        df.loc[df["Ticker"] == ticker, "Target1"] = new_tg1
        df.loc[df["Ticker"] == ticker, "Target2"] = new_tg2
        save_watchlist_df(df)

def delete_from_watchlist(ticker):
    df = get_watchlist_df()
    if not df.empty:
        df_final = df[df["Ticker"] != ticker]
        save_watchlist_df(df_final)

# =======================================================================
# ⚙️ 보조지표 및 데이터프레임 구조 생성 공통 함수
# =======================================================================
def process_technical_indicators(df):
    if len(df) < 25:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.ffill().bfill()
    
    c, h, l, v = df["Close"].squeeze(), df["High"].squeeze(), df["Low"].squeeze(), df["Volume"].squeeze()
    
    df["RSI"] = ta.momentum.rsi(c, window=14).fillna(0)
    df["OBV"] = ta.volume.OnBalanceVolumeIndicator(close=c, volume=v).on_balance_volume().fillna(0)
    
    macd = ta.trend.MACD(close=c)
    df["MACD"] = macd.macd().fillna(0)
    df["MACD_Signal"] = macd.macd_signal().fillna(0)
    df["MACD_Hist"] = macd.macd_diff().fillna(0)
    
    bb = ta.volatility.BollingerBands(close=c, window=20, window_dev=2)
    df["BB_High"] = bb.bollinger_hband().fillna(0)
    df["BB_Low"] = bb.bollinger_lband().fillna(0)
    
    df["Ichimoku_SpanA"] = ((((h.rolling(9).max() + l.rolling(9).min()) / 2 + (h.rolling(26).max() + l.rolling(26).min()) / 2) / 2).shift(26).fillna(0))
    df["Ichimoku_SpanB"] = (((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26).fillna(0))
    
    stoch = ta.momentum.StochasticOscillator(high=h, low=l, close=c, window=14, smooth_window=3)
    df["Stoch_K"] = stoch.stoch_signal().fillna(0)
    df["Stoch_D"] = df["Stoch_K"].rolling(3).mean().fillna(0)
    
    df["Close_line"] = c
    df["Open_line"] = df["Open"].squeeze()
    df["High_line"] = h
    df["Low_line"] = l
    df["Volume_line"] = v
    
    return df

@st.cache_data(ttl=300, show_spinner=False)
def fetch_watchlist_data(tickers):
    if not tickers:
        return {}
    tech_map = {}

    kwargs = {"progress": False}
    if USE_PROXY: kwargs['proxy'] = PROXY_IP
    
    group_data = yf.download(
        tickers, period="6mo", interval="1d", group_by="ticker", threads=True, **kwargs
    )

    for t in tickers:
        try:
            if isinstance(group_data.columns, pd.MultiIndex):
                df = group_data[t].copy()
            else:
                df = group_data.copy()
                
            df = df.dropna(how="all")
            df = process_technical_indicators(df)
            
            if df.empty or "Close_line" not in df.columns:
                tech_map[t] = {"Price": 0.0, "RSI": 0.0, "ST": 0.0, "1YearHigh": 0.0, "1YearLow": 0.0}
                continue
            
            c_price = float(df["Close_line"].iloc[-1])
            rsi_val = float(df["RSI"].iloc[-1])
            stoch_val = float(df["Stoch_K"].iloc[-1])
            y_high = float(df["High_line"].max())
            y_low = float(df["Low_line"].min())
            
            tech_map[t] = {
                "Price": c_price, 
                "RSI": round(rsi_val, 1), 
                "ST": round(stoch_val, 1),
                "1YearHigh": y_high, 
                "1YearLow": y_low
            }
        except Exception:
            tech_map[t] = {"Price": 0.0, "RSI": 0.0, "ST": 0.0, "1YearHigh": 0.0, "1YearLow": 0.0}
            
    return tech_map


# =======================================================================
# 🔥 핵심 데이터 수집 엔진 (KRX 연동)
# =======================================================================
@st.cache_data(ttl=86400, show_spinner=False)
def get_market_database(market_type):
    if "한국" in market_type:
        ticker_map = get_market_database_krx(market_type)
        if ticker_map:
            return ticker_map
        return {
            "005930.KS": "삼성전자", "000660.KS": "SK하이닉스", "373220.KS": "LG에너지솔루션",
            "207940.KS": "삼성바이오로직스", "005380.KS": "현대차", "000270.KS": "기아"
        }
    else:
        ticker_map = {}
        try:
            sp500 = fdr.StockListing("S&P500")
            if "Name" in sp500.columns:
                sp500 = sp500[~sp500["Name"].str.contains(r"(?i)acquisition|spac|warrant|unit|trust", regex=True)]
            for _, r in sp500.iterrows():
                if len(ticker_map) < 500:
                    ticker_map[str(r["Symbol"]).replace(".", "-")] = str(r["Name"])
            nasdaq = fdr.StockListing("NASDAQ")
            if "Name" in nasdaq.columns:
                nasdaq = nasdaq[~nasdaq["Name"].str.contains(r"(?i)acquisition|spac|warrant|unit|trust", regex=True)]
            for _, r in nasdaq.head(300).iterrows():
                tk = str(r["Symbol"]).replace(".", "-")
                if tk not in ticker_map and len(ticker_map) < 500:
                    ticker_map[tk] = str(r["Name"])
        except Exception as e:
            st.error(f"🚨 [미국 종목 수집 실패] {e}")
        return ticker_map

@st.cache_data(ttl=14400, show_spinner=False)
def build_database(market_type, timeframe="일봉"):
    tickers = list(get_market_database(market_type).keys())
    if not tickers:
        tickers = ["005930.KS"]

    p = "6mo"
    i = "1wk" if timeframe == "주봉" else "1d"

    kwargs = {"progress": False}
    if USE_PROXY: kwargs['proxy'] = PROXY_IP

    group_data = yf.download(
        tickers, period=p, interval=i, group_by="ticker", threads=True, **kwargs
    )

    all_data = {}
    for t in tickers:
        try:
            if isinstance(group_data.columns, pd.MultiIndex):
                df = group_data[t].copy()
            else:
                df = group_data.copy()
            
            df = df.dropna(how="all")
            df = process_technical_indicators(df)
            if not df.empty:
                all_data[t] = df
        except:
            continue
            
    return all_data

@st.cache_data(ttl=600, show_spinner=False)
def fetch_specific_timeframe_data(ticker, selection):
    if selection == "60분봉":
        p, i = "2mo", "60m"
    elif selection == "주봉":
        p, i = "6mo", "1wk"
    else:
        p, i = "6mo", "1d"

    try:
        kwargs = {"progress": False}
        if USE_PROXY: kwargs['proxy'] = PROXY_IP

        df = yf.download(ticker, period=p, interval=i, **kwargs)
        return process_technical_indicators(df)
    except:
        return pd.DataFrame()

# =======================================================================
# 🌐 네이버 금융 수급 연동 로직 (선택적 사용 - 필터링 된 소수 종목만 요청)
# =======================================================================
def check_investor_streak_naver(ticker, investor_type, total_days, buy_days):
    if ".KS" not in ticker and ".KQ" not in ticker:
        return False

    time.sleep(REQUEST_DELAY)

    code = ticker.split(".")[0]
    url = f"https://finance.naver.com/item/frgn.naver?code={code}&page=1"
    try:
        req_proxies = PROXY_DICT if USE_PROXY else None
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5, proxies=req_proxies)

        res.encoding = "euc-kr"
        df = pd.read_html(StringIO(res.text))[3].copy()
        df.columns = [
            "날짜", "종가", "전일비", "등락률", "거래량", "기관", "외국인", "보유주수", "보유율",
        ]
        df = df.dropna().copy()
        df["기관"] = pd.to_numeric(df["기관"], errors="coerce")
        df["외국인"] = pd.to_numeric(df["외국인"], errors="coerce")
        recent_df = df.head(total_days)

        if investor_type == "외인":
            return (recent_df["외국인"] > 0).sum() >= buy_days
        elif investor_type == "기관":
            return (recent_df["기관"] > 0).sum() >= buy_days
        elif investor_type == "양매수":
            return ((recent_df["외국인"] > 0).sum() >= buy_days) and (
                (recent_df["기관"] > 0).sum() >= buy_days
            )
    except:
        pass
    return False

# =======================================================================
# 📰 뉴스 수집
# =======================================================================
def fetch_news_rss(query, category):
    encoded_query = urllib.request.quote(f"{query} when:1d")
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        xml_data = get_urllib_opener().open(req, timeout=5).read()
        root = ET.fromstring(xml_data)
        news_list = []
        for item in root.findall(".//item")[:20]:
            title = item.find("title").text
            link = item.find("link").text
            pub_date = item.find("pubDate").text
            source_tag = item.find("source")
            source = source_tag.text if source_tag is not None else "Google News"
            desc = item.find("description").text
            clean_desc = re.sub("<[^<]+?>", "", desc)
            clean_desc = (
                clean_desc[:120] + "..." if len(clean_desc) > 120 else clean_desc
            )

            try:
                dt_obj = datetime.datetime.strptime(
                    pub_date, "%a, %d %b %Y %H:%M:%S %Z"
                )
                if "GMT" in pub_date or "UTC" in pub_date:
                    dt_obj += datetime.timedelta(hours=9)
                formatted_date = dt_obj.strftime("%Y-%m-%d %H:%M")
            except:
                formatted_date = pub_date

            news_list.append(
                {
                    "category": category,
                    "title": title,
                    "date": formatted_date,
                    "desc": clean_desc,
                    "source": source,
                    "link": link,
                }
            )
        return news_list
    except:
        return []


# =======================================================================
# 🌟 섹터 매핑 로직
# =======================================================================
SECTOR_DB_FILE = "sector_db.csv"

def load_sector_db():
    if os.path.exists(SECTOR_DB_FILE):
        try:
            df = pd.read_csv(SECTOR_DB_FILE)
            return dict(zip(df["Ticker"], df["Sector"]))
        except:
            return {}
    return {}

def save_sector_db(db_dict):
    df = pd.DataFrame(list(db_dict.items()), columns=["Ticker", "Sector"])
    df.to_csv(SECTOR_DB_FILE, index=False)

def translate_yf_sector(sec):
    if not isinstance(sec, str) or sec == "nan" or not sec.strip():
        return "미분류"
    s = sec.lower()
    if any(
        x in s for x in ["technology", "software", "semiconductor", "computer", "it"]
    ):
        return "정보기술 (IT)"
    if any(x in s for x in ["health", "medical", "pharma", "biotech", "life sciences"]):
        return "헬스케어"
    if any(x in s for x in ["financial", "bank", "insurance", "capital", "credit"]):
        return "금융"
    if any(
        x in s
        for x in [
            "consumer cyclical",
            "consumer discretionary",
            "auto",
            "apparel",
            "retail",
            "leisure",
            "hotel",
            "restaurant",
        ]
    ):
        return "임의소비재"
    if any(
        x in s
        for x in ["communication", "media", "telecom", "internet", "entertainment"]
    ):
        return "커뮤니케이션"
    if any(
        x in s
        for x in [
            "industrial",
            "aerospace",
            "defense",
            "machinery",
            "transport",
            "logistics",
            "building",
            "construction",
        ]
    ):
        return "산업재"
    if any(x in s for x in ["material", "chemical", "steel", "metal", "mining"]):
        return "소재"
    if any(x in s for x in ["energy", "oil", "gas"]):
        return "에너지"
    if any(
        x in s
        for x in ["defensive", "staple", "beverage", "tobacco", "food", "personal care"]
    ):
        return "필수소비재"
    if any(x in s for x in ["utility", "power", "water", "electricity"]):
        return "유틸리티"
    if any(x in s for x in ["real estate", "reit", "property"]):
        return "부동산"
    return "미분류"

@st.cache_data(ttl=86400, show_spinner=False)
def sync_market_sectors_v8(market_type):
    db = load_sector_db()
    name_db = get_market_database(market_type)
    tickers = list(name_db.keys())
    changed = False

    if "한국" in market_type:
        pass # 한국 시장 섹터는 아래의 자동 단어 맵핑으로 처리 (속도 최적화)
    else:
        try:
            sp500 = fdr.StockListing("S&P500")
            for _, r in sp500.iterrows():
                tk = str(r["Symbol"]).replace(".", "-")
                if tk in tickers and (tk not in db or db[tk] == "미분류"):
                    db[tk] = translate_yf_sector(str(r.get("Sector", "")))
                    changed = True
            nasdaq = fdr.StockListing("NASDAQ")
            for _, r in nasdaq.iterrows():
                tk = str(r["Symbol"]).replace(".", "-")
                if tk in tickers and (tk not in db or db[tk] == "미분류"):
                    db[tk] = translate_yf_sector(str(r.get("Industry", "")))
                    changed = True
        except:
            pass

    final_map = {}
    for tk in tickers:
        val = db.get(tk, "미분류")
        if val == "미분류":
            name = name_db.get(tk, "")
            n = str(name).replace(" ", "").upper()
            if any(
                x in n
                for x in [
                    "전자", "반도체", "전기", "컴퓨터", "소프트", "시스템", "디스플레이", 
                    "에스디아이", "하이닉스", "IT", "아이티", "테크", "솔루션", "페타시스", 
                    "ISC", "심텍", "주성", "이오테크닉스", "하나마이크론", "솔브레인", 
                    "동진쎄미켐", "HPSP", "한미반도체", "리노공업", "티씨케이", "원익", 
                    "비에이치", "에스에프에이", "테스", "티에스이", "RFHIC", "텍", 
                    "에스앤에스", "비츠로", "쏠리드", "비나"
                ]
            ):
                val = "정보기술 (IT)"
            elif any(
                x in n
                for x in [
                    "제약", "바이오", "약품", "생명과학", "메디칼", "메디컬", "헬스케어", 
                    "신약", "파마", "티슈진", "케어", "펩트론", "알테오젠", "셀트리온", 
                    "HLB", "유한양행", "녹십자", "종근당", "대웅", "루닛", "휴젤", 
                    "보로노이", "레고켐", "리가켐", "삼천당", "에스티팜", "팜", "메디톡스", 
                    "셀", "오스코", "네이처"
                ]
            ):
                val = "헬스케어"
            elif any(
                x in n
                for x in [
                    "통신", "텔레콤", "미디어", "엔터", "스튜디오", "네이버", "카카오", 
                    "게임", "컴투스", "크래프톤", "엔씨", "KT", "에스엠", "JYP", "하이브", 
                    "와이지", "디어유", "ENM", "드래곤", "펄어비스", "위메이드", "웹젠", 
                    "더존", "아프리카", "SOOP", "NC"
                ]
            ):
                val = "커뮤니케이션"
            elif any(
                x in n
                for x in [
                    "화학", "케미칼", "케미컬", "신소재", "스틸", "철강", "금속", 
                    "에코프로", "포스코", "POSCO", "머티리얼", "소재", "엘앤에프", 
                    "엔켐", "대주전자", "코스모", "고려아연", "풍산", "동국", "솔루스", 
                    "레이크", "나노", "TCC", "롯데에너지", "SKC"
                ]
            ):
                val = "소재"
            elif any(
                x in n
                for x in [
                    "건설", "중공업", "해양", "조선", "항공", "운송", "해운", "글로비스", 
                    "에어로", "로보틱스", "로봇", "방산", "물산", "오토", "모비스", 
                    "대한전선", "전선", "일렉트릭", "E&A", "엔지니어링", "로템", "LIG", 
                    "KAI", "한진", "팬오션", "HMM", "통운", "두산", "효성", "LS", "LX", 
                    "한화", "현대", "대우", "에스피지", "로지스틱스", "에스엠벡셀", "쎄트렉아이"
                ]
            ):
                val = "산업재"
            elif any(
                x in n
                for x in [
                    "은행", "금융", "지주", "증권", "보험", "생명", "화재", "해상", 
                    "캐피탈", "인베스트", "투자", "홀딩스", "페이", "뱅크", "우리기술", "파트너스"
                ]
            ):
                val = "금융"
            elif any(
                x in n
                for x in [
                    "식품", "제일제당", "푸드", "제과", "음료", "농심", "삼양", "생활건강", 
                    "KT&G", "담배", "동원", "하이트", "오리온", "풀무원", "달바글로벌", 
                    "화장품", "코스메틱", "뷰티", "로직스"
                ]
            ):
                val = "필수소비재"
            elif any(
                x in n
                for x in [
                    "리테일", "쇼핑", "호텔", "투어", "여행", "백화점", "자동차", 
                    "모터스", "타이어", "에이피알", "신라", "카지노", "파라다이스", 
                    "강원랜드", "GKL", "이마트", "BGF", "F&F", "코스맥스", "아모레", 
                    "클리오", "기아", "위아", "만도", "넥센", "금호", "코웨이", "신세계", "렌탈"
                ]
            ):
                val = "임의소비재"
            elif any(
                x in n
                for x in [
                    "이노베이션", "오일", "정유", "에너지", "에스오일", "S-OIL", "GS"
                ]
            ):
                val = "에너지"
            elif any(x in n for x in ["전력", "가스", "난방", "한국전력", "지역난방"]):
                val = "유틸리티"
            elif any(x in n for x in ["리츠", "부동산", "인프라"]):
                val = "부동산"
        final_map[tk] = val

    if changed:
        save_sector_db(db)

    return final_map

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_sectors_for_watchlist_v8(tickers):
    hard_map_kor = sync_market_sectors_v8("한국")
    hard_map_us = sync_market_sectors_v8("미국")
    return {
        t: hard_map_kor.get(t, "미분류")
        if (".KS" in t or ".KQ" in t)
        else hard_map_us.get(t, "미분류")
        for t in tickers
    }

# =======================================================================
# --- [UI] 대시보드 커널 ---
# =======================================================================
def toggle_news_state():
    st.session_state["show_news"] = not st.session_state.get("show_news", False)
    if st.session_state["show_news"] and "scraped_news" not in st.session_state:
        st.session_state["auto_fetch_news"] = True

def start_100b_dashboard():
    def reset_all_filters():
        defaults = {
            "k_market": "한국", "k_array": "조건없음", "k_ma_n": 20, "k_ma_cond": "조건없음",
            "k_ichi": "조건없음", "k_bb": "조건없음", "k_macd": "조건없음", "k_rsi": (0, 100),
            "k_stoch": "조건없음", "k_vol": "조건없음", "k_vol_n": 20, "k_inv_type": "조건없음",
            "k_inv_m": 5, "k_inv_n": 3, "k_vol_rank": False, "k_ma_s": 5, "k_ma_l": 120,
            "k_ma_c": "조건없음", "k_bb_sq": False, "k_bb_sq_n": 20, "k_bb_sq_pct": 5.0,
            "k_maup_n": 20, "k_maup_m": 5, "k_maup_cond": "조건없음", "k_sector": "조건없음",
            "k_drop_cond": False, "k_drop_target": 30, "k_drop_margin": 5,
        }
        for k, v in defaults.items():
            st.session_state[k] = v
        if "matched_stocks" in st.session_state:
            del st.session_state["matched_stocks"]

    st.set_page_config(page_title="나만의 주식 검색기 V6.1", layout="wide")

    if "selected_ticker" not in st.session_state:
        st.session_state["selected_ticker"] = "NONE"

    registered_tickers = get_watchlist_df()["Ticker"].tolist()

    st.markdown(
        """
        <style>
            [data-testid="stSidebarUserContent"] { padding-top: 0rem !important; margin-top: -40px !important; }
            [data-testid="stSidebarUserContent"] h3 { font-size: 15px !important; margin-top: -20px !important; margin-bottom: -10px !important; }
            .inline-label { font-size: 13px !important; font-weight: bold; color: #333333; margin-top: -10px !important; margin-bottom: 2px !important; }
            div[data-baseweb="select"] { font-size: 12px !important; } 
            div[data-baseweb="select"] > div { min-height: 40px !important; height: 40px !important; }
            [data-testid="stVerticalBlockBorderWrapper"] { padding: 5px 8px !important; margin-bottom: -20px !important; }
            .stButton button { min-height: 28px !important; height: 28px !important; font-size: 12px !important; padding: 0px 2px !important; white-space: nowrap !important; }
            hr { margin-top: 5px !important; margin-bottom: 5px !important; }
            [data-testid="stMarkdownContainer"] p { margin-bottom: 0px !important; }
            .stCheckbox { margin-top: 5px !important; }
            button[data-baseweb="tab"] { font-size: 16px !important; font-weight: bold !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("📈 100억 벌고 싶다 (V6.1 초고속 스캔)")
    st.divider()

    # 인베스팅닷컴 탭 삭제
    tab1, tab2 = st.tabs(
        ["🔍 초고속 검색기", "⭐ 나의 관심종목 (신규 추가 가능)"]
    )

    # =======================================================================
    # 🔍 탭 1: 검색기 메인 화면
    # =======================================================================
    with tab1:
        with st.sidebar:
            col_market, col_btn1, col_btn2 = st.columns([4, 3, 3], gap="small")
            with col_market:
                st.markdown(
                    '<div class="inline-label" style="margin-bottom: 5px;">시장</div>',
                    unsafe_allow_html=True,
                )
                market = st.selectbox(
                    "시장",
                    ["한국", "미국"],
                    label_visibility="collapsed",
                    key="k_market",
                )
            with col_btn1:
                st.markdown(
                    '<div class="inline-label" style="margin-bottom: 5px;">&nbsp;</div>',
                    unsafe_allow_html=True,
                )
                st.button(
                    "🧹필터", use_container_width=True, on_click=reset_all_filters
                )
            with col_btn2:
                st.markdown(
                    '<div class="inline-label" style="margin-bottom: 5px;">&nbsp;</div>',
                    unsafe_allow_html=True,
                )
                if st.button("🗑️캐시", use_container_width=True):
                    st.cache_data.clear()
                    st.rerun()
            st.divider()

            st.markdown("### 💾 나의 전략 저장소")
            saved_filters_dict = load_saved_filters()
            preset_names = list(saved_filters_dict.keys())

            with st.container(border=True):
                if preset_names:
                    sel_preset = st.selectbox(
                        "저장된 전략 불러오기",
                        ["선택하세요"] + preset_names,
                        label_visibility="collapsed",
                    )
                    c_load, c_del = st.columns(2, gap="small")
                    if c_load.button("📂 불러오기", use_container_width=True):
                        if sel_preset != "선택하세요":
                            preset_data = saved_filters_dict[sel_preset]
                            for k, v in preset_data.items():
                                st.session_state[k] = tuple(v) if k == "k_rsi" else v
                            st.rerun()
                    if c_del.button("🗑️ 삭제", use_container_width=True):
                        if sel_preset != "선택하세요":
                            delete_filter_preset(sel_preset)
                            st.rerun()
                else:
                    st.info("저장된 전략이 없습니다.")

                st.markdown("---")

                new_preset_name = st.text_input(
                    "현재 조건 이름 지정",
                    placeholder="예: 20선터치, 거래량 폭발",
                    label_visibility="collapsed",
                )
                if st.button("💾 현재 세팅 저장", use_container_width=True):
                    if new_preset_name.strip():
                        keys_to_save = [
                            "k_market", "k_array", "k_ma_n", "k_ma_cond", "k_ichi",
                            "k_bb", "k_macd", "k_rsi", "k_stoch", "k_vol", "k_vol_n",
                            "k_inv_type", "k_inv_m", "k_inv_n", "k_vol_rank", "k_ma_s",
                            "k_ma_l", "k_ma_c", "k_bb_sq", "k_bb_sq_n", "k_bb_sq_pct",
                            "k_maup_n", "k_maup_m", "k_maup_cond", "k_sector",
                            "k_drop_cond", "k_drop_target", "k_drop_margin"
                        ]
                        current_data = {}
                        for k in keys_to_save:
                            val = st.session_state.get(k)
                            if val is None:
                                if k == "k_rsi": val = (0, 100)
                                elif k in ["k_vol_rank", "k_bb_sq", "k_drop_cond"]: val = False
                                elif k in ["k_ma_n", "k_vol_n", "k_bb_sq_n", "k_maup_n"]: val = 20
                                elif k in ["k_ma_s", "k_inv_m", "k_maup_m", "k_drop_margin"]: val = 5
                                elif k == "k_ma_l": val = 120
                                elif k == "k_inv_n": val = 3
                                elif k == "k_bb_sq_pct": val = 5.0
                                elif k == "k_drop_target": val = 30
                                elif k == "k_market": val = "한국"
                                else: val = "조건없음"
                            current_data[k] = val

                        save_filter_preset(new_preset_name.strip(), current_data)
                        st.rerun()
                    else:
                        st.warning("저장할 이름을 입력해주세요.")
            st.divider()

            st.markdown("### 🚀 종목 스캔")
            scan_action_placeholder = st.empty()  
            st.divider()

            timeframe = "일봉"

            # 🔥 신규 기능: 1년 고저밴드 (Range) 위치 필터
            st.markdown("### 📉 고저 밴드 내 현재가 위치")
            with st.container(border=True):
                k_drop_cond = st.checkbox("🎯 1년 고저밴드 위치(%) 필터 적용", key="k_drop_cond")
                if k_drop_cond:
                    c1, c2 = st.columns(2, gap="small")
                    with c1:
                        st.number_input("목표 위치(%)", 1, 100, 30, key="k_drop_target", help="예: 30을 넣으면 최저점에서 30% 올라온 위치 탐색")
                    with c2:
                        st.number_input("오차 범위(±%)", 1, 50, 5, key="k_drop_margin", help="예: 5를 넣으면 25% ~ 35% 범위 탐색")

            st.markdown("### 📊 추세")
            c1, c2 = st.columns([35, 65], gap="small")
            with c1:
                st.markdown(
                    '<div class="inline-label">정/역배열</div>', unsafe_allow_html=True
                )
            with c2:
                array_cond = st.selectbox(
                    "정/역",
                    [
                        "조건없음",
                        "정배열 (5>20>60)",
                        "역배열 (5<20<60)",
                        "5>20 & 20<60",
                    ],
                    label_visibility="collapsed",
                    key="k_array",
                )
            with st.container(border=True):
                c1, c2 = st.columns(2, gap="small")
                with c1:
                    ma_n = st.number_input("이평선(N봉)", 1, 200, 20, key="k_ma_n")
                with c2:
                    ma_cond = st.selectbox(
                        "이평선 조건",
                        ["조건없음", "위", "아래", "터치"],
                        key="k_ma_cond",
                    )
            with st.container(border=True):
                st.markdown(
                    '<div class="inline-label" style="margin-bottom: 5px;">이평선 골든크로스</div>',
                    unsafe_allow_html=True,
                )
                c1, c2, c3 = st.columns([1, 1, 1.5], gap="small")
                with c1:
                    ma_short = st.number_input("단기", 1, 200, 5, key="k_ma_s")
                with c2:
                    ma_long = st.number_input("장기", 1, 200, 120, key="k_ma_l")
                with c3:
                    ma_cross_cond = st.selectbox(
                        "크로스조건",
                        ["조건없음", "적용"],
                        label_visibility="hidden",
                        key="k_ma_c",
                    )
            with st.container(border=True):
                st.markdown(
                    '<div class="inline-label" style="margin-bottom: 5px;">이평선 연속 우상향</div>',
                    unsafe_allow_html=True,
                )
                c1, c2, c3 = st.columns([1, 1, 1.5], gap="small")
                with c1:
                    ma_up_n = st.number_input("N일선", 1, 200, 20, key="k_maup_n")
                with c2:
                    ma_up_m = st.number_input("M일 연속", 1, 60, 5, key="k_maup_m")
                with c3:
                    ma_up_cond = st.selectbox(
                        "우상향조건",
                        ["조건없음", "적용"],
                        label_visibility="hidden",
                        key="k_maup_cond",
                    )

            st.markdown("### ⚡ 모멘텀")
            with st.container(border=True):
                c1, c2, c3 = st.columns([1, 1, 1], gap="small")
                with c1:
                    ichi_cond = st.selectbox(
                        "일목균형표", ["조건없음", "위", "아래"], key="k_ichi"
                    )
                with c2:
                    bb_cond = st.selectbox(
                        "볼린저밴드", ["조건없음", "상단", "중단", "하단"], key="k_bb"
                    )
                with c3:
                    macd_cond = st.selectbox(
                        "MACD", ["조건없음", "골든크로스", "0선돌파"], key="k_macd"
                    )

            with st.container(border=True):
                bb_squeeze_cond = st.checkbox(
                    "🎯 볼린저밴드(스퀴즈)",
                    value=st.session_state.get("k_bb_sq", False),
                    key="k_bb_sq",
                )
                if bb_squeeze_cond:
                    c1, c2 = st.columns(2, gap="small")
                    with c1:
                        st.number_input(
                            "유지 기간(N봉)",
                            5,
                            300,
                            st.session_state.get("k_bb_sq_n", 20),
                            key="k_bb_sq_n",
                        )
                    with c2:
                        st.number_input(
                            "수축 폭(%)",
                            1.0,
                            30.0,
                            st.session_state.get("k_bb_sq_pct", 5.0),
                            step=1.0,
                            key="k_bb_sq_pct",
                        )

            with st.container(border=True):
                c1, c2 = st.columns(2, gap="small")
                with c1:
                    st.markdown(
                        '<div class="inline-label" style="margin-bottom: 5px;">RSI</div>',
                        unsafe_allow_html=True,
                    )
                    rsi_min, rsi_max = st.slider(
                        "RSI 범위",
                        0,
                        100,
                        (0, 100),
                        label_visibility="collapsed",
                        key="k_rsi",
                    )
                    rsi_show = "적용"
                with c2:
                    st.markdown(
                        '<div class="inline-label" style="margin-bottom: 5px;">스토캐스틱</div>',
                        unsafe_allow_html=True,
                    )
                    stoch_cond = st.selectbox(
                        "스토캐스틱",
                        ["조건없음", "20이하 골든크로스"],
                        label_visibility="collapsed",
                        key="k_stoch",
                    )

            st.markdown("### 🏢 섹터 필터")
            with st.container(border=True):
                avail_sectors = [
                    "조건없음", "정보기술 (IT)", "금융", "헬스케어", "임의소비재",
                    "커뮤니케이션", "산업재", "필수소비재", "에너지", "소재", "유틸리티",
                    "부동산", "미분류",
                ]
                st.markdown(
                    '<div class="inline-label" style="margin-bottom: 5px;">섹터 선택</div>',
                    unsafe_allow_html=True,
                )
                sector_cond = st.selectbox(
                    "섹터", avail_sectors, label_visibility="collapsed", key="k_sector"
                )

            with st.container(border=True):
                vol_rank_1 = st.checkbox(
                    "🔥 최근 거래량 상위 20위",
                    value=st.session_state.get("k_vol_rank", False),
                    key="k_vol_rank",
                )

            st.markdown("### 🇰🇷 수급 & 거래량")
            with st.container(border=True):
                c1, c2 = st.columns(2, gap="small")
                with c1:
                    vol_cond = st.selectbox(
                        "거래량 폭발", ["조건없음", "150%", "200%", "300%"], key="k_vol"
                    )
                with c2:
                    vol_n = st.number_input("기준(N봉)", 1, 100, 20, key="k_vol_n")
            with st.container(border=True):
                st.markdown(
                    '<div class="inline-label" style="margin-bottom: 5px;">외인/기관 (M일 중 N일 매수)</div>',
                    unsafe_allow_html=True,
                )
                c1, c2, c3 = st.columns([1.5, 1, 1], gap="small")
                with c1:
                    investor_type = st.selectbox(
                        "주체",
                        ["조건없음", "외인", "기관", "양매수"],
                        label_visibility="collapsed",
                        key="k_inv_type",
                    )
                with c2:
                    investor_total_days = st.number_input(
                        "총(M)일",
                        1,
                        100,
                        5,
                        label_visibility="collapsed",
                        key="k_inv_m",
                    )
                with c3:
                    investor_buy_days = st.number_input(
                        "매수(N)일",
                        1,
                        100,
                        3,
                        label_visibility="collapsed",
                        key="k_inv_n",
                    )

            search_btn = scan_action_placeholder.button(
                "🚀 500종목 일봉 초고속 스캔 (실행)",
                use_container_width=True,
                type="primary",
            )

            if search_btn:
                st.session_state["selected_ticker"] = "NONE"
                with st.spinner(
                    f"글로벌 데이터({timeframe}) 스캔 중... (무거운 실적 수집 제거로 초고속!)"
                ):
                    db = build_database(market, timeframe)

                with st.spinner(
                    f"섹터 DB 점검 및 동기화 중... (최초 1회만 데이터 수집)"
                ):
                    sector_map = sync_market_sectors_v8(market)

                matched_stocks, debug_list = {}, []
                name_map = get_market_database(market)
                valid_investor_tickers = set(db.keys())

                if "한국" in market and investor_type != "조건없음":
                    with st.spinner(
                        f"수급 분석 중... (최근 {investor_total_days}일 중 {investor_buy_days}일 {investor_type})"
                    ):
                        passed_tickers = set()
                        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exc:
                            futures = {
                                exc.submit(
                                    check_investor_streak_naver,
                                    t,
                                    investor_type,
                                    investor_total_days,
                                    investor_buy_days,
                                ): t
                                for t in valid_investor_tickers
                            }
                            for f in as_completed(futures):
                                if f.result():
                                    passed_tickers.add(futures[f])
                        valid_investor_tickers = passed_tickers
                        if not valid_investor_tickers:
                            st.warning("설정한 수급 조건을 만족하는 종목이 없습니다.")

                for ticker, df in db.items():
                    if (
                        sector_cond != "조건없음"
                        and sector_map.get(ticker, "미분류") != sector_cond
                    ):
                        continue

                    if len(df) < 60:
                        continue
                    if (
                        "한국" in market
                        and investor_type != "조건없음"
                        and ticker not in valid_investor_tickers
                    ):
                        continue
                    if (
                        df["Volume_line"].iloc[-1] == 0
                        or df["Volume_line"].iloc[-2] == 0
                    ):
                        continue

                    year_high, year_low = 0, 0
                    band_position = 0
                    try:
                        last_year_df = df.tail(252 if timeframe == "일봉" else 52)
                        year_high = last_year_df["High_line"].max()
                        year_low = last_year_df["Low_line"].min()
                    except:
                        pass

                    latest, prev = df.iloc[-1], df.iloc[-2]
                    cp = float(latest["Close_line"])

                    # 🔥 완벽한 고저밴드 (Range) 위치 계산 로직
                    if year_high > 0 and (year_high - year_low) > 0:
                        band_position = ((cp - year_low) / (year_high - year_low)) * 100

                    # 🔥 신규 고저 밴드 위치 필터
                    if st.session_state.get("k_drop_cond", False):
                        t_ratio = st.session_state.get("k_drop_target", 30)
                        m_ratio = st.session_state.get("k_drop_margin", 5)
                        if year_high == 0 or not (t_ratio - m_ratio <= band_position <= t_ratio + m_ratio):
                            continue

                    if not (rsi_min <= latest["RSI"] <= rsi_max):
                        continue

                    if array_cond != "조건없음":
                        ma5, ma20, ma60 = (
                            df["Close_line"].rolling(5).mean().iloc[-1],
                            df["Close_line"].rolling(20).mean().iloc[-1],
                            df["Close_line"].rolling(60).mean().iloc[-1],
                        )
                        if "정배열 (5>20>60)" in array_cond and not (ma5 > ma20 > ma60):
                            continue
                        if "역배열 (5<20<60)" in array_cond and not (ma5 < ma20 < ma60):
                            continue
                        if array_cond == "5>20 & 20<60" and not (
                            ma5 > ma20 and ma20 < ma60
                        ):
                            continue

                    if ma_cond != "조건없음":
                        l_ma = float(df["Close_line"].rolling(ma_n).mean().iloc[-1])
                        if ma_cond == "위" and cp <= l_ma:
                            continue
                        if ma_cond == "아래" and cp >= l_ma:
                            continue
                        if ma_cond == "터치" and abs(cp - l_ma) / l_ma > 0.005:
                            continue
                    if ichi_cond != "조건없음":
                        s_max, s_min = (
                            max(latest["Ichimoku_SpanA"], latest["Ichimoku_SpanB"]),
                            min(latest["Ichimoku_SpanA"], latest["Ichimoku_SpanB"]),
                        )
                        if "위" in ichi_cond and cp <= s_max:
                            continue
                        if "아래" in ichi_cond and cp >= s_min:
                            continue
                    if bb_cond != "조건없음":
                        if bb_cond == "상단" and cp < latest["BB_High"] * 0.98:
                            continue
                        if bb_cond == "하단" and cp > latest["BB_Low"] * 1.02:
                            continue
                        if (
                            bb_cond == "중단"
                            and abs(cp - (latest["BB_High"] + latest["BB_Low"]) / 2)
                            / ((latest["BB_High"] + latest["BB_Low"]) / 2)
                            > 0.02
                        ):
                            continue

                    if st.session_state.get("k_bb_sq"):
                        ma20_line = df["Close_line"].rolling(20).mean()
                        bb_width_pct = (df["BB_High"] - df["BB_Low"]) / ma20_line * 100
                        sq_n = st.session_state.get("k_bb_sq_n", 20)
                        sq_pct = st.session_state.get("k_bb_sq_pct", 5.0)
                        if bb_width_pct.tail(sq_n).max() > sq_pct:
                            continue

                    if stoch_cond != "조건없음" and "20이하 골든크로스" in stoch_cond:
                        if not (
                            (
                                prev["Stoch_K"] <= prev["Stoch_D"]
                                and latest["Stoch_K"] > latest["Stoch_D"]
                            )
                            and prev["Stoch_K"] <= 20
                        ):
                            continue
                    if ma_cross_cond == "적용":
                        ma_s_prev, ma_s_curr = (
                            df["Close_line"].rolling(ma_short).mean().iloc[-2],
                            df["Close_line"].rolling(ma_short).mean().iloc[-1],
                        )
                        ma_l_prev, ma_l_curr = (
                            df["Close_line"].rolling(ma_long).mean().iloc[-2],
                            df["Close_line"].rolling(ma_long).mean().iloc[-1],
                        )
                        if not (ma_s_prev <= ma_l_prev and ma_s_curr > ma_l_curr):
                            continue

                    if ma_up_cond == "적용":
                        ma_up_series = df["Close_line"].rolling(ma_up_n).mean()
                        is_rising = True
                        for i in range(ma_up_m):
                            if len(ma_up_series) < i + 2 or not (
                                ma_up_series.iloc[-(i + 1)]
                                > ma_up_series.iloc[-(i + 2)]
                            ):
                                is_rising = False
                                break
                        if not is_rising:
                            continue

                    if macd_cond == "0선돌파" and not (
                        prev["MACD"] <= 0 and latest["MACD"] > 0
                    ):
                        continue
                    if macd_cond == "골든크로스" and not (
                        prev["MACD"] <= prev["MACD_Signal"]
                        and latest["MACD"] > latest["MACD_Signal"]
                    ):
                        continue

                    sliced_for_disp = df.tail(vol_n)
                    bg_mean_disp = (
                        sliced_for_disp["Volume_line"].iloc[:-2].mean()
                        if len(sliced_for_disp) > 2
                        else 0
                    )
                    recent_max_vol = max(latest["Volume_line"], prev["Volume_line"])
                    vol_ratio = (
                        (recent_max_vol / bg_mean_disp * 100) if bg_mean_disp > 0 else 0
                    )

                    if vol_cond != "조건없음":
                        recent_df = df.tail(vol_n)

                        bg_df = recent_df.iloc[:-2]
                        if bg_df.empty or bg_df["Volume_line"].mean() == 0:
                            continue

                        bg_mean = bg_df["Volume_line"].mean()
                        bg_max = bg_df["Volume_line"].max()

                        target_ratio = float(vol_cond.replace("%", ""))

                        if (recent_max_vol / bg_mean * 100) < target_ratio:
                            continue

                        if recent_max_vol < (bg_max * 1.5):
                            continue

                    matched_stocks[ticker] = df
                    debug_list.append(
                        {
                            "티커": ticker,
                            "종목명": name_map.get(ticker, ticker),
                            "현재가": f"{cp:,.0f}원"
                            if "한국" in market
                            else f"${cp:,.2f}",
                            "1년 최고": f"{year_high:,.0f}"
                            if pd.notna(year_high) and year_high > 0
                            else "0",
                            "1년 최저": f"{year_low:,.0f}"
                            if pd.notna(year_low) and year_low > 0
                            else "0",
                            "고저밴드": f"{band_position:.1f}%",
                            "RSI": round(latest["RSI"], 1),
                            "Stoch %K": round(latest["Stoch_K"], 1),
                            "거래량 비율": f"{vol_ratio:.1f}%",
                            "당일거래량": float(latest["Volume_line"]),
                        }
                    )

                if vol_rank_1 and len(debug_list) > 0:
                    debug_list.sort(key=lambda x: x.get("당일거래량", 0), reverse=True)
                    debug_list, top_tickers = (
                        debug_list[:20],
                        [d["티커"] for d in debug_list[:20]],
                    )
                    matched_stocks = {
                        k: v for k, v in matched_stocks.items() if k in top_tickers
                    }

                st.session_state.update(
                    {
                        "matched_stocks": matched_stocks if matched_stocks else "NONE",
                        "debug_list": debug_list,
                        "current_market": market,
                        "filtered_list": debug_list,
                    }
                )

        if st.session_state.get("matched_stocks") == "NONE":
            st.error("조건에 맞는 종목이 없습니다.")
        elif isinstance(
            st.session_state.get("matched_stocks"), dict
        ) and st.session_state.get("matched_stocks"):
            ms, dl, c_m = (
                st.session_state["matched_stocks"],
                st.session_state["debug_list"],
                st.session_state["current_market"],
            )
            n_map = get_market_database(c_m)
            sector_map = sync_market_sectors_v8(c_m)

            now_kst = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
            if now_kst.hour < 15 or (now_kst.hour == 15 and now_kst.minute < 30):
                data_time_str = f"{(now_kst - datetime.timedelta(days=1)).strftime('%Y년 %m월 %d일')} (장 마감 전 기준)"
            else:
                data_time_str = (
                    f"{now_kst.strftime('%Y년 %m월 %d일 %H시 %M분')} (장 마감 후 기준)"
                )
            st.info(f"🕒 데이터 기준 시점: {data_time_str}")

            kw = st.text_input("결과 내 검색", placeholder="삼성, AAPL 등").lower()
            filtered_list = [
                d for d in dl if kw in d["종목명"].lower() or kw in d["티커"].lower()
            ]

            if not filtered_list:
                st.warning("매칭되는 종목이 없습니다.")
            else:
                st.success(f"총 {len(filtered_list)}개 종목 매칭 성공!")

                export_dl = []
                for i, item in enumerate(filtered_list):
                    export_dl.append(
                        {
                            "순번": i + 1,
                            "티커": item["티커"],
                            "종목명": item["종목명"],
                            "섹터": sector_map.get(item["티커"], "미분류"),
                            "현재가": item["현재가"],
                            "1년고": item.get("1년 최고", "-"),
                            "1년저": item.get("1년 최저", "-"),
                            "고저밴드(%)": item.get("고저밴드", "0%"),
                            "RSI": float(item.get("RSI", 0)),
                            "ST": float(item.get("Stoch %K", 0)),
                            "거래량%": item.get("거래량 비율", "0%"),
                        }
                    )
                df_export = pd.DataFrame(export_dl)
                csv_data_tab1 = df_export.to_csv(index=False).encode("utf-8-sig")

                e_col1, e_col2 = st.columns([8, 2])
                with e_col2:
                    st.download_button(
                        "📥 검색결과 엑셀 다운로드",
                        csv_data_tab1,
                        "search_results.csv",
                        "text/csv",
                        use_container_width=True,
                    )

                col_ratio_tab1 = [1, 2, 1.5, 4, 2, 1.5, 1, 1, 1, 1, 1, 1.5]
                h_cols = st.columns(col_ratio_tab1)
                headers = [
                    "순번", "선택", "티커", "종목명", "섹터", "현재가", 
                    "1년고", "1년저", "고저밴드", "RSI", "ST", "거래량%",
                ]
                for i, h in enumerate(headers):
                    h_cols[i].write(f"**{h}**")
                st.divider()

                with st.container(height=400):
                    for i, item in enumerate(filtered_list):
                        cols = st.columns(col_ratio_tab1)
                        cols[0].write(i + 1)
                        b_cols = cols[1].columns([1, 1])
                        if b_cols[0].button(
                            "🔍분석", key=f"btn_anal_{i}_{item['티커']}"
                        ):
                            st.session_state["selected_ticker"] = item["티커"]

                        if item["티커"] in registered_tickers:
                            b_cols[1].button(
                                "🔴등록됨",
                                key=f"btn_reg_done_{i}_{item['티커']}",
                                disabled=True,
                                type="primary",
                            )
                        else:
                            if b_cols[1].button(
                                "💾등록", key=f"btn_reg_{i}_{item['티커']}"
                            ):
                                st.session_state[f"show_input_{item['티커']}"] = True

                        if st.session_state.get(f"show_input_{item['티커']}"):
                            i_cols = st.columns([1, 1, 1])
                            target1 = i_cols[0].number_input(
                                "1차",
                                key=f"price1_{item['티커']}",
                                label_visibility="collapsed",
                                placeholder="1차 매수가",
                            )
                            target2 = i_cols[1].number_input(
                                "2차",
                                key=f"price2_{item['티커']}",
                                label_visibility="collapsed",
                                placeholder="2차 매수가",
                            )
                            if i_cols[2].button("확정", key=f"save_{item['티커']}"):
                                save_to_watchlist_local(
                                    item["티커"], item["종목명"], target1, target2
                                )
                                st.session_state[f"show_input_{item['티커']}"] = False
                                st.rerun()

                        cols[2].write(item["티커"])
                        cols[3].write(item["종목명"])
                        sec_name = sector_map.get(item["티커"], "미분류")
                        cols[4].write(str(sec_name)[:12])
                        cols[5].write(item["현재가"])
                        cols[6].write(item.get("1년 최고", "-"))
                        cols[7].write(item.get("1년 최저", "-"))
                        cols[8].write(item.get("고저밴드", "0%"))
                        cols[9].write(str(item.get("RSI", 0)))
                        cols[10].write(str(item.get("Stoch %K", 0)))
                        cols[11].write(item.get("거래량 비율", "0%"))
                        st.divider()

                sel_tk = st.session_state["selected_ticker"]
                if sel_tk != "NONE":
                    st.divider()
                    st.subheader(
                        f"📊 {sel_tk} ({n_map.get(sel_tk, '')}) 종합 투자 분석"
                    )

                    tf = st.radio(
                        "시간 축",
                        ["일봉", "주봉", "60분봉"],
                        horizontal=True,
                        key="time_frame_radio",
                    )
                    df = fetch_specific_timeframe_data(sel_tk, tf)

                    if df.empty:
                        st.error("데이터 로드 실패")
                    else:
                        active_subplots = []
                        if rsi_show == "적용":
                            active_subplots.append("RSI")
                        if stoch_cond != "조건없음":
                            active_subplots.append("STOCH")
                        if macd_cond != "조건없음":
                            active_subplots.append("MACD")

                        total_rows = 1 + len(active_subplots)
                        row_heights = (
                            [1.0]
                            if total_rows == 1
                            else [0.5]
                            + [0.5 / len(active_subplots)] * len(active_subplots)
                        )
                        specs = [[{"secondary_y": True}]] + [[{}]] * len(
                            active_subplots
                        )

                        fig = make_subplots(
                            rows=total_rows,
                            cols=1,
                            shared_xaxes=True,
                            vertical_spacing=0.03,
                            row_heights=row_heights,
                            specs=specs,
                        )
                        idx = df.index.astype(str) if tf == "60분봉" else df.index

                        fig.add_trace(
                            go.Candlestick(
                                x=idx,
                                open=df["Open_line"],
                                high=df["High_line"],
                                low=df["Low_line"],
                                close=df["Close_line"],
                                name="캔들",
                            ),
                            row=1,
                            col=1,
                        )
                        vc = [
                            "rgba(255,50,50,0.8)" if c >= o else "rgba(50,50,255,0.8)"
                            for o, c in zip(df["Open_line"], df["Close_line"])
                        ]
                        fig.add_trace(
                            go.Bar(
                                x=idx,
                                y=df["Volume_line"],
                                marker_color=vc,
                                name="거래량",
                            ),
                            row=1,
                            col=1,
                            secondary_y=True,
                        )

                        if bb_cond != "조건없음" or st.session_state.get("k_bb_sq"):
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["BB_High"],
                                    line=dict(color="#8A2BE2", dash="dot"),
                                    name="BB상단",
                                ),
                                row=1,
                                col=1,
                            )
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["BB_Low"],
                                    line=dict(color="#8A2BE2", dash="dot"),
                                    fill="tonexty",
                                    fillcolor="rgba(138,43,226,0.05)",
                                    name="BB하단",
                                ),
                                row=1,
                                col=1,
                            )

                        if ichi_cond != "조건없음":
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["Ichimoku_SpanA"],
                                    line=dict(color="#00FA9A", width=1),
                                    name="일목A",
                                ),
                                row=1,
                                col=1,
                            )
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["Ichimoku_SpanB"],
                                    line=dict(color="#FA8072", width=1),
                                    fill="tonexty",
                                    fillcolor="rgba(250,128,114,0.1)",
                                    name="일목B",
                                ),
                                row=1,
                                col=1,
                            )
                        if array_cond != "조건없음":
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["Close_line"].rolling(5).mean(),
                                    line=dict(color="#FF1493", width=1.5),
                                    name="5이평",
                                ),
                                row=1,
                                col=1,
                            )
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["Close_line"].rolling(20).mean(),
                                    line=dict(color="#FFD700", width=1.5),
                                    name="20이평",
                                ),
                                row=1,
                                col=1,
                            )
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["Close_line"].rolling(60).mean(),
                                    line=dict(color="#00BFFF", width=1.5),
                                    name="60이평",
                                ),
                                row=1,
                                col=1,
                            )
                        if ma_cond != "조건없음":
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["Close_line"].rolling(ma_n).mean(),
                                    line=dict(color="#00FF00", width=2),
                                    name=f"{ma_n}이평",
                                ),
                                row=1,
                                col=1,
                            )
                        if ma_cross_cond == "적용":
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["Close_line"].rolling(ma_short).mean(),
                                    line=dict(color="#00FFFF", width=2),
                                    name=f"{ma_short}단기",
                                ),
                                row=1,
                                col=1,
                            )
                            fig.add_trace(
                                go.Scatter(
                                    x=idx,
                                    y=df["Close_line"].rolling(ma_long).mean(),
                                    line=dict(color="#FF4500", width=2),
                                    name=f"{ma_long}장기",
                                ),
                                row=1,
                                col=1,
                            )

                        current_row = 2
                        for subplot in active_subplots:
                            if subplot == "RSI":
                                fig.add_trace(
                                    go.Scatter(
                                        x=idx,
                                        y=df["RSI"],
                                        line=dict(color="purple"),
                                        name="RSI",
                                    ),
                                    row=current_row,
                                    col=1,
                                )
                                fig.add_hline(
                                    y=70,
                                    line_dash="dot",
                                    line_color="orange",
                                    row=current_row,
                                    col=1,
                                )
                                fig.add_hline(
                                    y=30,
                                    line_dash="dot",
                                    line_color="dodgerblue",
                                    row=current_row,
                                    col=1,
                                )
                                fig.update_yaxes(
                                    title_text="<b>RSI</b>",
                                    title_font=dict(size=12, color="purple"),
                                    range=[0, 100],
                                    fixedrange=True,
                                    row=current_row,
                                    col=1,
                                )
                            elif subplot == "STOCH":
                                fig.add_trace(
                                    go.Scatter(
                                        x=idx,
                                        y=df["Stoch_K"],
                                        line=dict(color="darkcyan"),
                                        name="%K",
                                    ),
                                    row=current_row,
                                    col=1,
                                )
                                fig.add_trace(
                                    go.Scatter(
                                        x=idx,
                                        y=df["Stoch_D"],
                                        line=dict(color="chocolate", dash="dot"),
                                        name="%D",
                                    ),
                                    row=current_row,
                                    col=1,
                                )
                                fig.add_hline(
                                    y=80,
                                    line_dash="dot",
                                    line_color="red",
                                    row=current_row,
                                    col=1,
                                )
                                fig.add_hline(
                                    y=20,
                                    line_dash="dot",
                                    line_color="green",
                                    row=current_row,
                                    col=1,
                                )
                                fig.update_yaxes(
                                    title_text="<b>STOCH</b>",
                                    title_font=dict(size=12, color="darkcyan"),
                                    range=[0, 100],
                                    fixedrange=True,
                                    row=current_row,
                                    col=1,
                                )
                            elif subplot == "MACD":
                                fig.add_trace(
                                    go.Bar(
                                        x=idx,
                                        y=df["MACD_Hist"],
                                        marker_color="gray",
                                        name="MACD Hist",
                                    ),
                                    row=current_row,
                                    col=1,
                                )
                                fig.add_trace(
                                    go.Scatter(
                                        x=idx,
                                        y=df["MACD"],
                                        line=dict(color="blue"),
                                        name="MACD",
                                    ),
                                    row=current_row,
                                    col=1,
                                )
                                fig.add_trace(
                                    go.Scatter(
                                        x=idx,
                                        y=df["MACD_Signal"],
                                        line=dict(color="orange", dash="dot"),
                                        name="Signal",
                                    ),
                                    row=current_row,
                                    col=1,
                                )
                                fig.update_yaxes(
                                    title_text="<b>MACD</b>",
                                    title_font=dict(size=12, color="blue"),
                                    row=current_row,
                                    col=1,
                                )
                            current_row += 1

                        fig.update_yaxes(
                            title_text="<b>주가</b>", row=1, col=1, secondary_y=False
                        )
                        fig.update_yaxes(
                            title_text="<b>거래량</b>",
                            showgrid=False,
                            range=[0, df["Volume_line"].max() * 5],
                            fixedrange=True,
                            row=1,
                            col=1,
                            secondary_y=True,
                        )
                        if tf == "60분봉":
                            for r in range(1, total_rows + 1):
                                fig.update_xaxes(
                                    type="category", nticks=20, row=r, col=1
                                )
                        fig.update_xaxes(showticklabels=True, row=total_rows, col=1)
                        fig.update_layout(
                            height=max(600, 400 + (len(active_subplots) * 200)),
                            hovermode="x unified",
                            dragmode="pan",
                            margin=dict(l=80, r=40, t=40, b=40),
                            xaxis_rangeslider_visible=False,
                        )
                        st.plotly_chart(
                            fig, use_container_width=True, config={"scrollZoom": True}
                        )
                        st.divider()

                        
    # =======================================================================
    # ⭐ 탭 2: 관심종목 관리 화면
    # =======================================================================
    with tab2:
        st.subheader("⭐ 나의 관심종목 포트폴리오")
        st.write(
            "등록하신 종목들의 실시간 지표와 1/2차 분할 매수 타점을 관리할 수 있습니다."
        )

        with st.expander("➕ 리스트에 없는 새로운 종목 추가하기", expanded=False):
            st.markdown(
                "야후 파이낸스에 상장된 **전 세계 모든 주식/ETF (500개 풀 외 전부)**를 무제한 추가할 수 있습니다.\n\n"
                "- **티커를 아는 경우**: `OKLO`, `TSLA`, `005930.KS` 등 티커를 직접 입력하면 즉시 추가됩니다.\n"
                "- **종목명만 아는 경우**: 한국/미국 주식은 이름만 적어도 전체 시장 리스트를 뒤져서 자동으로 티커를 찾아줍니다."
            )
            c_add1, c_add2 = st.columns([7, 3])
            with c_add1:
                custom_input = st.text_input(
                    "티커(기호) 또는 종목명 입력", 
                    placeholder="예: OKLO, TSLA, 또는 카카오뱅크",
                    label_visibility="collapsed"
                )
            with c_add2:
                if st.button("🌟 관심종목 추가", use_container_width=True, type="primary"):
                    if custom_input.strip():
                        search_term = custom_input.strip()
                        search_term_upper = search_term.upper()
                        
                        # 🔥 수정된 부분: KRX 전체 2500개 딕셔너리 호출 (0.001초 소요)
                        rev_map_kr = get_krx_full_search_map() # {"삼성전자": "005930.KS"}
                        name_map_kr = {v: k for k, v in rev_map_kr.items()} # {"005930.KS": "삼성전자"}
                        
                        name_map_us = get_market_database("미국")
                        rev_map_us = {v: k for k, v in name_map_us.items()}
                        
                        final_ticker = ""
                        final_name = ""
                        
                        if search_term in rev_map_kr:
                            final_ticker = rev_map_kr[search_term]
                            final_name = search_term
                        elif search_term in rev_map_us:
                            final_ticker = rev_map_us[search_term]
                            final_name = search_term
                        else:
                            final_ticker = search_term_upper
                            if final_ticker in name_map_kr:
                                final_name = name_map_kr[final_ticker]
                            elif final_ticker in name_map_us:
                                final_name = name_map_us[final_ticker]
                            else:
                                final_name = search_term_upper  

                        with st.spinner(f"'{final_ticker}' 정보 검색 중..."):
                            try:
                                check_df = yf.download(final_ticker, period="1d", progress=False)
                                if not check_df.empty:
                                    if final_name == final_ticker:
                                        try:
                                            fetched_name = yf.Ticker(final_ticker).info.get('shortName', final_name)
                                        except:
                                            fetched_name = final_name
                                    else:
                                        fetched_name = final_name
                                        
                                    save_to_watchlist_local(final_ticker, fetched_name, 0.0, 0.0)
                                    st.rerun()
                                else:
                                    st.error("❌ 야후 파이낸스에서 찾을 수 없는 종목입니다. 티커 형식을 다시 확인해 주세요.")
                            except Exception as e:
                                st.error("🚨 데이터를 가져오는 중 오류가 발생했습니다.")
                    else:
                        st.warning("종목명이나 티커를 입력해 주세요.")

        if "show_news" not in st.session_state:
            st.session_state["show_news"] = False

        c_news1, c_news2 = st.columns([8, 2])
        btn_text = (
            "⬇️ 뉴스 닫기"
            if st.session_state.get("show_news")
            else "📰 관심종목 실시간 뉴스 스크랩 (열기)"
        )

        with c_news1:
            st.button(
                btn_text,
                use_container_width=True,
                type="primary",
                on_click=toggle_news_state,
            )

        refresh_news = False
        with c_news2:
            if st.session_state.get("show_news"):
                refresh_news = st.button("🔄 새로고침", use_container_width=True)

        if st.session_state.get("show_news") and (
            refresh_news or st.session_state.pop("auto_fetch_news", False)
        ):
            if "scraped_news" in st.session_state:
                del st.session_state["scraped_news"]

            with st.spinner("네이버, 구글 뉴스 데이터망 최신화 중..."):
                all_news = []
                df_watch = get_watchlist_df()
                
                queries = [
                    ("거시경제 OR 금리인상 OR 통화정책", "🌐 거시경제/정책"),
                    ("달러 환율 OR 환율 전망", "💵 환율"),
                    ("국제유가 OR WTI", "🛢️ 유가"),
                    ("비트코인 OR 암호화폐", "🪙 암호화폐"),
                ]
                if not df_watch.empty:
                    for nm in df_watch["Name"]:
                        queries.append(
                            (f"{nm} (공시 OR 실적 OR 특징주)", f"🏢 {nm} 관련뉴스")
                        )

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exc:
                    futures = [exc.submit(fetch_news_rss, q[0], q[1]) for q in queries]
                    for f in as_completed(futures):
                        all_news.extend(f.result())

                unique_news = []
                for news in all_news:
                    is_dup = False
                    for un in unique_news:
                        similarity = difflib.SequenceMatcher(
                            None,
                            news["title"].replace(" ", ""),
                            un["title"].replace(" ", ""),
                        ).ratio()
                        if similarity > 0.6:
                            is_dup = True
                            break
                    if not is_dup:
                        unique_news.append(news)

                unique_news.sort(key=lambda x: x["date"], reverse=True)
                st.session_state["scraped_news"] = unique_news

        if st.session_state.get("show_news"):
            if "scraped_news" in st.session_state and st.session_state["scraped_news"]:
                st.markdown(
                    "#### 📬 수집된 최신 뉴스 브리핑 (최근 24시간 이내 / 중복 제거 완료)"
                )
                with st.container(height=700, border=True):
                    if len(st.session_state["scraped_news"]) == 0:
                        st.write("새로운 뉴스가 없습니다.")
                    else:
                        for news in st.session_state["scraped_news"]:
                            st.markdown(
                                f"**[{news['category']}] [{news['title']}]({news['link']})**"
                            )
                            st.caption(f"🗓️ {news['date']} | 🗞️ {news['source']}")
                            st.write(f"> {news['desc']}")
                            st.divider()
        else:
            now_kst = datetime.datetime.now(pytz.timezone("Asia/Seoul"))

            if 9 <= now_kst.hour < 15 or (now_kst.hour == 15 and now_kst.minute <= 30):
                status_text = "장중 실시간 (야후 파이낸스 15~20분 지연 반영)"
            elif now_kst.hour < 9:
                status_text = "장 시작 전 (전일 종가 기준)"
            else:
                status_text = "장 마감 (당일 종가 기준)"

            data_time_str = f"{now_kst.strftime('%Y년 %m월 %d일 %H:%M:%S')} ({status_text})"

            info_col, btn_col = st.columns([8.5, 1.5])
            with info_col:
                st.info(f"🕒 데이터 기준 시점: {data_time_str}")
            with btn_col:
                if st.button("🔄 현재가 갱신", use_container_width=True):
                    fetch_watchlist_data.clear() 
                    st.rerun()

            df_watch = get_watchlist_df()
            
            if df_watch.empty:
                st.info("등록된 관심종목이 없습니다. 검색창으로 돌아가 종목을 등록해 주세요.")
            else:
                tickers = df_watch["Ticker"].tolist()
                with st.spinner("실시간 데이터 불러오는 중..."):
                    tech_map = fetch_watchlist_data(tickers)
                    sector_map_watch = fetch_sectors_for_watchlist_v8(tickers)

                    st.markdown("##### 🗂️ 포트폴리오 정렬 및 내보내기")
                    sc1, sc2 = st.columns([2, 8])
                    with sc1:
                        sort_by = st.selectbox(
                            "정렬 기준",
                            [
                                "1차매수 근접도(%)",
                                "종목명",
                                "현재가",
                                "등록일 (최신순)",
                                "고저밴드(%)",
                                "RSI",
                                "ST",
                            ],
                            label_visibility="collapsed",
                        )
                    with sc2:
                        sort_order = st.radio(
                            "정렬 방식",
                            ["내림차순 (큰 값부터)", "오름차순 (작은 값부터)"],
                            horizontal=True,
                            label_visibility="collapsed",
                        )
                    st.write("")

                    display_rows = []
                    for idx, row in df_watch.iterrows():
                        tk, nm, dt = row["Ticker"], row["Name"], row.get("Date", "N/A")
                        tg1 = float(row["Target1"]) if "Target1" in row else 0.0
                        tg2 = float(row["Target2"]) if "Target2" in row else 0.0

                        price = tech_map.get(tk, {}).get("Price", 0)
                        rsi = tech_map.get(tk, {}).get("RSI", 0)
                        stoch = tech_map.get(tk, {}).get("ST", 0)
                        y_high = tech_map.get(tk, {}).get("1YearHigh", 0)
                        y_low = tech_map.get(tk, {}).get("1YearLow", 0)
                        
                        band_pos = 0
                        if y_high > 0 and (y_high - y_low) > 0:
                            band_pos = ((price - y_low) / (y_high - y_low)) * 100

                        diff1_pct = (
                            ((tg1 - price) / price) * 100
                            if price > 0 and tg1 > 0
                            else -9999
                        )

                        display_rows.append(
                            {
                                "tk": tk,
                                "nm": nm,
                                "dt": dt,
                                "tg1": tg1,
                                "tg2": tg2,
                                "price": price,
                                "rsi": rsi,
                                "stoch": stoch,
                                "band_pos": band_pos,
                                "diff1_pct": diff1_pct,
                                "sector": sector_map_watch.get(tk, "미분류"),
                            }
                        )

                    rev = sort_order == "내림차순 (큰 값부터)"
                    if sort_by == "종목명":
                        display_rows.sort(key=lambda x: x["nm"], reverse=rev)
                    elif sort_by == "등록일 (최신순)":
                        display_rows.sort(key=lambda x: x["dt"], reverse=rev)
                    elif sort_by == "현재가":
                        display_rows.sort(key=lambda x: x["price"], reverse=rev)
                    elif sort_by == "1차매수 근접도(%)":
                        display_rows.sort(key=lambda x: x["diff1_pct"], reverse=rev)
                    elif sort_by == "고저밴드(%)":
                        display_rows.sort(key=lambda x: x["band_pos"], reverse=rev)
                    elif sort_by == "RSI":
                        display_rows.sort(key=lambda x: x["rsi"], reverse=rev)
                    elif sort_by == "ST":
                        display_rows.sort(key=lambda x: x["stoch"], reverse=rev)

                    export_df = pd.DataFrame(display_rows)[
                        [
                            "nm",
                            "tk",
                            "sector",
                            "price",
                            "tg1",
                            "diff1_pct",
                            "tg2",
                            "band_pos",
                            "rsi",
                            "stoch",
                            "dt",
                        ]
                    ]
                    export_df.columns = [
                        "종목명",
                        "티커",
                        "섹터",
                        "현재가",
                        "1차매수가",
                        "1차매수_근접도(%)",
                        "2차매수가",
                        "고저밴드(%)",
                        "RSI",
                        "STOCH",
                        "등록일",
                    ]
                    csv_data = export_df.to_csv(index=False).encode("utf-8-sig")

                    e_col1, e_col2 = st.columns([8, 2])
                    with e_col2:
                        st.download_button(
                            label="📥 엑셀(CSV) 내보내기",
                            data=csv_data,
                            file_name=f"watchlist_{datetime.datetime.now().strftime('%Y%m%d')}.csv",
                            mime="text/csv",
                            use_container_width=True,
                        )

                    # 🔥 탭 2(관심종목) 열 너비 조절용 리스트
                    col_ratio_tab2 = [2.0, 1, 1, 1.0, 0.8, 0.8, 0.6, 0.5, 0.5, 1.7]
                    hc = st.columns(col_ratio_tab2)
                    hc[0].write("**종목명**")
                    hc[1].write("**등록일**")
                    hc[2].write("**섹터**")
                    hc[3].write("**현재가**")
                    hc[4].write("**1차매수**")
                    hc[5].write("**2차매수**")
                    hc[6].write("**고저밴드**")
                    hc[7].write("**RSI**")
                    hc[8].write("**ST**")
                    hc[9].write("**관리(수정/삭제)**")
                    st.divider()

                    with st.container(height=600):
                        for item in display_rows:
                            tk, nm, dt = item["tk"], item["nm"], item["dt"]
                            tg1, tg2, price = item["tg1"], item["tg2"], item["price"]
                            rsi, stoch, band_pos = (
                                item["rsi"],
                                item["stoch"],
                                item["band_pos"],
                            )
                            diff1_pct = item["diff1_pct"]

                            cc = st.columns(col_ratio_tab2)
                            cc[0].write(f"**{nm}**\n({tk})")
                            cc[1].write(dt)
                            cc[2].write(str(item.get("sector", "미분류"))[:12])

                            price_str = (
                                f"{price:,.0f}원" if "KS" in tk or "KQ" in tk else f"${price:,.2f}"
                            )

                            if price > 0:
                                if tg1 > 0:
                                    if price <= tg1 or diff1_pct >= -0.5:
                                        cc[3].markdown(
                                            f"<span style='background-color: #ff4b4b; color: white; font-weight: bold; padding: 3px 6px; border-radius: 4px;'>🚨 {price_str}</span>",
                                            unsafe_allow_html=True,
                                        )
                                    elif diff1_pct >= -3.0:
                                        cc[3].markdown(
                                            f"<span style='background-color: #ffd700; color: black; font-weight: bold; padding: 3px 6px; border-radius: 4px;'>🎯 {price_str}</span>",
                                            unsafe_allow_html=True,
                                        )
                                    else:
                                        cc[3].write(price_str)
                                else:
                                    cc[3].write(price_str)
                            else:
                                cc[3].write("데이터 없음")

                            if tg1 > 0:
                                color1, sign1 = (
                                    ("#ff4b4b", "+")
                                    if diff1_pct > 0
                                    else ("#00bfff", "")
                                )
                                tg1_fmt = (
                                    f"{tg1:,.0f}" if tg1 > 1000 else f"{tg1:,.2f}"
                                )
                                cc[4].markdown(
                                    f"<span>{tg1_fmt}</span> <span style='color:{color1}; font-size:12px; font-weight:bold;'>({sign1}{diff1_pct:.2f}%)</span>",
                                    unsafe_allow_html=True,
                                )
                            else:
                                cc[4].write("0")

                            tg2_fmt = f"{tg2:,.0f}" if tg2 > 1000 else f"{tg2:,.2f}"
                            
                            if tg2 > 0 and tg1 > 0 and price < tg1 and price > 0:
                                diff2_pct = ((tg2 - price) / price) * 100
                                color2, sign2 = (
                                    ("#ff4b4b", "+")
                                    if diff2_pct > 0
                                    else ("#00bfff", "")
                                )
                                cc[5].markdown(
                                    f"<span>{tg2_fmt}</span> <span style='color:{color2}; font-size:12px; font-weight:bold;'>({sign2}{diff2_pct:.2f}%)</span>",
                                    unsafe_allow_html=True,
                                )
                            else:
                                cc[5].write(tg2_fmt)

                            cc[6].write(f"{band_pos:.1f}%")
                            cc[7].write(f"{rsi}")
                            cc[8].write(f"{stoch}")

                            mc1, mc2, mc3, mc4 = cc[9].columns([1, 1, 0.8, 0.8])
                            new_tg1 = mc1.number_input(
                                "1차",
                                value=tg1,
                                key=f"edit1_{tk}",
                                label_visibility="collapsed",
                            )
                            new_tg2 = mc2.number_input(
                                "2차",
                                value=tg2,
                                key=f"edit2_{tk}",
                                label_visibility="collapsed",
                            )
                            if mc3.button("수정", key=f"btn_edit_{tk}"):
                                update_target_price(tk, new_tg1, new_tg2)
                                st.success("수정 완료!")
                                st.rerun()
                            if mc4.button("삭제", key=f"btn_del_{tk}"):
                                delete_from_watchlist(tk)
                                st.error("삭제 완료!")
                                st.rerun()
                            st.divider()

if __name__ == "__main__":
    if "streamlit" not in sys.modules and not sys.argv[0].endswith("streamlit"):
        print("\n" + "="*60)
        print("🚨 주의: 일반 파이썬 명령어로 실행할 수 없습니다!")
        print("💡 터미널(Shell)에 아래 명령어를 입력하여 실행해주세요:\n\n    👉  streamlit run main.py\n")
        print("="*60 + "\n")
    else:
        start_100b_dashboard()
