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
    conn = st.connection("gsheets", type=GSheetsConnection)
    try:
        df = conn.read(worksheet="관심종목", ttl=0)
        if df.empty or "Ticker" not in df.columns:
            return pd.DataFrame(columns=["Ticker", "Name", "Target1", "Target2", "Date"])
        return df
    except Exception:
        return pd.DataFrame(columns=["Ticker", "Name", "Target1", "Target2", "Date"])

def save_watchlist_df(df):
    conn = st.connection("gsheets", type=GSheetsConnection)
    conn.update(worksheet="관심종목", data=df)

def save_to_watchlist_local(ticker, name, target1, target2):
    df = get_watchlist_df()
    df_new = pd.DataFrame({
        "Ticker": [ticker], "Name": [name], "Target1": [target1],
        "Target2": [target2], "Date": [datetime.datetime.now().strftime("%Y-%m-%d")]
    })
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
        tickers, period="1y", interval="1d", group_by="ticker", threads=True, **kwargs
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
            
            tech_map[t] = {
                "Price": float(df["Close_line"].iloc[-1]), 
                "RSI": round(float(df["RSI"].iloc[-1]), 1), 
                "ST": round(float(df["Stoch_K"].iloc[-1]), 1),
                "1YearHigh": float(df["High_line"].max()), 
                "1YearLow": float(df["Low_line"].min())
            }
        except Exception:
            tech_map[t] = {"Price": 0.0, "RSI": 0.0, "ST": 0.0, "1YearHigh": 0.0, "1YearLow": 0.0}
            
    return tech_map

# =======================================================================
# 🔥 통합 데이터 수집 엔진 (구글 시트 및 실시간 ETF 대응)
# =======================================================================
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sheet_data(sheet_name):
    conn = st.connection("gsheets", type=GSheetsConnection)
    try:
        df = conn.read(worksheet=sheet_name, ttl=3600)
        if df.empty: return {}
        
        data_dict = {}
        for _, row in df.iterrows():
            ticker = str(row.get("Ticker", ""))
            if ticker:
                data_dict[ticker] = {
                    "Name": str(row.get("Name", "")),
                    "PER": float(row.get("PER", 0.0)) if pd.notna(row.get("PER", 0.0)) else 0.0,
                    "PBR": float(row.get("PBR", 0.0)) if pd.notna(row.get("PBR", 0.0)) else 0.0,
                    "Foreigner": float(row.get("Foreigner", 0.0)) if pd.notna(row.get("Foreigner", 0.0)) else 0.0,
                }
        return data_dict
    except Exception as e:
        return {}

@st.cache_data(ttl=86400, show_spinner=False)
def get_krx_full_search_map():
    try:
        df = fdr.StockListing('KRX')
        df = df[~df['Name'].str.contains('스팩|우B|우$|우선주', regex=True, na=False)]
        search_map = {}
        for _, row in df.iterrows():
            code = str(row["Code"]).zfill(6)
            mkt = str(row.get("Market", ""))
            suffix = ".KS" if "KOSPI" in mkt.upper() else ".KQ"
            search_map[row["Name"]] = f"{code}{suffix}"
        return search_map
    except Exception:
        return {}

@st.cache_data(ttl=86400, show_spinner=False)
def get_market_database(market_type, asset_type="일반 주식"):
    if asset_type == "일반 주식":
        if "한국" in market_type:
            krx_data = fetch_sheet_data("KRX_DATA")
            if krx_data: return {k: v["Name"] for k, v in krx_data.items()}
            return {"005930.KS": "삼성전자"}
        else:
            us_data = fetch_sheet_data("US_DATA")
            if us_data: return {k: v["Name"] for k, v in us_data.items()}
            return {"AAPL": "Apple"}
    else:
        # =======================================================================
        # 🌟 [ETF 전용 모드] 한/미 100개 정예 멤버 구성
        # =======================================================================
        if "한국" in market_type:
            try:
                df = fdr.StockListing("ETF/KR")
                etf_dict = {}
                # 🇰🇷 [업그레이드] 거래량 및 시가총액 최상위 딱 100개만 커트!
                for _, row in df.head(100).iterrows(): 
                    symbol = str(row["Symbol"]).zfill(6)
                    etf_dict[f"{symbol}.KS"] = str(row["Name"])
                return etf_dict
            except:
                return {"122630.KS": "KODEX 레버리지", "114800.KS": "KODEX 인버스"}
        else:
            # 🇺🇸 [업그레이드] 미국 우량/테마/레버리지/인버스 ETF 100선 총망라!
            return {
                # 1. 시장 지수 추종 (S&P 500, 나스닥 등)
                "SPY": "S&P 500", "IVV": "S&P 500", "VOO": "S&P 500", 
                "QQQ": "NASDAQ 100", "QQQM": "NASDAQ 100 (저보수)", "DIA": "Dow Jones", 
                "VTI": "미국 전체 주식", "IWM": "Russell 2000 (중소형주)",
                
                # 2. 배당 / 인컴
                "SCHD": "미국 배당성장", "VIG": "배당성장", "VYM": "고배당", 
                "JEPI": "프리미엄 인컴 (월배당)", "JEPQ": "나스닥 프리미엄 인컴",
                "DGRO": "배당성장", "NOBL": "배당귀족",
                
                # 3. 11대 주요 섹터
                "XLK": "기술주", "XLF": "금융주", "XLV": "헬스케어", 
                "XLE": "에너지", "XLY": "임의소비재", "XLP": "필수소비재", 
                "XLI": "산업재", "XLU": "유틸리티", "XLB": "소재", "XLRE": "부동산",
                
                # 4. 반도체 및 핵심 테크
                "SOXX": "필라델피아 반도체", "SMH": "반도체 핵심", "IGV": "소프트웨어", 
                "CIBR": "사이버보안", "HACK": "사이버보안", "SKYY": "클라우드",
                
                # 5. 바이오 / 테마 / 파괴적 혁신
                "IBB": "바이오테크", "XBI": "바이오테크 (균등)", "ARKK": "파괴적 혁신",
                "ARKG": "제노믹스", "ARKW": "차세대 인터넷",
                
                # 6. 채권 시장 (금리 인하/인상 방어용)
                "TLT": "20년 이상 장기 국채", "IEF": "7-10년 중기 국채", "SHY": "1-3년 단기 국채",
                "AGG": "미국 종합 채권", "BND": "전체 채권 시장", "LQD": "투자등급 회사채", 
                "HYG": "하이일드(정크) 본드", "JNK": "하이일드 본드",
                
                # 7. 원자재 / 금 / 비트코인
                "GLD": "금 현물", "IAU": "금 현물", "SLV": "은 현물", "USO": "원유", "UNG": "천연가스", 
                "URA": "우라늄", "COPX": "구리 채굴", "LIT": "리튬 & 배터리",
                "IBIT": "비트코인 현물", "FBTC": "비트코인 현물",
                
                # 8. 스타일 (가치/성장/모멘텀)
                "VUG": "대형 성장주", "VTV": "대형 가치주", "IWF": "Russell 1000 성장", "IWD": "Russell 1000 가치",
                "QUAL": "우량주", "MTUM": "모멘텀", "USMV": "저변동성",
                
                # 9. 🚀 레버리지 (2배/3배 상승 배팅)
                "SSO": "S&P 500 2배", "UPRO": "S&P 500 3배",
                "QLD": "NASDAQ 100 2배", "TQQQ": "NASDAQ 100 3배",
                "USD": "반도체 2배", "SOXL": "반도체 3배",
                "UCO": "원유 2배", "BOIL": "천연가스 2배",
                "CURE": "헬스케어 3배", "FAS": "금융 3배", "TECL": "기술주 3배",
                "TMF": "장기 국채 3배 (금리 인하 풀배팅)",
                
                # 10. 📉 숏/인버스 (하락 배팅 방어용)
                "SH": "S&P 500 인버스 (-1배)", "SDS": "S&P 500 인버스 (-2배)", "SPXU": "S&P 500 인버스 (-3배)",
                "PSQ": "NASDAQ 인버스 (-1배)", "QID": "NASDAQ 인버스 (-2배)", "SQQQ": "NASDAQ 인버스 (-3배)",
                "SOXS": "반도체 인버스 (-3배)", "TZA": "중소형주 인버스 (-3배)", "KOLD": "천연가스 인버스 (-2배)",
                "TBF": "장기 국채 인버스 (-1배)", "TBT": "장기 국채 인버스 (-2배)", "TMV": "장기 국채 인버스 (-3배)"
            }

