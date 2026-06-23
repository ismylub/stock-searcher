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
from bs4 import BeautifulSoup

# =======================================================================
# 🛡️ 글로벌 설정
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

FILTER_DB_FILE = "saved_filters.json"

def load_saved_filters():
    if os.path.exists(FILTER_DB_FILE):
        try:
            with open(FILTER_DB_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return {}
    return {}

def save_filter_preset(name, current_state_dict):
    filters = load_saved_filters()
    filters[name] = current_state_dict
    with open(FILTER_DB_FILE, "w", encoding="utf-8") as f: json.dump(filters, f, ensure_ascii=False, indent=4)

def delete_filter_preset(name):
    filters = load_saved_filters()
    if name in filters:
        del filters[name]
        with open(FILTER_DB_FILE, "w", encoding="utf-8") as f: json.dump(filters, f, ensure_ascii=False, indent=4)

def get_watchlist_df():
    conn = st.connection("gsheets", type=GSheetsConnection)
    try:
        df = conn.read(worksheet="관심종목", ttl=0)
        if df.empty or "Ticker" not in df.columns: return pd.DataFrame(columns=["Ticker", "Name", "Target1", "Target2", "Date"])
        return df
    except: return pd.DataFrame(columns=["Ticker", "Name", "Target1", "Target2", "Date"])

def save_watchlist_df(df):
    conn = st.connection("gsheets", type=GSheetsConnection)
    conn.update(worksheet="관심종목", data=df)

def save_to_watchlist_local(ticker, name, target1, target2):
    df = get_watchlist_df()
    df_new = pd.DataFrame({"Ticker": [ticker], "Name": [name], "Target1": [target1], "Target2": [target2], "Date": [datetime.datetime.now().strftime("%Y-%m-%d")]})
    df_final = pd.concat([df[df["Ticker"] != ticker], df_new])
    save_watchlist_df(df_final)
    st.success(f"✅ [{name}] 관심종목 저장 완료!")

def update_target_price(ticker, new_tg1, new_tg2):
    df = get_watchlist_df()
    if not df.empty and ticker in df["Ticker"].values:
        df.loc[df["Ticker"] == ticker, "Target1"] = new_tg1
        df.loc[df["Ticker"] == ticker, "Target2"] = new_tg2
        save_watchlist_df(df)

def delete_from_watchlist(ticker):
    df = get_watchlist_df()
    if not df.empty:
        save_watchlist_df(df[df["Ticker"] != ticker])

def process_technical_indicators(df):
    if len(df) < 25: return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    df = df.ffill().bfill()
    c, h, l, v = df["Close"].squeeze(), df["High"].squeeze(), df["Low"].squeeze(), df["Volume"].squeeze()
    
    df["RSI"] = ta.momentum.rsi(c, window=14).fillna(0)
    df["OBV"] = ta.volume.OnBalanceVolumeIndicator(close=c, volume=v).on_balance_volume().fillna(0)
    
    macd = ta.trend.MACD(close=c)
    df["MACD"], df["MACD_Signal"], df["MACD_Hist"] = macd.macd().fillna(0), macd.macd_signal().fillna(0), macd.macd_diff().fillna(0)
    
    bb = ta.volatility.BollingerBands(close=c, window=20, window_dev=2)
    df["BB_High"], df["BB_Low"] = bb.bollinger_hband().fillna(0), bb.bollinger_lband().fillna(0)
    
    df["Ichimoku_SpanA"] = ((((h.rolling(9).max() + l.rolling(9).min()) / 2 + (h.rolling(26).max() + l.rolling(26).min()) / 2) / 2).shift(26).fillna(0))
    df["Ichimoku_SpanB"] = (((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26).fillna(0))
    
    stoch = ta.momentum.StochasticOscillator(high=h, low=l, close=c, window=14, smooth_window=3)
    df["Stoch_K"] = stoch.stoch_signal().fillna(0)
    df["Stoch_D"] = df["Stoch_K"].rolling(3).mean().fillna(0)
    
    df["Close_line"], df["Open_line"], df["High_line"], df["Low_line"], df["Volume_line"] = c, df["Open"].squeeze(), h, l, v
    return df

@st.cache_data(ttl=300, show_spinner=False)
def fetch_watchlist_data(tickers):
    if not tickers: return {}
    tech_map = {}
    kwargs = {"progress": False}
    if USE_PROXY: kwargs['proxy'] = PROXY_IP
    group_data = yf.download(tickers, period="1y", interval="1d", group_by="ticker", threads=True, **kwargs)

    for t in tickers:
        try:
            df = group_data[t].copy() if isinstance(group_data.columns, pd.MultiIndex) else group_data.copy()
            df = process_technical_indicators(df.dropna(how="all"))
            if df.empty or "Close_line" not in df.columns: raise Exception()
            tech_map[t] = {"Price": float(df["Close_line"].iloc[-1]), "RSI": round(float(df["RSI"].iloc[-1]), 1), "ST": round(float(df["Stoch_K"].iloc[-1]), 1), "1YearHigh": float(df["High_line"].max()), "1YearLow": float(df["Low_line"].min())}
        except: tech_map[t] = {"Price": 0.0, "RSI": 0.0, "ST": 0.0, "1YearHigh": 0.0, "1YearLow": 0.0}
    return tech_map

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sheet_data(sheet_name):
    conn = st.connection("gsheets", type=GSheetsConnection)
    try:
        df = conn.read(worksheet=sheet_name, ttl=3600)
        if df.empty: return {}
        data_dict = {}
        for _, row in df.iterrows():
            ticker = str(row.get("Ticker", ""))
            if ticker: data_dict[ticker] = {"Name": str(row.get("Name", "")), "PER": float(row.get("PER", 0.0)) if pd.notna(row.get("PER", 0.0)) else 0.0, "PBR": float(row.get("PBR", 0.0)) if pd.notna(row.get("PBR", 0.0)) else 0.0, "Foreigner": float(row.get("Foreigner", 0.0)) if pd.notna(row.get("Foreigner", 0.0)) else 0.0}
        return data_dict
    except: return {}

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_supply_trend_data():
    conn = st.connection("gsheets", type=GSheetsConnection)
    try:
        df = conn.read(worksheet="수급히스토리", ttl=3600)
        if df.empty: return {}
        trend_map = {}
        for ticker, group in df.groupby("티커"):
            recent60 = group.tail(60)
            net_buy_days = ((pd.to_numeric(recent60.get("외인순매수", 0), errors='coerce').fillna(0) + 
                             pd.to_numeric(recent60.get("기관순매수", 0), errors='coerce').fillna(0)) > 0).sum()
            total_days = len(recent60)
            if total_days > 0:
                buy_pct = int((net_buy_days / total_days) * 100)
                sell_pct = 100 - buy_pct
                trend_map[ticker] = f"매수 {buy_pct}% / 매도 {sell_pct}%"
        return trend_map
    except:
        return {} 

@st.cache_data(ttl=86400, show_spinner=False)
def get_krx_full_search_map():
    try:
        df = fdr.StockListing('KRX')
        df = df[~df['Name'].str.contains('스팩|우B|우$|우선주', regex=True, na=False)]
        return {row["Name"]: f"{str(row['Code']).zfill(6)}{'.KS' if 'KOSPI' in str(row.get('Market', '')).upper() else '.KQ'}" for _, row in df.iterrows()}
    except: return {}

@st.cache_data(ttl=86400, show_spinner=False)
def get_market_database(market_type, asset_type="일반 주식"):
    if asset_type == "일반 주식":
        if "한국" in market_type:
            krx_data = fetch_sheet_data("KRX_DATA")
            return {k: v["Name"] for k, v in krx_data.items()} if krx_data else {"005930.KS": "삼성전자"}
        else:
            us_data = fetch_sheet_data("US_DATA")
            return {k: v["Name"] for k, v in us_data.items()} if us_data else {"AAPL": "Apple"}
    else:
        if "한국" in market_type:
            try:
                df = fdr.StockListing("ETF/KR")
                return {f"{str(row['Symbol']).zfill(6)}.KS": str(row["Name"]) for _, row in df.head(100).iterrows()}
            except: return {"122630.KS": "KODEX 레버리지"}
        else:
            return {"SPY": "S&P 500", "QQQ": "NASDAQ 100"}

@st.cache_data(ttl=14400, show_spinner=False)
def build_database(market_type, timeframe="일봉", asset_type="일반 주식"):
    tickers = list(get_market_database(market_type, asset_type).keys())
    if not tickers: tickers = ["005930.KS"] if "한국" in market_type else ["AAPL"]
    p, i = "1y", ("1wk" if timeframe == "주봉" else "1d")
    kwargs = {"progress": False}
    if USE_PROXY: kwargs['proxy'] = PROXY_IP
    group_data = yf.download(tickers, period=p, interval=i, group_by="ticker", threads=True, **kwargs)
    all_data = {}
    for t in tickers:
        try:
            df = group_data[t].copy() if isinstance(group_data.columns, pd.MultiIndex) else group_data.copy()
            df = process_technical_indicators(df.dropna(how="all"))
            if not df.empty: all_data[t] = df
        except: continue
    return all_data

@st.cache_data(ttl=600, show_spinner=False)
def fetch_specific_timeframe_data(ticker, selection):
    p, i = ("2mo", "60m") if selection == "60분봉" else (("1y", "1wk") if selection == "주봉" else ("1y", "1d"))
    try:
        kwargs = {"progress": False}
        if USE_PROXY: kwargs['proxy'] = PROXY_IP
        return process_technical_indicators(yf.download(ticker, period=p, interval=i, **kwargs))
    except: return pd.DataFrame()

def check_investor_streak_naver(ticker, investor_type, total_days, buy_days, min_vol_ratio=5.0):
    if ".KS" not in ticker and ".KQ" not in ticker: return False
    time.sleep(REQUEST_DELAY)
    code = ticker.split(".")[0]
    url = f"https://finance.naver.com/item/frgn.naver?code={code}&page=1"
    try:
        req_proxies = PROXY_DICT if USE_PROXY else None
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5, proxies=req_proxies)
        res.encoding = "euc-kr"
        df = pd.read_html(StringIO(res.text))[3].copy().dropna()
        df.columns = ["날짜", "종가", "전일비", "등락률", "거래량", "기관", "외국인", "보유주수", "보유율"]
        for col in ["기관", "외국인", "거래량"]: df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        recent_df = df.head(total_days)
        valid_foreign = (recent_df["외국인"] > 0) & (recent_df["거래량"] > 0) & ((recent_df["외국인"] / recent_df["거래량"] * 100) >= min_vol_ratio)
        valid_inst = (recent_df["기관"] > 0) & (recent_df["거래량"] > 0) & ((recent_df["기관"] / recent_df["거래량"] * 100) >= min_vol_ratio)
        if investor_type == "외인": return valid_foreign.sum() >= buy_days
        elif investor_type == "기관": return valid_inst.sum() >= buy_days
        elif investor_type == "양매수": return (valid_foreign.sum() >= buy_days) and (valid_inst.sum() >= buy_days)
    except: pass
    return False

def fetch_news_rss(query, category):
    encoded_query = urllib.request.quote(f"{query} when:1d")
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        xml_data = get_urllib_opener().open(req, timeout=5).read()
        news_list = []
        for item in ET.fromstring(xml_data).findall(".//item")[:20]:
            title, link, pub_date, source_tag, desc = item.find("title").text, item.find("link").text, item.find("pubDate").text, item.find("source"), item.find("description").text
            clean_desc = re.sub("<[^<]+?>", "", desc)
            clean_desc = clean_desc[:120] + "..." if len(clean_desc) > 120 else clean_desc
            try:
                dt_obj = datetime.datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z")
                if "GMT" in pub_date or "UTC" in pub_date: dt_obj += datetime.timedelta(hours=9)
                formatted_date = dt_obj.strftime("%Y-%m-%d %H:%M")
            except: formatted_date = pub_date
            news_list.append({"category": category, "title": title, "date": formatted_date, "desc": clean_desc, "source": source_tag.text if source_tag is not None else "Google News", "link": link})
        return news_list
    except: return []

SECTOR_DB_FILE = "sector_db.csv"
def load_sector_db():
    if os.path.exists(SECTOR_DB_FILE):
        try: return dict(zip(pd.read_csv(SECTOR_DB_FILE)["Ticker"], pd.read_csv(SECTOR_DB_FILE)["Sector"]))
        except: pass
    return {}

def save_sector_db(db_dict): pd.DataFrame(list(db_dict.items()), columns=["Ticker", "Sector"]).to_csv(SECTOR_DB_FILE, index=False)

def translate_yf_sector(sec):
    if not isinstance(sec, str) or not sec.strip() or sec == "nan": return "미분류"
    s = sec.lower()
    if any(x in s for x in ["technology", "software", "semiconductor", "computer", "it"]): return "정보기술 (IT)"
    if any(x in s for x in ["health", "medical", "pharma", "biotech"]): return "헬스케어"
    if any(x in s for x in ["financial", "bank", "insurance", "capital"]): return "금융"
    if any(x in s for x in ["consumer cyclical", "auto", "retail", "leisure"]): return "임의소비재"
    if any(x in s for x in ["communication", "media", "telecom"]): return "커뮤니케이션"
    if any(x in s for x in ["industrial", "aerospace", "defense", "machinery"]): return "산업재"
    if any(x in s for x in ["material", "chemical", "steel", "mining"]): return "소재"
    if any(x in s for x in ["energy", "oil", "gas"]): return "에너지"
    if any(x in s for x in ["defensive", "staple", "food"]): return "필수소비재"
    if any(x in s for x in ["utility", "power", "water"]): return "유틸리티"
    if any(x in s for x in ["real estate", "reit"]): return "부동산"
    return "미분류"

@st.cache_data(ttl=86400, show_spinner=False)
def sync_market_sectors_v8(market_type):
    db, name_db = load_sector_db(), get_market_database(market_type, "일반 주식")
    tickers, changed = list(name_db.keys()), False
    if "한국" not in market_type:
        try:
            for n in ["S&P500", "NASDAQ"]:
                for _, r in fdr.StockListing(n).iterrows():
                    tk = str(r["Symbol"]).replace(".", "-")
                    if tk in tickers and (tk not in db or db[tk] == "미분류"):
                        db[tk], changed = translate_yf_sector(str(r.get("Sector" if n=="S&P500" else "Industry", ""))), True
        except: pass
    final_map = {}
    for tk in tickers:
        val = db.get(tk, "미분류")
        if val == "미분류" and "한국" in market_type:
            n = str(name_db.get(tk, "")).replace(" ", "").upper()
            if any(x in n for x in ["전자", "반도체", "IT", "소프트"]): val = "정보기술 (IT)"
            elif any(x in n for x in ["제약", "바이오", "헬스케어"]): val = "헬스케어"
            elif any(x in n for x in ["통신", "엔터", "게임"]): val = "커뮤니케이션"
            elif any(x in n for x in ["화학", "철강", "소재"]): val = "소재"
            elif any(x in n for x in ["건설", "중공업", "조선", "방산"]): val = "산업재"
            elif any(x in n for x in ["은행", "금융", "지주", "증권"]): val = "금융"
            elif any(x in n for x in ["식품", "음료", "화장품"]): val = "필수소비재"
            elif any(x in n for x in ["쇼핑", "자동차"]): val = "임의소비재"
            elif any(x in n for x in ["에너지", "정유"]): val = "에너지"
            elif any(x in n for x in ["전력", "가스"]): val = "유틸리티"
        final_map[tk] = val
    if changed: save_sector_db(db)
    return final_map

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_sectors_for_watchlist_v8(tickers):
    map_kor, map_us = sync_market_sectors_v8("한국"), sync_market_sectors_v8("미국")
    return {t: map_kor.get(t, "미분류") if (".KS" in t or ".KQ" in t) else map_us.get(t, "미분류") for t in tickers}

def toggle_news_state():
    st.session_state["show_news"] = not st.session_state.get("show_news", False)
    if st.session_state["show_news"] and "scraped_news" not in st.session_state: st.session_state["auto_fetch_news"] = True

def format_trend_html(trend_str):
    if not trend_str or "매수" not in trend_str: return "<span style='color:#aaaaaa;'>데이터 없음</span>"
    parts = trend_str.split(" / ")
    if len(parts) == 2:
        buy_val = int(re.sub(r'[^0-9]', '', parts[0]))
        buy_color = "#ff4b4b" if buy_val > 50 else "#555555"
        sell_color = "#00bfff" if buy_val <= 50 else "#555555"
        return f"<span style='color:{buy_color};font-weight:bold;'>{parts[0]}</span> / <span style='color:{sell_color};'>{parts[1]}</span>"
    return trend_str

# =======================================================================
# 🚀 메인 대시보드
# =======================================================================
def start_100b_dashboard():
    def reset_all_filters():
        defaults = {"k_market": "한국", "k_asset_type": "일반 주식", "k_array": "조건없음", "k_ma_n": 20, "k_ma_cond": "조건없음", "k_ichi": "조건없음", "k_bb": "조건없음", "k_macd": "조건없음", "k_rsi": (0, 100), "k_stoch": "조건없음", "k_vol": "조건없음", "k_vol_n": 20, "k_inv_type": "조건없음", "k_inv_m": 5, "k_inv_n": 3, "k_inv_pct": 5.0, "k_vol_rank": False, "k_ma_s": 5, "k_ma_l": 120, "k_ma_c": "조건없음", "k_bb_sq": False, "k_bb_sq_n": 20, "k_bb_sq_pct": 5.0, "k_maup_n": 20, "k_maup_m": 5, "k_maup_cond": "조건없음", "k_sector": "조건없음", "k_drop_cond": False, "k_drop_target": 30, "k_drop_margin": 5, "k_per": 0.0, "k_pbr": 0.0, "k_foreigner_rate": 0.0}
        for k, v in defaults.items(): st.session_state[k] = v
        if "matched_stocks" in st.session_state: del st.session_state["matched_stocks"]

    st.set_page_config(page_title="나만의 주식 검색기 V7.1", layout="wide")
    if "selected_ticker" not in st.session_state: st.session_state["selected_ticker"] = "NONE"
    registered_tickers = get_watchlist_df()["Ticker"].tolist()

    st.markdown("""<style>[data-testid="stSidebarUserContent"] { padding-top: 0rem !important; margin-top: -40px !important; } [data-testid="stSidebarUserContent"] h3 { font-size: 15px !important; margin-top: -20px !important; margin-bottom: -10px !important; } .inline-label { font-size: 13px !important; font-weight: bold; color: #333333; margin-top: -10px !important; margin-bottom: 2px !important; } div[data-baseweb="select"] { font-size: 12px !important; } div[data-baseweb="select"] > div { min-height: 40px !important; height: 40px !important; } [data-testid="stVerticalBlockBorderWrapper"] { padding: 5px 8px !important; margin-bottom: -20px !important; } .stButton button { min-height: 28px !important; height: 28px !important; font-size: 12px !important; padding: 0px 2px !important; white-space: nowrap !important; } hr { margin-top: 5px !important; margin-bottom: 5px !important; } [data-testid="stMarkdownContainer"] p { margin-bottom: 0px !important; } .stCheckbox { margin-top: 5px !important; } button[data-baseweb="tab"] { font-size: 16px !important; font-weight: bold !important; } div[data-testid="column"] p { font-size: 12px !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; margin-bottom: 0px !important; letter-spacing: -0.5px; } div[data-testid="column"] button { font-size: 11px !important; padding: 0px 4px !important; }</style>""", unsafe_allow_html=True)
    st.title("📈 100억 벌고 싶다 (V7.1 수급 디테일 완성)")
    st.divider()

    tab1, tab2 = st.tabs(["🔍 초고속 검색기", "⭐ 나의 관심종목 (신규 추가 가능)"])

    with tab1:
        with st.sidebar:
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
                if st.button("🗑️캐시 삭제", use_container_width=True): st.cache_data.clear(); st.rerun()
            st.divider()

            st.markdown("### 💾 나의 전략 저장소")
            saved_filters_dict = load_saved_filters()
            preset_names = list(saved_filters_dict.keys())
            with st.container(border=True):
                if preset_names:
                    sel_preset = st.selectbox("저장된 전략 불러오기", ["선택하세요"] + preset_names, label_visibility="collapsed")
                    c_load, c_del = st.columns(2, gap="small")
                    if c_load.button("📂 불러오기", use_container_width=True) and sel_preset != "선택하세요":
                        for k, v in saved_filters_dict[sel_preset].items(): st.session_state[k] = tuple(v) if k == "k_rsi" else v
                        st.rerun()
                    if c_del.button("🗑️ 삭제", use_container_width=True) and sel_preset != "선택하세요":
                        delete_filter_preset(sel_preset); st.rerun()
                else: st.info("저장된 전략이 없습니다.")

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
                                elif k in ["k_inv_pct", "k_bb_sq_pct"]: val = 5.0
                                elif k == "k_ma_l": val = 120
                                elif k == "k_inv_n": val = 3
                                elif k == "k_drop_target": val = 30
                                elif k == "k_market": val = "한국"
                                elif k == "k_asset_type": val = "일반 주식"
                                elif k in ["k_per", "k_pbr", "k_foreigner_rate"]: val = 0.0
                                else: val = "조건없음"
                            current_data[k] = val
                        save_filter_preset(new_preset_name.strip(), current_data); st.rerun()
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

            st.markdown("### ⚡ 모멘텀")
            with st.container(border=True):
                c1, c2, c3 = st.columns([1, 1, 1], gap="small")
                with c1: ichi_cond = st.selectbox("일목균형표", ["조건없음", "위", "아래"], key="k_ichi")
                with c2: bb_cond = st.selectbox("볼린저밴드", ["조건없음", "상단", "중단", "하단"], key="k_bb")
                with c3: macd_cond = st.selectbox("MACD", ["조건없음", "골든크로스", "0선돌파"], key="k_macd")
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
                c1, c2 = st.columns(2, gap="small")
                with c1: vol_cond = st.selectbox("거래량 폭발", ["조건없음", "150%", "200%", "300%"], key="k_vol")
                with c2: vol_n = st.number_input("기준(N봉)", 1, 100, 20, key="k_vol_n")

            st.markdown("---")
            if asset_type == "일반 주식":
                st.markdown("### 💰 가치 & 지분율 (구글 시트 연동)")
                with st.container(border=True):
                    c1, c2 = st.columns(2, gap="small")
                    with c1: per_cond = st.number_input("PER 이하 (0=미적용)", min_value=0.0, max_value=500.0, value=st.session_state.get("k_per", 0.0), step=1.0, key="k_per")
                    with c2: pbr_cond = st.number_input("PBR 이하 (0=미적용)", min_value=0.0, max_value=50.0, value=st.session_state.get("k_pbr", 0.0), step=0.1, key="k_pbr")
                    fr_label = "외인지분(%)" if market == "한국" else "기관지분(%)"
                    k_foreigner_rate = st.number_input(f"{fr_label} 이상 (0=미적용)", min_value=0.0, max_value=100.0, value=st.session_state.get("k_foreigner_rate", 0.0), step=1.0, key="k_foreigner_rate")
            else:
                per_cond, pbr_cond, k_foreigner_rate = 0.0, 0.0, 0.0
                st.info("💡 ETF 모드에서는 재무 가치 지표 및 메인 수급 필터가 자동으로 제외됩니다.")

            btn_label = "📊 우량 ETF 초고속 스캔 (실행)" if asset_type == "ETF 전용" else "🚀 글로벌 데이터 초고속 스캔 (실행)"
            search_btn = scan_action_placeholder.button(btn_label, use_container_width=True, type="primary")

            if search_btn:
                st.session_state["selected_ticker"] = "NONE"
                with st.spinner(f"데이터 1년치 추출 및 차트 분석 중..."):
                    db = build_database(market, timeframe, asset_type)

                with st.spinner(f"가치 및 수급 지표 동기화 중..."):
                    sector_map = sync_market_sectors_v8(market) if asset_type == "일반 주식" else {}
                    sheet_data = fetch_sheet_data("KRX_DATA" if market == "한국" else "US_DATA") if asset_type == "일반 주식" else {}
                    supply_trend_map = fetch_supply_trend_data() 

                matched_stocks, debug_list = {}, []
                name_map = get_market_database(market, asset_type)

                for ticker, df in db.items():
                    if asset_type == "일반 주식" and sector_cond != "조건없음" and sector_map.get(ticker, "미분류") != sector_cond: continue
                    if len(df) < 60: continue
                        
                    t_per, t_pbr, t_foreign_rate = 0.0, 0.0, 0.0
                    if asset_type == "일반 주식":
                        t_per, t_pbr, t_foreign_rate = sheet_data.get(ticker, {}).get("PER", 0.0), sheet_data.get(ticker, {}).get("PBR", 0.0), sheet_data.get(ticker, {}).get("Foreigner", 0.0)
                        if per_cond > 0 and (t_per <= 0 or t_per > per_cond): continue
                        if pbr_cond > 0 and (t_pbr <= 0 or t_pbr > pbr_cond): continue
                        if k_foreigner_rate > 0 and t_foreign_rate < k_foreigner_rate: continue

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

                    matched_stocks[ticker] = df
                    t_trend = supply_trend_map.get(ticker, "데이터 없음")
                    
                    # 🌟 명확한 키값 할당 (결측치는 "-" 대신 "N/A" 사용)
                    debug_list.append({
                        "티커": ticker, "종목명": name_map.get(ticker, ticker), 
                        "현재가": f"{cp:,.0f}" if market == "한국" else f"{cp:,.2f}", 
                        "1년고": f"{year_high:,.0f}" if pd.notna(year_high) and year_high > 0 else "0", 
                        "1년저": f"{year_low:,.0f}" if pd.notna(year_low) and year_low > 0 else "0", 
                        "고저밴드": f"{band_position:.1f}%", 
                        "외인기관율": f"{t_foreign_rate:.2f}%" if t_foreign_rate > 0 else "N/A",
                        "수급추세": t_trend
                    })

                st.session_state.update({"matched_stocks": matched_stocks if matched_stocks else "NONE", "debug_list": debug_list, "current_market": market, "current_asset_type": asset_type})

        if st.session_state.get("matched_stocks") == "NONE": st.error("조건에 맞는 종목이 없습니다.")
        elif isinstance(st.session_state.get("matched_stocks"), dict) and st.session_state.get("matched_stocks"):
            ms, dl, c_m, c_a = st.session_state["matched_stocks"], st.session_state["debug_list"], st.session_state["current_market"], st.session_state.get("current_asset_type", "일반 주식")
            n_map = get_market_database(c_m, c_a)
            sector_map = sync_market_sectors_v8(c_m) if c_a == "일반 주식" else {}

            now_kst = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
            st.info(f"🕒 데이터 동기화 완료")

            kw = st.text_input("결과 내 검색", placeholder="종목명 또는 티커 입력").lower()
            filtered_list = [d for d in dl if kw in d["종목명"].lower() or kw in d["티커"].lower()]

            if not filtered_list: st.warning("매칭되는 종목이 없습니다.")
            else:
                st.success(f"총 {len(filtered_list)}개 자산 매칭 성공! (스캔 버튼을 꼭 눌러주세요)")
                
                # 🌟 [헤더 텍스트 변경: 한국=외인%, 미국=기관%]
                fr_header_text = "외인지분(%)" if c_m == "한국" else "기관지분(%)"
                col_ratio_tab1 = [0.7, 1.5, 1.3, 1.8, 1.2, 1.2, 1.2, 1.2, 1.0, 1.0, 1.5]
                h_cols = st.columns(col_ratio_tab1)
                for i, h in enumerate(["순번", "선택", "티커", "종목명", "섹터", "현재가", "1년고", "1년저", "고저밴드", fr_header_text, "수급추세"]): 
                    h_cols[i].write(f"**{h}**")
                st.divider()

                with st.container(height=400):
                    for i, item in enumerate(filtered_list):
                        cols = st.columns(col_ratio_tab1)
                        cols[0].write(i + 1)
                        b_cols = cols[1].columns([1, 1])
                        if b_cols[0].button("🔍분석", key=f"btn_anal_{i}_{item['티커']}"): st.session_state["selected_ticker"] = item["티커"]
                        if item["티커"] in registered_tickers: b_cols[1].button("🔴등록", key=f"btn_reg_done_{i}_{item['티커']}", disabled=True, type="primary")
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
                        cols[6].write(item.get("1년고", "N/A"))
                        cols[7].write(item.get("1년저", "N/A"))
                        cols[8].write(item.get("고저밴드", "0%"))
                        # 🌟 N/A 텍스트가 점으로 변하지 않도록 강제 문자열 출력
                        cols[9].text(item.get("외인기관율", "N/A"))
                        cols[10].markdown(format_trend_html(item.get("수급추세", "N/A")), unsafe_allow_html=True)
                        st.divider()

                # =========================================================
                # 🚀 종합분석 (2줄 카드뷰 + 차트)
                # =========================================================
                sel_tk = st.session_state["selected_ticker"]
                if sel_tk != "NONE":
                    st.divider()
                    st.subheader(f"📊 {sel_tk} ({n_map.get(sel_tk, '')}) 종합 분석")

                    df_anal = ms.get(sel_tk, pd.DataFrame()) if isinstance(ms, dict) else fetch_specific_timeframe_data(sel_tk, "일봉")
                    if not df_anal.empty:
                        sheet_anal = fetch_sheet_data("KRX_DATA" if c_m == "한국" else "US_DATA")
                        f_per = sheet_anal.get(sel_tk, {}).get("PER", 0.0)
                        f_pbr = sheet_anal.get(sel_tk, {}).get("PBR", 0.0)
                        f_fr = sheet_anal.get(sel_tk, {}).get("Foreigner", 0.0)
                        
                        latest_rsi = round(df_anal["RSI"].iloc[-1], 1)
                        latest_stoch = round(df_anal["Stoch_K"].iloc[-1], 1)
                        bg_mean = df_anal["Volume_line"].iloc[:-2].mean() if len(df_anal) > 2 else 0
                        vol_ratio = (df_anal["Volume_line"].iloc[-1] / bg_mean * 100) if bg_mean > 0 else 0

                        mc1, mc2, mc3 = st.columns(3)
                        mc1.metric("PER (가치 지표)", f"{f_per:.2f}" if c_a == "일반 주식" and f_per > 0 else "N/A")
                        mc2.metric("PBR (가치 지표)", f"{f_pbr:.2f}" if c_a == "일반 주식" and f_pbr > 0 else "N/A")
                        
                        # 🌟 동적 기관명 적용
                        fr_label_anal = "외인 보유율(%)" if c_m == "한국" else "기관 보유율(%)"
                        mc3.metric(fr_label_anal, f"{f_fr:.2f}%" if c_a == "일반 주식" and f_fr > 0 else "N/A")
                        
                        st.markdown("<br>", unsafe_allow_html=True)
                        mc4, mc5, mc6 = st.columns(3)
                        mc4.metric("RSI (과매수/과매도)", f"{latest_rsi}")
                        mc5.metric("Stochastic %K (단기 탄력)", f"{latest_stoch}")
                        mc6.metric("당일 거래량 폭발(%)", f"{vol_ratio:.1f}%")
                        st.divider()

                    tf = st.radio("시간 축", ["일봉", "주봉", "60분봉"], horizontal=True, key="time_frame_radio")
                    
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

                        fig.update_yaxes(title_text="<b>주가</b>", row=1, col=1, secondary_y=False)
                        fig.update_yaxes(title_text="<b>거래량</b>", showgrid=False, range=[0, df["Volume_line"].max() * 5], row=1, col=1, secondary_y=True)
                        fig.update_layout(height=max(500, 300 + (len(active_subplots) * 200)), hovermode="x unified", dragmode="pan", margin=dict(l=80, r=40, t=40, b=40), xaxis_rangeslider_visible=False)
                        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})
                        st.divider()

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
                                save_to_watchlist_local(final_ticker, final_name, 0.0, 0.0); st.rerun()
                            else: st.error("❌ 찾을 수 없습니다.")
                        except: st.error("🚨 오류 발생.")

        df_watch = get_watchlist_df()
        if not df_watch.empty:
            tickers = df_watch["Ticker"].tolist()
            with st.spinner("실시간 데이터 불러오는 중..."):
                tech_map = fetch_watchlist_data(tickers)
                sector_map_watch = fetch_sectors_for_watchlist_v8(tickers)
                sheet_data_kr = fetch_sheet_data("KRX_DATA")
                sheet_data_us = fetch_sheet_data("US_DATA")
                supply_trend_map = fetch_supply_trend_data() 
            
            sc1, sc2 = st.columns([2, 8])
            with sc1: sort_by = st.selectbox("정렬 기준", ["1차매수 근접도(%)", "종목명", "현재가", "등록일 (최신순)", "고저밴드(%)"], label_visibility="collapsed")
            with sc2: sort_order = st.radio("정렬 방식", ["내림차순", "오름차순"], horizontal=True, label_visibility="collapsed")
            
            display_rows = []
            for _, row in df_watch.iterrows():
                tk, nm, dt = row["Ticker"], row["Name"], row.get("Date", "N/A")
                tg1, tg2 = float(row.get("Target1", 0)), float(row.get("Target2", 0))
                price = tech_map.get(tk, {}).get("Price", 0)
                y_high, y_low = tech_map.get(tk, {}).get("1YearHigh", 0), tech_map.get(tk, {}).get("1YearLow", 0)
                band_pos = ((price - y_low) / (y_high - y_low)) * 100 if y_high > 0 and (y_high - y_low) > 0 else 0
                diff1_pct = ((tg1 - price) / price) * 100 if price > 0 and tg1 > 0 else -9999
                
                fr_rate = sheet_data_kr.get(tk, {}).get("Foreigner", 0.0) if ".KS" in tk or ".KQ" in tk else sheet_data_us.get(tk, {}).get("Foreigner", 0.0)

                display_rows.append({
                    "tk": tk, "nm": nm, "dt": dt, "tg1": tg1, "tg2": tg2, 
                    "price": price, "band_pos": band_pos, "diff1_pct": diff1_pct, 
                    "sector": sector_map_watch.get(tk, "미분류"), 
                    "fr_rate": fr_rate, "trend": supply_trend_map.get(tk, "데이터 없음")
                })

            display_rows.sort(key=lambda x: {"종목명": x["nm"], "등록일 (최신순)": x["dt"], "현재가": x["price"], "1차매수 근접도(%)": x["diff1_pct"], "고저밴드(%)": x["band_pos"]}[sort_by], reverse=(sort_order == "내림차순"))
            
            # 🌟 헤더 통일: 한국-외인/미국-기관 이 모두 섞여 있으므로 포괄적인 제목 사용
            col_ratio_tab2 = [1.3, 0.8, 1, 1.0, 0.8, 0.8, 0.6, 0.8, 1.4, 1.5]
            hc = st.columns(col_ratio_tab2)
            for i, h in enumerate(["종목명", "등록일", "섹터", "현재가", "1차매수", "2차매수", "고저", "외인(韓)/기관(美)%", "수급추세", "관리(수정/삭제)"]): 
                hc[i].write(f"**{h}**")
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
                    cc[6].write(f"{item['band_pos']:.1f}%")
                    
                    # 🌟 점(•) 방지: 강제 text 출력 및 N/A 처리
                    cc[7].text(f"{item['fr_rate']:.2f}%" if item['fr_rate'] > 0 else "N/A")
                    cc[8].markdown(format_trend_html(item['trend']), unsafe_allow_html=True)

                    mc1, mc2, mc3, mc4 = cc[9].columns([1, 1, 0.8, 0.8])
                    new_tg1 = mc1.number_input("1차", value=tg1, key=f"edit1_{tk}", label_visibility="collapsed")
                    new_tg2 = mc2.number_input("2차", value=tg2, key=f"edit2_{tk}", label_visibility="collapsed")
                    if mc3.button("수정", key=f"btn_edit_{tk}"): update_target_price(tk, new_tg1, new_tg2); st.rerun()
                    if mc4.button("삭제", key=f"btn_del_{tk}"): delete_from_watchlist(tk); st.rerun()
                    st.divider()

if __name__ == "__main__":
    if "streamlit" not in sys.modules and not sys.argv[0].endswith("streamlit"): print("\n🚨 전용 웹 구동기 작동 필요\n")
    else: start_100b_dashboard()