@st.cache_data(ttl=14400, show_spinner=False)
def build_database(market_type, timeframe="일봉", asset_type="일반 주식"):
    tickers = list(get_market_database(market_type, asset_type).keys())
    if not tickers:
        tickers = ["005930.KS"] if "한국" in market_type else ["AAPL"]

    p, i = "1y", ("1wk" if timeframe == "주봉" else "1d")
    kwargs = {"progress": False}
    if USE_PROXY: kwargs['proxy'] = PROXY_IP

    group_data = yf.download(tickers, period=p, interval=i, group_by="ticker", threads=True, **kwargs)

    all_data = {}
    for t in tickers:
        try:
            df = group_data[t].copy() if isinstance(group_data.columns, pd.MultiIndex) else group_data.copy()
            df = df.dropna(how="all")
            df = process_technical_indicators(df)
            if not df.empty:
                all_data[t] = df
        except:
            continue
    return all_data

@st.cache_data(ttl=600, show_spinner=False)
def fetch_specific_timeframe_data(ticker, selection):
    p, i = ("2mo", "60m") if selection == "60분봉" else (("1y", "1wk") if selection == "주봉" else ("1y", "1d"))
    try:
        kwargs = {"progress": False}
        if USE_PROXY: kwargs['proxy'] = PROXY_IP
        df = yf.download(ticker, period=p, interval=i, **kwargs)
        return process_technical_indicators(df)
    except:
        return pd.DataFrame()

# =======================================================================
# 🌐 네이버 금융 수급 연동 로직 (한국 일반주식 한정)
# =======================================================================
def check_investor_streak_naver(ticker, investor_type, total_days, buy_days, min_vol_ratio=5.0):
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
        df.columns = ["날짜", "종가", "전일비", "등락률", "거래량", "기관", "외국인", "보유주수", "보유율"]
        df = df.dropna().copy()
        for col in ["기관", "외국인", "거래량"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        
        recent_df = df.head(total_days)
        valid_foreign = (recent_df["외국인"] > 0) & (recent_df["거래량"] > 0) & ((recent_df["외국인"] / recent_df["거래량"] * 100) >= min_vol_ratio)
        valid_inst = (recent_df["기관"] > 0) & (recent_df["거래량"] > 0) & ((recent_df["기관"] / recent_df["거래량"] * 100) >= min_vol_ratio)

        if investor_type == "외인": return valid_foreign.sum() >= buy_days
        elif investor_type == "기관": return valid_inst.sum() >= buy_days
        elif investor_type == "양매수": return (valid_foreign.sum() >= buy_days) and (valid_inst.sum() >= buy_days)
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
            clean_desc = clean_desc[:120] + "..." if len(clean_desc) > 120 else clean_desc
            try:
                dt_obj = datetime.datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z")
                if "GMT" in pub_date or "UTC" in pub_date: dt_obj += datetime.timedelta(hours=9)
                formatted_date = dt_obj.strftime("%Y-%m-%d %H:%M")
            except:
                formatted_date = pub_date
            news_list.append({"category": category, "title": title, "date": formatted_date, "desc": clean_desc, "source": source, "link": link})
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
            return dict(zip(pd.read_csv(SECTOR_DB_FILE)["Ticker"], pd.read_csv(SECTOR_DB_FILE)["Sector"]))
        except: pass
    return {}

def save_sector_db(db_dict):
    pd.DataFrame(list(db_dict.items()), columns=["Ticker", "Sector"]).to_csv(SECTOR_DB_FILE, index=False)

def translate_yf_sector(sec):
    if not isinstance(sec, str) or not sec.strip() or sec == "nan": return "미분류"
    s = sec.lower()
    if any(x in s for x in ["technology", "software", "semiconductor", "computer", "it"]): return "정보기술 (IT)"
    if any(x in s for x in ["health", "medical", "pharma", "biotech", "life sciences"]): return "헬스케어"
    if any(x in s for x in ["financial", "bank", "insurance", "capital", "credit"]): return "금융"
    if any(x in s for x in ["consumer cyclical", "consumer discretionary", "auto", "apparel", "retail", "leisure", "hotel", "restaurant"]): return "임의소비재"
    if any(x in s for x in ["communication", "media", "telecom", "internet", "entertainment"]): return "커뮤니케이션"
    if any(x in s for x in ["industrial", "aerospace", "defense", "machinery", "transport", "logistics", "building", "construction"]): return "산업재"
    if any(x in s for x in ["material", "chemical", "steel", "metal", "mining"]): return "소재"
    if any(x in s for x in ["energy", "oil", "gas"]): return "에너지"
    if any(x in s for x in ["defensive", "staple", "beverage", "tobacco", "food", "personal care"]): return "필수소비재"
    if any(x in s for x in ["utility", "power", "water", "electricity"]): return "유틸리티"
    if any(x in s for x in ["real estate", "reit", "property"]): return "부동산"
    return "미분류"

@st.cache_data(ttl=86400, show_spinner=False)
def sync_market_sectors_v8(market_type):
    db = load_sector_db()
    name_db = get_market_database(market_type, "일반 주식")
    tickers = list(name_db.keys())
    changed = False

    if "한국" not in market_type:
        try:
            for n in ["S&P500", "NASDAQ"]:
                for _, r in fdr.StockListing(n).iterrows():
                    tk = str(r["Symbol"]).replace(".", "-")
                    if tk in tickers and (tk not in db or db[tk] == "미분류"):
                        db[tk] = translate_yf_sector(str(r.get("Sector" if n=="S&P500" else "Industry", "")))
                        changed = True
        except: pass

    final_map = {}
    for tk in tickers:
        val = db.get(tk, "미분류")
        if val == "미분류" and "한국" in market_type:
            n = str(name_db.get(tk, "")).replace(" ", "").upper()
            if any(x in n for x in ["전자", "반도체", "IT", "소프트"]): val = "정보기술 (IT)"
            elif any(x in n for x in ["제약", "바이오", "헬스케어"]): val = "헬스케어"
            elif any(x in n for x in ["통신", "엔터", "게임", "미디어"]): val = "커뮤니케이션"
            elif any(x in n for x in ["화학", "철강", "소재"]): val = "소재"
            elif any(x in n for x in ["건설", "중공업", "조선", "방산"]): val = "산업재"
            elif any(x in n for x in ["은행", "금융", "지주", "증권"]): val = "금융"
            elif any(x in n for x in ["식품", "음료", "화장품"]): val = "필수소비재"
            elif any(x in n for x in ["쇼핑", "호텔", "여행", "자동차"]): val = "임의소비재"
            elif any(x in n for x in ["에너지", "정유"]): val = "에너지"
            elif any(x in n for x in ["전력", "가스"]): val = "유틸리티"
            elif any(x in n for x in ["리츠", "부동산"]): val = "부동산"
        final_map[tk] = val

    if changed: save_sector_db(db)
    return final_map

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_sectors_for_watchlist_v8(tickers):
    map_kor = sync_market_sectors_v8("한국")
    map_us = sync_market_sectors_v8("미국")
    return {t: map_kor.get(t, "미분류") if (".KS" in t or ".KQ" in t) else map_us.get(t, "미분류") for t in tickers}

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
            "k_market": "한국", "k_asset_type": "일반 주식", "k_array": "조건없음", "k_ma_n": 20, "k_ma_cond": "조건없음",
            "k_ichi": "조건없음", "k_bb": "조건없음", "k_macd": "조건없음", "k_rsi": (0, 100),
            "k_stoch": "조건없음", "k_vol": "조건없음", "k_vol_n": 20, "k_inv_type": "조건없음",
            "k_inv_m": 5, "k_inv_n": 3, "k_inv_pct": 5.0, "k_vol_rank": False, "k_ma_s": 5, 
            "k_ma_l": 120, "k_ma_c": "조건없음", "k_bb_sq": False, "k_bb_sq_n": 20, "k_bb_sq_pct": 5.0,
            "k_maup_n": 20, "k_maup_m": 5, "k_maup_cond": "조건없음", "k_sector": "조건없음",
            "k_drop_cond": False, "k_drop_target": 30, "k_drop_margin": 5,
            "k_per": 0.0, "k_pbr": 0.0, "k_foreigner_rate": 0.0,
        }
        for k, v in defaults.items(): st.session_state[k] = v
        if "matched_stocks" in st.session_state: del st.session_state["matched_stocks"]

    st.set_page_config(page_title="나만의 주식 검색기 V6.2", layout="wide")

    if "selected_ticker" not in st.session_state: st.session_state["selected_ticker"] = "NONE"
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
            div[data-testid="column"] p { font-size: 12px !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; margin-bottom: 0px !important; letter-spacing: -0.5px; }
            div[data-testid="column"] button { font-size: 11px !important; padding: 0px 4px !important; }
        </style>
        """, unsafe_allow_html=True,
    )

    st.title("📈 100억 벌고 싶다 (V6.2 한/미 완벽 통합)")
    st.divider()

    tab1, tab2 = st.tabs(["🔍 초고속 검색기", "⭐ 나의 관심종목 (신규 추가 가능)"])

    with tab1:
        with st.sidebar:
            # 🌟 [메인 업그레이드] 시장과 자산군 선택 영역 분리
            col_market, col_asset = st.columns(2, gap="small")
            with col_market:
                st.markdown('<div class="inline-label" style="margin-bottom: 5px;">시장</div>', unsafe_allow_html=True)
                market = st.selectbox("시장", ["한국", "미국"], label_visibility="collapsed", key="k_market")
            with col_asset:
                st.markdown('<div class="inline-label" style="margin-bottom: 5px;">조회 대상</div>', unsafe_allow_html=True)
                asset_type = st.selectbox("조회 대상", ["일반 주식", "ETF 전용"], label_visibility="collapsed", key="k_asset_type")
                
            col_btn1, col_btn2 = st.columns(2, gap="small")
            with col_btn1: st.button("🧹필터 초기화", use_container_width=True, on_click=reset_all_filters)
            with col_btn2:
                if st.button("🗑️캐시 삭제", use_container_width=True):
                    st.cache_data.clear(); st.rerun()
            st.divider()

            st.markdown("### 💾 나의 전략 저장소")
            saved_filters_dict = load_saved_filters()
            preset_names = list(saved_filters_dict.keys())

            with st.container(border=True):
                if preset_names:
                    sel_preset = st.selectbox("저장된 전략 불러오기", ["선택하세요"] + preset_names, label_visibility="collapsed")
                    c_load, c_del = st.columns(2, gap="small")
                    if c_load.button("📂 불러오기", use_container_width=True) and sel_preset != "선택하세요":
                        for k, v in saved_filters_dict[sel_preset].items():
                            st.session_state[k] = tuple(v) if k == "k_rsi" else v
                        st.rerun()
                    if c_del.button("🗑️ 삭제", use_container_width=True) and sel_preset != "선택하세요":
                        delete_filter_preset(sel_preset)
                        st.rerun()
                else:
                    st.info("저장된 전략이 없습니다.")

                new_preset_name = st.text_input("현재 조건 이름 지정", placeholder="예: 20선터치, 거래량 폭발", label_visibility="collapsed")
                if st.button("💾 현재 세팅 저장", use_container_width=True):
                    if new_preset_name.strip():
                        keys_to_save = ["k_market", "k_asset_type", "k_array", "k_ma_n", "k_ma_cond", "k_ichi", "k_bb", "k_macd", "k_rsi", "k_stoch", "k_vol", "k_vol_n", "k_inv_type", "k_inv_m", "k_inv_n", "k_inv_pct", "k_vol_rank", "k_ma_s", "k_ma_l", "k_ma_c", "k_bb_sq", "k_bb_sq_n", "k_bb_sq_pct", "k_maup_n", "k_maup_m", "k_maup_cond", "k_sector", "k_drop_cond", "k_drop_target", "k_drop_margin", "k_per", "k_pbr", "k_foreigner_rate"]
                        current_data = {}
                        for k in keys_to_save:
                            val = st.session_state.get(k)
                            if val is None:
                                if k == "k_rsi": val = (0, 100)
                                elif k in ["k_vol_rank", "k_bb_sq", "k_drop_cond"]: val = False
                                elif k in ["k_ma_n", "k_vol_n", "k_bb_sq_n", "k_maup_n"]: val = 20
                                elif k in ["k_ma_s", "k_inv_m", "k_maup_m", "k_drop_margin"]: val = 5
                                elif k == "k_inv_pct": val = 5.0
                                elif k == "k_ma_l": val = 120
                                elif k == "k_inv_n": val = 3
                                elif k == "k_bb_sq_pct": val = 5.0
                                elif k == "k_drop_target": val = 30
                                elif k == "k_market": val = "한국"
                                elif k == "k_asset_type": val = "일반 주식"
                                elif k in ["k_per", "k_pbr", "k_foreigner_rate"]: val = 0.0
                                else: val = "조건없음"
                            current_data[k] = val
                        save_filter_preset(new_preset_name.strip(), current_data)
                        st.rerun()
                    else: st.warning("이름을 입력해주세요.")
            st.divider()

            st.markdown("### 🚀 종목 스캔")
            scan_action_placeholder = st.empty()  
            st.divider()

            timeframe = "일봉"
            
            st.markdown("### 📉 고저 밴드 내 현재가 위치")
            with st.container(border=True):
                k_drop_cond = st.checkbox("🎯 1년 고저밴드 위치(%) 필터 적용", key="k_drop_cond")
                if k_drop_cond:
                    c1, c2 = st.columns(2, gap="small")
                    with c1: st.number_input("목표 위치(%)", 1, 100, 30, key="k_drop_target")
                    with c2: st.number_input("오차 범위(±%)", 1, 50, 5, key="k_drop_margin")

            st.markdown("### 📊 추세")
            c1, c2 = st.columns([35, 65], gap="small")
            with c1: st.markdown('<div class="inline-label">정/역배열</div>', unsafe_allow_html=True)
            with c2: array_cond = st.selectbox("정/역", ["조건없음", "정배열 (5>20>60)", "역배열 (5<20<60)", "5>20 & 20<60"], label_visibility="collapsed", key="k_array")
            with st.container(border=True):
                c1, c2 = st.columns(2, gap="small")
                with c1: ma_n = st.number_input("이평선(N봉)", 1, 200, 20, key="k_ma_n")
                with c2: ma_cond = st.selectbox("이평선 조건", ["조건없음", "위", "아래", "터치"], key="k_ma_cond")
            with st.container(border=True):
                st.markdown('<div class="inline-label" style="margin-bottom: 5px;">이평선 골든크로스</div>', unsafe_allow_html=True)
                c1, c2, c3 = st.columns([1, 1, 1.5], gap="small")
                with c1: ma_short = st.number_input("단기", 1, 200, 5, key="k_ma_s")
                with c2: ma_long = st.number_input("장기", 1, 200, 120, key="k_ma_l")
                with c3: ma_cross_cond = st.selectbox("크로스조건", ["조건없음", "적용"], label_visibility="hidden", key="k_ma_c")
            with st.container(border=True):
                st.markdown('<div class="inline-label" style="margin-bottom: 5px;">이평선 연속 우상향</div>', unsafe_allow_html=True)
                c1, c2, c3 = st.columns([1, 1, 1.5], gap="small")
                with c1: ma_up_n = st.number_input("N일선", 1, 200, 20, key="k_maup_n")
                with c2: ma_up_m = st.number_input("M일 연속", 1, 60, 5, key="k_maup_m")
                with c3: ma_up_cond = st.selectbox("우상향조건", ["조건없음", "적용"], label_visibility="hidden", key="k_maup_cond")

            st.markdown("### ⚡ 모멘텀")
            with st.container(border=True):
                c1, c2, c3 = st.columns([1, 1, 1], gap="small")
                with c1: ichi_cond = st.selectbox("일목균형표", ["조건없음", "위", "아래"], key="k_ichi")
                with c2: bb_cond = st.selectbox("볼린저밴드", ["조건없음", "상단", "중단", "하단"], key="k_bb")
                with c3: macd_cond = st.selectbox("MACD", ["조건없음", "골든크로스", "0선돌파"], key="k_macd")

            with st.container(border=True):
                bb_squeeze_cond = st.checkbox("🎯 볼린저밴드(스퀴즈)", value=st.session_state.get("k_bb_sq", False), key="k_bb_sq")
                if bb_squeeze_cond:
                    c1, c2 = st.columns(2, gap="small")
                    with c1: st.number_input("유지 기간(N봉)", 5, 300, st.session_state.get("k_bb_sq_n", 20), key="k_bb_sq_n")
                    with c2: st.number_input("수축 폭(%)", 1.0, 30.0, st.session_state.get("k_bb_sq_pct", 5.0), step=1.0, key="k_bb_sq_pct")

            with st.container(border=True):
                c1, c2 = st.columns(2, gap="small")
                with c1:
                    st.markdown('<div class="inline-label" style="margin-bottom: 5px;">RSI</div>', unsafe_allow_html=True)
                    rsi_min, rsi_max = st.slider("RSI 범위", 0, 100, (0, 100), label_visibility="collapsed", key="k_rsi")
                    rsi_show = "적용"
                with c2:
                    st.markdown('<div class="inline-label" style="margin-bottom: 5px;">스토캐스틱</div>', unsafe_allow_html=True)
                    stoch_cond = st.selectbox("스토캐스틱", ["조건없음", "20이하 골든크로스"], label_visibility="collapsed", key="k_stoch")

            st.markdown("### 🏢 섹터 및 거래량")
            with st.container(border=True):
                avail_sectors = ["조건없음", "정보기술 (IT)", "금융", "헬스케어", "임의소비재", "커뮤니케이션", "산업재", "필수소비재", "에너지", "소재", "유틸리티", "부동산", "미분류"]
                st.markdown('<div class="inline-label" style="margin-bottom: 5px;">섹터 선택</div>', unsafe_allow_html=True)
                sector_cond = st.selectbox("섹터", avail_sectors, label_visibility="collapsed", key="k_sector")

            with st.container(border=True):
                vol_rank_1 = st.checkbox("🔥 최근 거래량 상위 20위", value=st.session_state.get("k_vol_rank", False), key="k_vol_rank")

            with st.container(border=True):
                c1, c2 = st.columns(2, gap="small")
                with c1: vol_cond = st.selectbox("거래량 폭발", ["조건없음", "150%", "200%", "300%"], key="k_vol")
                with c2: vol_n = st.number_input("기준(N봉)", 1, 100, 20, key="k_vol_n")

            # ---------------------------------------------------------
            # 💰 가치 & 지분율 영역 (ETF 모드일 때는 화면 조건 자동 비활성화)
            # ---------------------------------------------------------
            st.markdown("---")
            if asset_type == "일반 주식":
                st.markdown("### 💰 가치 & 지분율 (구글 시트 연동)")
                with st.container(border=True):
                    c1, c2 = st.columns(2, gap="small")
                    with c1: per_cond = st.number_input("PER 이하 (0=미적용)", min_value=0.0, max_value=500.0, value=st.session_state.get("k_per", 0.0), step=1.0, key="k_per")
                    with c2: pbr_cond = st.number_input("PBR 이하 (0=미적용)", min_value=0.0, max_value=50.0, value=st.session_state.get("k_pbr", 0.0), step=0.1, key="k_pbr")
                    
                    fr_label = "외국인 지분율(%)" if market == "한국" else "기관 투자자 지분율(%)"
                    k_foreigner_rate = st.number_input(f"{fr_label} 이상 (0=미적용)", min_value=0.0, max_value=100.0, value=st.session_state.get("k_foreigner_rate", 0.0), step=1.0, key="k_foreigner_rate")

                if market == "한국":
                    st.markdown("#### 🕵️‍♂️ 수급 분석 (네이버)")
                    with st.container(border=True):
                        st.markdown('<div class="inline-label" style="margin-bottom: 5px;">외인/기관 (M일 중 N일 매수)</div>', unsafe_allow_html=True)
                        c1, c2, c3, c4 = st.columns([1.3, 0.9, 0.9, 1.2], gap="small")
                        with c1: investor_type = st.selectbox("주체", ["조건없음", "외인", "기관", "양매수"], label_visibility="collapsed", key="k_inv_type")
                        with c2: investor_total_days = st.number_input("총(M)", 1, 100, 5, label_visibility="collapsed", key="k_inv_m")
                        with c3: investor_buy_days = st.number_input("매수(N)", 1, 100, 3, label_visibility="collapsed", key="k_inv_n")
                        with c4: investor_min_pct = st.number_input("비중(%)", 0.0, 100.0, 5.0, step=1.0, label_visibility="collapsed", key="k_inv_pct")
                else:
                    investor_type, investor_total_days, investor_buy_days, investor_min_pct = "조건없음", 5, 3, 5.0
            else:
                # ETF 전용 모드일 때는 필터 조건 강제 초기화
                per_cond, pbr_cond, k_foreigner_rate, investor_type = 0.0, 0.0, 0.0, "조건없음"
                st.info("💡 ETF 모드에서는 재무 가치 지표 및 메인 수급 필터가 자동으로 제외됩니다.")

            btn_label = "📊 우량 ETF 초고속 스캔 (실행)" if asset_type == "ETF 전용" else "🚀 글로벌 데이터 초고속 스캔 (실행)"
            search_btn = scan_action_placeholder.button(btn_label, use_container_width=True, type="primary")

            if search_btn:
                st.session_state["selected_ticker"] = "NONE"
                with st.spinner(f"데이터 1년치 추출 및 차트 분석 중..."):
                    db = build_database(market, timeframe, asset_type)

                with st.spinner(f"가치 지표 데이터베이스 동기화 중..."):
                    sector_map = sync_market_sectors_v8(market) if asset_type == "일반 주식" else {}
                    sheet_data = fetch_sheet_data("KRX_DATA" if market == "한국" else "US_DATA") if asset_type == "일반 주식" else {}

                matched_stocks, debug_list = {}, []
                name_map = get_market_database(market, asset_type)
                valid_investor_tickers = set(db.keys())

                if market == "한국" and asset_type == "일반 주식" and investor_type != "조건없음":
                    with st.spinner(f"수급 분석 중..."):
                        passed_tickers = set()
                        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exc:
                            futures = {exc.submit(check_investor_streak_naver, t, investor_type, investor_total_days, investor_buy_days, investor_min_pct): t for t in valid_investor_tickers}
                            for f in as_completed(futures):
                                if f.result(): passed_tickers.add(futures[f])
                        valid_investor_tickers = passed_tickers

                for ticker, df in db.items():
                    if asset_type == "일반 주식" and sector_cond != "조건없음" and sector_map.get(ticker, "미분류") != sector_cond: continue
                    if len(df) < 60: continue
                        
                    t_per, t_pbr, t_foreign_rate = 0.0, 0.0, 0.0
                    if asset_type == "일반 주식":
                        t_per = sheet_data.get(ticker, {}).get("PER", 0.0)
                        t_pbr = sheet_data.get(ticker, {}).get("PBR", 0.0)
                        t_foreign_rate = sheet_data.get(ticker, {}).get("Foreigner", 0.0)
                        
                        if per_cond > 0 and (t_per <= 0 or t_per > per_cond): continue
                        if pbr_cond > 0 and (t_pbr <= 0 or t_pbr > pbr_cond): continue
                        if k_foreigner_rate > 0 and t_foreign_rate < k_foreigner_rate: continue

                    if market == "한국" and asset_type == "일반 주식" and investor_type != "조건없음" and ticker not in valid_investor_tickers: continue
                    if df["Volume_line"].iloc[-1] == 0 or df["Volume_line"].iloc[-2] == 0: continue

                    year_high, year_low, band_position = 0, 0, 0
                    try:
                        last_year_df = df.tail(252)
                        year_high, year_low = last_year_df["High_line"].max(), last_year_df["Low_line"].min()
                    except: pass

                    latest, prev = df.iloc[-1], df.iloc[-2]
                    cp = float(latest["Close_line"])

                    if year_high > 0 and (year_high - year_low) > 0: band_position = ((cp - year_low) / (year_high - year_low)) * 100

                    if st.session_state.get("k_drop_cond", False):
                        t_ratio, m_ratio = st.session_state.get("k_drop_target", 30), st.session_state.get("k_drop_margin", 5)
                        if year_high == 0 or not (t_ratio - m_ratio <= band_position <= t_ratio + m_ratio): continue

                    if not (rsi_min <= latest["RSI"] <= rsi_max): continue

                    if array_cond != "조건없음":
                        ma5, ma20, ma60 = df["Close_line"].rolling(5).mean().iloc[-1], df["Close_line"].rolling(20).mean().iloc[-1], df["Close_line"].rolling(60).mean().iloc[-1]
                        if "정배열" in array_cond and not (ma5 > ma20 > ma60): continue
                        if "역배열" in array_cond and not (ma5 < ma20 < ma60): continue
                        if array_cond == "5>20 & 20<60" and not (ma5 > ma20 and ma20 < ma60): continue

                    if ma_cond != "조건없음":
                        l_ma = float(df["Close_line"].rolling(ma_n).mean().iloc[-1])
                        if ma_cond == "위" and cp <= l_ma: continue
                        if ma_cond == "아래" and cp >= l_ma: continue
                        if ma_cond == "터치" and abs(cp - l_ma) / l_ma > 0.005: continue

                    if ichi_cond != "조건없음":
                        s_max, s_min = max(latest["Ichimoku_SpanA"], latest["Ichimoku_SpanB"]), min(latest["Ichimoku_SpanA"], latest["Ichimoku_SpanB"])
                        if "위" in ichi_cond and cp <= s_max: continue
                        if "아래" in ichi_cond and cp >= s_min: continue

                    if bb_cond != "조건없음":
                        if bb_cond == "상단" and cp < latest["BB_High"] * 0.98: continue
                        if bb_cond == "하단" and cp > latest["BB_Low"] * 1.02: continue
                        if bb_cond == "중단" and abs(cp - (latest["BB_High"] + latest["BB_Low"]) / 2) / ((latest["BB_High"] + latest["BB_Low"]) / 2) > 0.02: continue

                    if st.session_state.get("k_bb_sq"):
                        ma20_line = df["Close_line"].rolling(20).mean()
                        bb_width_pct = (df["BB_High"] - df["BB_Low"]) / ma20_line * 100
                        if bb_width_pct.tail(st.session_state.get("k_bb_sq_n", 20)).max() > st.session_state.get("k_bb_sq_pct", 5.0): continue

                    if stoch_cond != "조건없음" and "20이하 골든크로스" in stoch_cond:
                        if not ((prev["Stoch_K"] <= prev["Stoch_D"] and latest["Stoch_K"] > latest["Stoch_D"]) and prev["Stoch_K"] <= 20): continue

                    if ma_cross_cond == "적용":
                        if not (df["Close_line"].rolling(ma_short).mean().iloc[-2] <= df["Close_line"].rolling(ma_long).mean().iloc[-2] and df["Close_line"].rolling(ma_short).mean().iloc[-1] > df["Close_line"].rolling(ma_long).mean().iloc[-1]): continue

                    if macd_cond == "0선돌파" and not (prev["MACD"] <= 0 and latest["MACD"] > 0): continue
                    if macd_cond == "골든크로스" and not (prev["MACD"] <= prev["MACD_Signal"] and latest["MACD"] > latest["MACD_Signal"]): continue

                    sliced_for_disp = df.tail(vol_n)
                    bg_mean_disp = sliced_for_disp["Volume_line"].iloc[:-2].mean() if len(sliced_for_disp) > 2 else 0
                    recent_max_vol = max(latest["Volume_line"], prev["Volume_line"])
                    vol_ratio = (recent_max_vol / bg_mean_disp * 100) if bg_mean_disp > 0 else 0

                    if vol_cond != "조건없음":
                        recent_df = df.tail(vol_n)
                        bg_df = recent_df.iloc[:-2]
                        if bg_df.empty or bg_df["Volume_line"].mean() == 0: continue
                        if (recent_max_vol / bg_df["Volume_line"].mean() * 100) < float(vol_cond.replace("%", "")): continue
                        if recent_max_vol < (bg_df["Volume_line"].max() * 1.5): continue

                    matched_stocks[ticker] = df
                    debug_list.append({
                        "티커": ticker, "종목명": name_map.get(ticker, ticker),
                        "현재가": f"{cp:,.0f}" if market == "한국" else f"{cp:,.2f}",
                        "1년고": f"{year_high:,.0f}" if pd.notna(year_high) and year_high > 0 else "0",
                        "1년저": f"{year_low:,.0f}" if pd.notna(year_low) and year_low > 0 else "0",
                        "고저밴드": f"{band_position:.1f}%", "RSI": round(latest["RSI"], 1), "Stoch %K": round(latest["Stoch_K"], 1),
                        "거래량 비율": f"{vol_ratio:.1f}%", "당일거래량": float(latest["Volume_line"]),
                        "PER": t_per, "PBR": t_pbr, "지분율": t_foreign_rate,
                    })

                if vol_rank_1 and len(debug_list) > 0:
                    debug_list.sort(key=lambda x: x.get("당일거래량", 0), reverse=True)
                    top_tickers = [d["티커"] for d in debug_list[:20]]
                    debug_list = debug_list[:20]
                    matched_stocks = {k: v for k, v in matched_stocks.items() if k in top_tickers}

                st.session_state.update({"matched_stocks": matched_stocks if matched_stocks else "NONE", "debug_list": debug_list, "current_market": market, "filtered_list": debug_list, "current_asset_type": asset_type})

        if st.session_state.get("matched_stocks") == "NONE": st.error("조건에 맞는 종목이 없습니다.")
        elif isinstance(st.session_state.get("matched_stocks"), dict) and st.session_state.get("matched_stocks"):
            ms, dl, c_m, c_a = st.session_state["matched_stocks"], st.session_state["debug_list"], st.session_state["current_market"], st.session_state.get("current_asset_type", "일반 주식")
            n_map = get_market_database(c_m, c_a)
            sector_map = sync_market_sectors_v8(c_m) if c_a == "일반 주식" else {}

            now_kst = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
            st.info(f"🕒 실시간 주가 동기화 시점: {now_kst.strftime('%Y-%m-%d %H:%M:%S')}")

            kw = st.text_input("결과 내 검색", placeholder="종목명 또는 티커 입력").lower()
            filtered_list = [d for d in dl if kw in d["종목명"].lower() or kw in d["티커"].lower()]

            if not filtered_list: st.warning("매칭되는 종목이 없습니다.")
            else:
                st.success(f"총 {len(filtered_list)}개 자산 매칭 성공!")
                export_dl = [{"순번": i + 1, "티커": item["티커"], "종목명": item["종목명"], "섹터": sector_map.get(item["티커"], "ETF 상품" if c_a=="ETF 전용" else "미분류"), "현재가": item["현재가"], "1년고": item.get("1년고", "-"), "1년저": item.get("1년저", "-"), "고저밴드(%)": item.get("고저밴드", "0%"), "RSI": float(item.get("RSI", 0)), "ST": float(item.get("Stoch %K", 0)), "거래량%": item.get("거래량 비율", "0%"), "PER": float(item.get("PER", 0)), "PBR": float(item.get("PBR", 0)), "지분율(%)": float(item.get("지분율", 0))} for i, item in enumerate(filtered_list)]
                csv_data_tab1 = pd.DataFrame(export_dl).to_csv(index=False).encode("utf-8-sig")

                st.download_button("📥 검색결과 엑셀 다운로드", csv_data_tab1, "search_results.csv", "text/csv")

                col_ratio_tab1 = [0.6, 1.4, 1.1, 1.8, 1.4, 1.2, 1.0, 1.0, 1.0, 0.8, 0.8, 1.0, 0.7, 0.7, 0.9]
                h_cols = st.columns(col_ratio_tab1)
                headers = ["순번", "선택", "티커", "종목명", "섹터", "현재가", "1년고", "1년저", "고저밴드", "RSI", "ST", "거래량%", "PER", "PBR", "외인/기관%"]
                for i, h in enumerate(headers): h_cols[i].write(f"**{h}**")
                st.divider()

                with st.container(height=400):
                    for i, item in enumerate(filtered_list):
                        cols = st.columns(col_ratio_tab1)
                        cols[0].write(i + 1)
                        b_cols = cols[1].columns([1, 1])
                        if b_cols[0].button("🔍분석", key=f"btn_anal_{i}_{item['티커']}"): st.session_state["selected_ticker"] = item["티커"]

                        if item["티커"] in registered_tickers: b_cols[1].button("🔴등록됨", key=f"btn_reg_done_{i}_{item['티커']}", disabled=True, type="primary")
                        else:
                            if b_cols[1].button("💾등록", key=f"btn_reg_{i}_{item['티커']}"): st.session_state[f"show_input_{item['티커']}"] = True

                        if st.session_state.get(f"show_input_{item['티커']}"):
                            i_cols = st.columns([1, 1, 1])
                            target1 = i_cols[0].number_input("1차", key=f"price1_{item['티커']}", label_visibility="collapsed")
                            target2 = i_cols[1].number_input("2차", key=f"price2_{item['티커']}", label_visibility="collapsed")
                            if i_cols[2].button("확정", key=f"save_{item['티커']}"):
                                save_to_watchlist_local(item["티커"], item["종목명"], target1, target2)
                                st.session_state[f"show_input_{item['티커']}"] = False; st.rerun()

                        cols[2].write(item["티커"])
                        cols[3].write(item["종목명"])
                        cols[4].write("ETF 상품" if c_a == "ETF 전용" else str(sector_map.get(item["티커"], "미분류"))[:8])
                        cols[5].write(item["현재가"])
                        cols[6].write(item.get("1년고", "-"))
                        cols[7].write(item.get("1년저", "-"))
                        cols[8].write(item.get("고저밴드", "0%"))
                        cols[9].write(str(item.get("RSI", 0)))
                        cols[10].write(str(item.get("Stoch %K", 0)))
                        cols[11].write(item.get("거래량 비율", "0%"))
                        cols[12].write(f"{item.get('PER', 0):.2f}" if c_a == "일반 주식" and item.get("PER", 0) > 0 else "N/A")
                        cols[13].write(f"{item.get('PBR', 0):.2f}" if c_a == "일반 주식" and item.get("PBR", 0) > 0 else "N/A")
                        cols[14].write(f"{item.get('지분율', 0):.2f}%" if c_a == "일반 주식" and item.get("지분율", 0) > 0 else "N/A")
                        st.divider()

                # ---------------------------------------------------------
                # 🔍 [여기서부터 복사하세요] 분석 실행 및 가이드/차트 표시 영역
                # ---------------------------------------------------------
                @st.cache_data(ttl=86400, show_spinner=False)
                def fetch_etf_beginner_guide(ticker, market, etf_name):
                    try:
                        if market == "한국":
                            import requests
                            from bs4 import BeautifulSoup
                            import re
                            code = ticker.split(".")[0]
                            url = f"https://finance.naver.com/item/main.naver?code={code}"
                            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
                            soup = BeautifulSoup(res.text, 'html.parser')
                            
                            index_name = "자산운용사 자체 지수 또는 액티브 운용"
                            fee = "정보 없음"
                            
                            # 🎯 [정밀 타격] 엉뚱한 숫자 안 가져오게 '기초지수' 글자가 들어있는 th를 찾고 그 옆 td를 정확히 타격!
                            for th in soup.find_all('th'):
                                if "기초지수" in th.text:
                                    td = th.find_next('td')
                                    if td: index_name = td.text.strip().replace("\n", " ")
                                if "보수" in th.text:
                                    td = th.find_next('td')
                                    if td: fee = td.text.strip()
                                        
                            guide = f"**💡 무엇에 투자하나요? (추종 지수)**\n- **{index_name}**의 움직임을 똑같이 따라가도록 설계된 상품입니다.\n\n"
                            guide += f"**💰 펀드 운용 수수료**\n- **{fee}** (1년 동안 운용사가 떼어가는 비용입니다)\n\n"
                            guide += f"**📌 종목명 기초 상식:**\n- **{etf_name}** (앞의 영어는 운용사 브랜드명, 뒤는 투자 테마를 의미합니다.)"
                            
                            if any(x in etf_name for x in ["레버리지", "인버스", "2X"]):
                                guide += "\n\n⚠️ **초보자 주의:** 이 상품은 지수 변동성을 2배로 추종하거나 반대로 움직이는 고위험 상품입니다. 단기 대응용으로만 접근하세요!"
                            return guide
                            
                        else:
                            theme = etf_name
                            desc = ""
                            tip = ""
                            
                            if "S&P 500" in theme: desc = "미국 우량 대형주 500개에 투자하는 세계 1위 펀드입니다. 워렌 버핏이 가장 강력하게 추천하는 '근본' 상품입니다."
                            elif "NASDAQ 100" in theme or "나스닥" in theme: desc = "애플, 엔비디아 등 미국을 이끄는 100개 혁신 기술주에 집중합니다. 시대의 흐름을 타는 가장 빠른 방법입니다."
                            elif "배당" in theme or "인컴" in theme: desc = "주가 상승은 물론 따박따박 들어오는 배당금까지 챙기는 상품입니다. 마르지 않는 현금 파이프라인을 만들 때 필수입니다."
                            elif "반도체" in theme: desc = "AI 시대의 필수 부품인 반도체 글로벌 리더(엔비디아, TSMC 등)들을 싹 쓸어 담은 핵심 테마입니다."
                            elif "채권" in theme or "국채" in theme: desc = "주식 시장의 충격을 완화해주는 든든한 방패입니다. 금리가 내려갈 때 웃을 수 있는 안정적인 선택지입니다."
                            elif "비트코인" in theme: desc = "가장 안전하고 합법적으로 비트코인 상승세에 올라타는 방법입니다. 코인 거래소 없이 주식 계좌로 편하게 투자하세요."
                            elif any(x in theme for x in ["2배", "3배", "레버리지"]): desc = "수익이 2~3배로 폭발하지만 손실도 그만큼 빠릅니다. 야수의 심장을 가진 투자자를 위한 단기전 상품입니다."
                            elif "인버스" in theme: desc = "주가가 떨어질 때 오히려 돈을 버는 하락장 전용 상품입니다. 폭락장에서 내 계좌를 지키는 보험과 같습니다."
                            else: desc = f"글로벌 자본이 몰리는 **[{theme}]** 산업 전체에 분산 투자하여 리스크를 낮추고 성장을 누리는 상품입니다."

                            if any(x in theme for x in ["2배", "3배", "인버스"]):
                                tip = "⚠️ **절대 주의사항:** 주가가 제자리에 있어도 시간이 지나면 원금이 녹아내리는 상품입니다. 절대 '존버' 하지 마세요!"
                            elif any(x in theme for x in ["배당", "인컴"]):
                                tip = "✅ **전문가 팁:** 들어오는 배당금을 무조건 다시 재투자하세요. 시간이 지나면 복리의 마법이 계좌를 불려줍니다."
                            elif any(x in theme for x in ["국채", "채권"]):
                                tip = "✅ **전문가 팁:** 주식이 폭락할 때 채권은 버티는 경향이 있습니다. 포트폴리오의 에어백으로 10~20% 정도 섞어두세요."
                            else:
                                tip = "✅ **전문가 팁:** 개별 종목의 '악재'는 피하고 산업의 '성장'만 챙길 수 있는 가장 현명한 투자법입니다."

                            return f"**💡 핵심 족집게 브리핑 ({theme})**\n- {desc}\n\n{tip}"
                    except Exception:
                        return "가이드 데이터를 구성하는 중 오류가 발생했습니다."

                sel_tk = st.session_state["selected_ticker"]
                if sel_tk != "NONE":
                    st.divider()
                    st.subheader(f"📊 {sel_tk} ({n_map.get(sel_tk, '')}) 종합 분석")

                    if c_a == "일반 주식":
                        sheet_anal = fetch_sheet_data("KRX_DATA" if c_m == "한국" else "US_DATA")
                        f_per, f_pbr, f_fr = sheet_anal.get(sel_tk, {}).get("PER", 0.0), sheet_anal.get(sel_tk, {}).get("PBR", 0.0), sheet_anal.get(sel_tk, {}).get("Foreigner", 0.0)
                        mc1, mc2, mc3 = st.columns(3)
                        mc1.metric("PER (시트)", f"{f_per:.2f}" if f_per > 0 else "N/A")
                        mc2.metric("PBR (시트)", f"{f_pbr:.2f}" if f_pbr > 0 else "N/A")
                        mc3.metric("외국인/기관 보유율", f"{f_fr:.2f}%" if f_fr > 0 else "N/A")
                        st.divider()
                    else:
                        with st.spinner("전문가용 ETF 브리핑 불러오는 중..."):
                            etf_name_kr = n_map.get(sel_tk, sel_tk)
                            etf_desc = fetch_etf_beginner_guide(sel_tk, c_m, etf_name_kr)
                        st.info(f"**📖 ETF 1분 족집게 레포트**\n\n{etf_desc}")
                        st.divider()

                    tf = st.radio("시간 축", ["일봉", "주봉", "60분봉"], horizontal=True, key="time_frame_radio")
                    
                    # 🚀 [차트 복구 시작]
                    df = fetch_specific_timeframe_data(sel_tk, tf)
                    if df.empty: st.error("데이터 로드 실패")
                    else:
                        active_subplots = []
                        if rsi_show == "적용": active_subplots.append("RSI")
                        if stoch_cond != "조건없음": active_subplots.append("STOCH")
                        if macd_cond != "조건없음": active_subplots.append("MACD")

                        total_rows = 1 + len(active_subplots)
                        row_heights = [1.0] if total_rows == 1 else [0.5] + [0.5 / len(active_subplots)] * len(active_subplots)
                        specs = [[{"secondary_y": True}]] + [[{}]] * len(active_subplots)
                        fig = make_subplots(rows=total_rows, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=row_heights, specs=specs)
                        idx = df.index.astype(str) if tf == "60분봉" else df.index

                        fig.add_trace(go.Candlestick(x=idx, open=df["Open_line"], high=df["High_line"], low=df["Low_line"], close=df["Close_line"], name="캔들"), row=1, col=1)
                        vc = ["rgba(255,50,50,0.8)" if c >= o else "rgba(50,50,255,0.8)" for o, c in zip(df["Open_line"], df["Close_line"])]
                        fig.add_trace(go.Bar(x=idx, y=df["Volume_line"], marker_color=vc, name="거래량"), row=1, col=1, secondary_y=True)

                        if bb_cond != "조건없음" or st.session_state.get("k_bb_sq"):
                            fig.add_trace(go.Scatter(x=idx, y=df["BB_High"], line=dict(color="#8A2BE2", dash="dot"), name="BB상단"), row=1, col=1)
                            fig.add_trace(go.Scatter(x=idx, y=df["BB_Low"], line=dict(color="#8A2BE2", dash="dot"), fill="tonexty", fillcolor="rgba(138,43,226,0.05)", name="BB하단"), row=1, col=1)

                        if ichi_cond != "조건없음":
                            fig.add_trace(go.Scatter(x=idx, y=df["Ichimoku_SpanA"], line=dict(color="#00FA9A", width=1), name="일목A"), row=1, col=1)
                            fig.add_trace(go.Scatter(x=idx, y=df["Ichimoku_SpanB"], line=dict(color="#FA8072", width=1), fill="tonexty", fillcolor="rgba(250,128,114,0.1)", name="일목B"), row=1, col=1)
                        if array_cond != "조건없음":
                            fig.add_trace(go.Scatter(x=idx, y=df["Close_line"].rolling(5).mean(), line=dict(color="#FF1493", width=1.5), name="5이평"), row=1, col=1)
                            fig.add_trace(go.Scatter(x=idx, y=df["Close_line"].rolling(20).mean(), line=dict(color="#FFD700", width=1.5), name="20이평"), row=1, col=1)
                            fig.add_trace(go.Scatter(x=idx, y=df["Close_line"].rolling(60).mean(), line=dict(color="#00BFFF", width=1.5), name="60이평"), row=1, col=1)

                        current_row = 2
                        for subplot in active_subplots:
                            if subplot == "RSI":
                                fig.add_trace(go.Scatter(x=idx, y=df["RSI"], line=dict(color="purple"), name="RSI"), row=current_row, col=1)
                                fig.add_hline(y=70, line_dash="dot", line_color="orange", row=current_row, col=1); fig.add_hline(y=30, line_dash="dot", line_color="dodgerblue", row=current_row, col=1)
                                fig.update_yaxes(title_text="<b>RSI</b>", range=[0, 100], row=current_row, col=1)
                            elif subplot == "STOCH":
                                fig.add_trace(go.Scatter(x=idx, y=df["Stoch_K"], line=dict(color="darkcyan"), name="%K"), row=current_row, col=1)
                                fig.add_trace(go.Scatter(x=idx, y=df["Stoch_D"], line=dict(color="chocolate", dash="dot"), name="%D"), row=current_row, col=1)
                                fig.add_hline(y=80, line_dash="dot", line_color="red", row=current_row, col=1); fig.add_hline(y=20, line_dash="dot", line_color="green", row=current_row, col=1)
                                fig.update_yaxes(title_text="<b>STOCH</b>", range=[0, 100], row=current_row, col=1)
                            elif subplot == "MACD":
                                fig.add_trace(go.Bar(x=idx, y=df["MACD_Hist"], marker_color="gray", name="MACD Hist"), row=current_row, col=1)
                                fig.add_trace(go.Scatter(x=idx, y=df["MACD"], line=dict(color="blue"), name="MACD"), row=current_row, col=1)
                                fig.add_trace(go.Scatter(x=idx, y=df["MACD_Signal"], line=dict(color="orange", dash="dot"), name="Signal"), row=current_row, col=1)
                                fig.update_yaxes(title_text="<b>MACD</b>", row=current_row, col=1)
                            current_row += 1

                        fig.update_yaxes(title_text="<b>주가</b>", row=1, col=1, secondary_y=False)
                        fig.update_yaxes(title_text="<b>거래량</b>", showgrid=False, range=[0, df["Volume_line"].max() * 5], row=1, col=1, secondary_y=True)
                        fig.update_layout(height=max(600, 400 + (len(active_subplots) * 200)), hovermode="x unified", dragmode="pan", margin=dict(l=80, r=40, t=40, b=40), xaxis_rangeslider_visible=False)
                        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})
                        st.divider()

    # =======================================================================
    # ⭐ tab2: 관심종목 관리 화면은 그대로 유지됩니다.
    # =======================================================================
    with tab2:
        st.subheader("⭐ 나의 관심종목 포트폴리오")
        with st.expander("➕ 리스트에 없는 새로운 종목 추가하기", expanded=False):
            st.markdown("- **티커를 아는 경우**: `OKLO`, `TSLA`, `005930.KS` 등 직접 입력\n- **종목명만 아는 경우**: 한국/미국 주식 이름 검색 가능")
            c_add1, c_add2 = st.columns([7, 3])
            with c_add1: custom_input = st.text_input("티커(기호) 또는 종목명 입력", label_visibility="collapsed")
            with c_add2:
                if st.button("🌟 관심종목 추가", use_container_width=True, type="primary") and custom_input.strip():
                    search_term, search_term_upper = custom_input.strip(), custom_input.strip().upper()
                    rev_map_kr, name_map_us = get_krx_full_search_map(), get_market_database("미국", "일반 주식")
                    name_map_kr, rev_map_us = {v: k for k, v in rev_map_kr.items()}, {v: k for k, v in name_map_us.items()}
                    final_ticker, final_name = "", ""
                    
                    if search_term in rev_map_kr: final_ticker, final_name = rev_map_kr[search_term], search_term
                    elif search_term in rev_map_us: final_ticker, final_name = rev_map_us[search_term], search_term
                    else:
                        final_ticker = search_term_upper
                        if final_ticker in name_map_kr: final_name = name_map_kr[final_ticker]
                        elif final_ticker in name_map_us: final_name = name_map_us[final_ticker]
                        else: final_name = search_term_upper  

                    with st.spinner(f"'{final_ticker}' 검색 중..."):
                        try:
                            if not yf.download(final_ticker, period="1d", progress=False).empty:
                                if final_name == final_ticker:
                                    try: fetched_name = yf.Ticker(final_ticker).info.get('shortName', final_name)
                                    except: fetched_name = final_name
                                else: fetched_name = final_name
                                save_to_watchlist_local(final_ticker, fetched_name, 0.0, 0.0)
                                st.rerun()
                            else: st.error("❌ 야후 파이낸스에서 찾을 수 없습니다.")
                        except: st.error("🚨 오류 발생.")

        if "show_news" not in st.session_state: st.session_state["show_news"] = False
        c_news1, c_news2 = st.columns([8, 2])
        with c_news1: st.button("⬇️ 뉴스 닫기" if st.session_state.get("show_news") else "📰 관심종목 실시간 뉴스 스크랩 (열기)", use_container_width=True, type="primary", on_click=toggle_news_state)
        refresh_news = False
        with c_news2:
            if st.session_state.get("show_news"): refresh_news = st.button("🔄 새로고침", use_container_width=True)

        if st.session_state.get("show_news") and (refresh_news or st.session_state.pop("auto_fetch_news", False)):
            if "scraped_news" in st.session_state: del st.session_state["scraped_news"]
            with st.spinner("최신 뉴스 수집 중..."):
                df_watch = get_watchlist_df()
                queries = [("거시경제 OR 금리인상 OR 통화정책", "🌐 거시경제/정책"), ("달러 환율 OR 환율 전망", "💵 환율"), ("국제유가 OR WTI", "🛢️ 유가"), ("비트코인 OR 암호화폐", "🪙 암호화폐")]
                if not df_watch.empty:
                    for nm in df_watch["Name"]: queries.append((f"{nm} (공시 OR 실적 OR 특징주)", f"🏢 {nm} 관련뉴스"))

                all_news = []
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exc:
                    for f in as_completed([exc.submit(fetch_news_rss, q[0], q[1]) for q in queries]): all_news.extend(f.result())

                unique_news = []
                for news in all_news:
                    if not any(difflib.SequenceMatcher(None, news["title"].replace(" ", ""), un["title"].replace(" ", "")).ratio() > 0.6 for un in unique_news):
                        unique_news.append(news)
                unique_news.sort(key=lambda x: x["date"], reverse=True)
                st.session_state["scraped_news"] = unique_news

        if st.session_state.get("show_news") and st.session_state.get("scraped_news"):
            st.markdown("#### 📬 최신 뉴스 브리핑")
            with st.container(height=700, border=True):
                for news in st.session_state["scraped_news"]:
                    st.markdown(f"**[{news['category']}] [{news['title']}]({news['link']})**")
                    st.caption(f"🗓️ {news['date']} | 🗞️ {news['source']}")
                    st.write(f"> {news['desc']}"); st.divider()
        else:
            now_kst = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
            status_text = "장중 실시간" if 9 <= now_kst.hour < 15 or (now_kst.hour == 15 and now_kst.minute <= 30) else ("장 시작 전" if now_kst.hour < 9 else "장 마감")
            info_col, btn_col = st.columns([8.5, 1.5])
            with info_col: st.info(f"🕒 데이터 기준 시점: {now_kst.strftime('%Y년 %m월 %d일 %H:%M:%S')} ({status_text})")
            with btn_col:
                if st.button("🔄 현재가 갱신", use_container_width=True): fetch_watchlist_data.clear(); st.rerun()

            df_watch = get_watchlist_df()
            if df_watch.empty: st.info("등록된 관심종목이 없습니다.")
            else:
                tickers = df_watch["Ticker"].tolist()
                with st.spinner("실시간 데이터 불러오는 중..."):
                    tech_map = fetch_watchlist_data(tickers)
                    sector_map_watch = fetch_sectors_for_watchlist_v8(tickers)

                sc1, sc2 = st.columns([2, 8])
                with sc1: sort_by = st.selectbox("정렬 기준", ["1차매수 근접도(%)", "종목명", "현재가", "등록일 (최신순)", "고저밴드(%)", "RSI", "ST"], label_visibility="collapsed")
                with sc2: sort_order = st.radio("정렬 방식", ["내림차순 (큰 값부터)", "오름차순 (작은 값부터)"], horizontal=True, label_visibility="collapsed")
                st.write("")

                display_rows = []
                for idx, row in df_watch.iterrows():
                    tk, nm, dt = row["Ticker"], row["Name"], row.get("Date", "N/A")
                    tg1, tg2 = float(row.get("Target1", 0)), float(row.get("Target2", 0))
                    price, rsi, stoch = tech_map.get(tk, {}).get("Price", 0), tech_map.get(tk, {}).get("RSI", 0), tech_map.get(tk, {}).get("ST", 0)
                    y_high, y_low = tech_map.get(tk, {}).get("1YearHigh", 0), tech_map.get(tk, {}).get("1YearLow", 0)
                    band_pos = ((price - y_low) / (y_high - y_low)) * 100 if y_high > 0 and (y_high - y_low) > 0 else 0
                    diff1_pct = ((tg1 - price) / price) * 100 if price > 0 and tg1 > 0 else -9999
                    display_rows.append({"tk": tk, "nm": nm, "dt": dt, "tg1": tg1, "tg2": tg2, "price": price, "rsi": rsi, "stoch": stoch, "band_pos": band_pos, "diff1_pct": diff1_pct, "sector": sector_map_watch.get(tk, "미분류")})

                rev = sort_order == "내림차순 (큰 값부터)"
                sort_keys = {"종목명": "nm", "등록일 (최신순)": "dt", "현재가": "price", "1차매수 근접도(%)": "diff1_pct", "고저밴드(%)": "band_pos", "RSI": "rsi", "ST": "stoch"}
                display_rows.sort(key=lambda x: x[sort_keys[sort_by]], reverse=rev)

                export_df = pd.DataFrame(display_rows)[["nm", "tk", "sector", "price", "tg1", "diff1_pct", "tg2", "band_pos", "rsi", "stoch", "dt"]]
                export_df.columns = ["종목명", "티커", "섹터", "현재가", "1차매수가", "1차매수_근접도(%)", "2차매수가", "고저밴드(%)", "RSI", "STOCH", "등록일"]
                e_col1, e_col2 = st.columns([8, 2])
                with e_col2: st.download_button("📥 엑셀(CSV) 내보내기", export_df.to_csv(index=False).encode("utf-8-sig"), f"watchlist_{datetime.datetime.now().strftime('%Y%m%d')}.csv", "text/csv", use_container_width=True)

                col_ratio_tab2 = [1.5, 0.8, 1, 1.0, 0.7, 0.7, 0.5, 0.5, 0.5, 1.7]
                hc = st.columns(col_ratio_tab2)
                for i, h in enumerate(["종목명", "등록일", "섹터", "현재가", "1차매수", "2차매수", "고저", "RSI", "ST", "관리(수정/삭제)"]): hc[i].write(f"**{h}**")
                st.divider()

                with st.container(height=600):
                    for item in display_rows:
                        tk, nm, dt, tg1, tg2, price, diff1_pct = item["tk"], item["nm"], item["dt"], item["tg1"], item["tg2"], item["price"], item["diff1_pct"]
                        cc = st.columns(col_ratio_tab2)
                        cc[0].write(f"**{nm}**\n({tk})")
                        cc[1].write(dt)
                        cc[2].write(str(item.get("sector", "미분류"))[:12])

                        price_str = f"{price:,.0f}" if "KS" in tk or "KQ" in tk else f"{price:,.2f}"
                        if price > 0:
                            if tg1 > 0 and (price <= tg1 or diff1_pct >= -0.5): cc[3].markdown(f"<span style='background-color:#ff4b4b;color:white;font-weight:bold;padding:3px 6px;border-radius:4px;'>🚨 {price_str}</span>", unsafe_allow_html=True)
                            elif tg1 > 0 and diff1_pct >= -3.0: cc[3].markdown(f"<span style='background-color:#ffd700;color:black;font-weight:bold;padding:3px 6px;border-radius:4px;'>🎯 {price_str}</span>", unsafe_allow_html=True)
                            else: cc[3].write(price_str)
                        else: cc[3].write("데이터 없음")

                        if tg1 > 0: cc[4].markdown(f"<span>{f'{tg1:,.0f}' if tg1>1000 else f'{tg1:,.2f}'}</span> <span style='color:{'#ff4b4b' if diff1_pct>0 else '#00bfff'};font-size:12px;font-weight:bold;'>({'+' if diff1_pct>0 else ''}{diff1_pct:.2f}%)</span>", unsafe_allow_html=True)
                        else: cc[4].write("0")

                        if tg2 > 0 and tg1 > 0 and price < tg1 and price > 0:
                            diff2_pct = ((tg2 - price) / price) * 100
                            cc[5].markdown(f"<span>{f'{tg2:,.0f}' if tg2>1000 else f'{tg2:,.2f}'}</span> <span style='color:{'#ff4b4b' if diff2_pct>0 else '#00bfff'};font-size:12px;font-weight:bold;'>({'+' if diff2_pct>0 else ''}{diff2_pct:.2f}%)</span>", unsafe_allow_html=True)
                        else: cc[5].write(f"{tg2:,.0f}" if tg2 > 1000 else f"{tg2:,.2f}")

                        cc[6].write(f"{item['band_pos']:.1f}%"); cc[7].write(f"{item['rsi']}"); cc[8].write(f"{item['stoch']}")

                        mc1, mc2, mc3, mc4 = cc[9].columns([1, 1, 0.8, 0.8])
                        new_tg1 = mc1.number_input("1차", value=tg1, key=f"edit1_{tk}", label_visibility="collapsed")
                        new_tg2 = mc2.number_input("2차", value=tg2, key=f"edit2_{tk}", label_visibility="collapsed")
                        if mc3.button("수정", key=f"btn_edit_{tk}"): update_target_price(tk, new_tg1, new_tg2); st.rerun()
                        if mc4.button("삭제", key=f"btn_del_{tk}"): delete_from_watchlist(tk); st.rerun()
                        st.divider()

if __name__ == "__main__":
    if "streamlit" not in sys.modules and not sys.argv[0].endswith("streamlit"):
        print("\n" + "="*60 + "\n🚨 전용 웹 구동기 작동 필요\n" + "="*60 + "\n")
    else: start_100b_dashboard()
