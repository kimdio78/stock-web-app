import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import urllib3
import FinanceDataReader as fdr
import time
import re
import webbrowser

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# [v3] 한국 표준시 — Streamlit Cloud는 UTC로 구동되므로 명시적으로 KST 변환
KST = timezone(timedelta(hours=9))


# =====================================================================
# [v3 신설] 메타/보조 유틸 — 토큰 절감용 (타임스탬프, 유동성, 매크로)
# =====================================================================
def get_data_timestamp():
    """크롤링(데이터 기준) 시각과 장 세션 상태를 KST로 반환.
    Claude가 'PDF가 당일 최신인지'를 판단해 시세·수급 재검색을 생략하는 근거."""
    now = datetime.now(KST)
    wd = now.weekday()  # 0=월 ... 6=일
    hm = now.hour * 100 + now.minute
    if wd >= 5:
        session = "휴장(주말)"
    elif 900 <= hm <= 1530:
        session = "장중"
    elif hm < 900:
        session = "장 시작 전"
    else:
        session = "장 마감 이후"
    return {
        "iso": now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "human": now.strftime("%Y-%m-%d %H:%M KST"),
        "session": session,
    }


def _to_num(text):
    """'10,270' / '1.29 %' 같은 문자열 → float (실패 시 None)"""
    if text is None:
        return None
    try:
        m = re.search(r'-?\d[\d,]*\.?\d*', str(text))
        if not m:
            return None
        return float(m.group().replace(',', ''))
    except Exception:
        return None


def get_liquidity_and_band(ticker, high_52=None, low_52=None, curr_price=0):
    """[v3 §8.3] 20일 평균 거래대금(ADTV), 거래량 비율, 52주 밴드 내 위치.
    거래대금은 종가×거래량 근사(원). FinanceDataReader 사용."""
    out = {"adtv": None, "vol_avg20": None, "vol_today": None,
           "vol_ratio": None, "band_pos": None}
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=45)
        df = fdr.DataReader(ticker, start, end)
        if df is not None and not df.empty:
            df = df.tail(20)
            tv = (df['Close'] * df['Volume'])           # 근사 거래대금(원)
            out["adtv"] = float(tv.mean())
            out["vol_avg20"] = float(df['Volume'].mean())
            out["vol_today"] = float(df['Volume'].iloc[-1])
            if out["vol_avg20"]:
                out["vol_ratio"] = out["vol_today"] / out["vol_avg20"]
    except Exception:
        pass
    # 52주 밴드 내 위치 (추가 호출 없이 이미 크롤링한 값으로 계산)
    try:
        hi = _to_num(high_52)
        lo = _to_num(low_52)
        if hi and lo and hi > lo and curr_price > 0:
            out["band_pos"] = (curr_price - lo) / (hi - lo) * 100
    except Exception:
        pass
    return out


def get_macro_indicators():
    """[v3 §4.A/§5.3] USD/KRW, (best-effort) 국고채 10년물.
    국고채 심볼은 환경에 따라 미지원일 수 있어 실패 시 None."""
    out = {"usdkrw": None, "kr10y": None}
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=10)
        fx = fdr.DataReader('USD/KRW', start, end)
        if fx is not None and not fx.empty:
            out["usdkrw"] = float(fx['Close'].iloc[-1])
    except Exception:
        pass
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=15)
        bond = fdr.DataReader('KR10YT=RR', start, end)   # best-effort
        if bond is not None and not bond.empty:
            out["kr10y"] = float(bond['Close'].iloc[-1])
    except Exception:
        pass
    return out


# --- 데이터 수집 함수들 ---
@st.cache_data(ttl=3600)
def load_stock_data():
    try:
        df = fdr.StockListing('KRX')
        if not df.empty:
            df['Search_Key'] = df['Name'] + " (" + df['Code'] + ")"
            search_map = dict(zip(df['Search_Key'], df['Code']))
            ticker_to_name = dict(zip(df['Code'], df['Name']))
            search_list = list(search_map.keys())
            return search_list, search_map, ticker_to_name
    except:
        pass
    return [], {}, {}

def get_naver_stock_details(ticker):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        
        data = {
            'name': ticker, 'overview': "정보 없음", 
            'now_price': '0', 'diff_rate': '0.00', 'diff_amount': '0', 'direction': 'flat',
            'market_cap': '-', 'shares': 0, 'foreign_rate': '-', 
            'per': '-', 'eps': '-', 'pbr': '-', 'bps': '-', 'dvr': '-',
            'high_52': '-', 'low_52': '-'
        }
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            name_tag = soup.select_one(".wrap_company h2 a")
            if name_tag: data['name'] = name_tag.text.strip()

            overview_div = soup.select_one("#summary_info")
            if overview_div:
                data['overview'] = "\n ".join([p.text.strip() for p in overview_div.select("p") if p.text.strip()])

            try:
                now_tag = soup.select_one(".no_today .blind")
                if now_tag: data['now_price'] = now_tag.text.strip()
                
                exday_tag = soup.select_one(".no_exday")
                if exday_tag:
                    spans = exday_tag.select("span.blind")
                    if len(spans) >= 2:
                        data['diff_amount'] = spans[0].text.strip()
                        data['diff_rate'] = spans[1].text.strip()
                    
                    if exday_tag.select_one(".ico.up"): data['direction'] = 'up'
                    elif exday_tag.select_one(".ico.down"): data['direction'] = 'down'
                    elif exday_tag.select_one(".ico.upper"): data['direction'] = 'upper'
                    elif exday_tag.select_one(".ico.lower"): data['direction'] = 'lower'
            except: pass

            try:
                mc_element = soup.select_one("#_market_sum")
                if mc_element:
                    data['market_cap'] = mc_element.text.strip().replace('\t', '').replace('\n', '') + " 억원"
            except: pass

            try:
                first_table = soup.select_one("div.first table")
                if first_table:
                    for tr in first_table.select("tr"):
                        if "상장주식수" in tr.text:
                            em = tr.select_one("em")
                            if em:
                                shares_str = em.text.strip().replace(',', '')
                                data['shares'] = int(shares_str)
                            break
            except: pass

            try:
                per_el = soup.select_one("#_per")
                if per_el: data['per'] = per_el.text.strip()
                eps_el = soup.select_one("#_eps")
                if eps_el: data['eps'] = eps_el.text.strip()
                pbr_el = soup.select_one("#_pbr")
                if pbr_el: data['pbr'] = pbr_el.text.strip()
                dvr_el = soup.select_one("#_dvr")
                if dvr_el: data['dvr'] = dvr_el.text.strip()
            except: pass

            all_ths = soup.select("th")
            for th in all_ths:
                th_text = th.text.strip()
                if "외국인소진율" in th_text:
                    td = th.find_next_sibling("td")
                    if td:
                        em = td.select_one("em")
                        data['foreign_rate'] = em.text.strip() if em else td.text.strip()
                elif "52주최고" in th_text:
                    td = th.find_next_sibling("td")
                    if td:
                        ems = td.select("em")
                        if len(ems) >= 2:
                            data['high_52'] = ems[0].text.strip()
                            data['low_52'] = ems[1].text.strip()
                elif "BPS" in th_text and "PBR" not in th_text:
                    td = th.find_next_sibling("td")
                    if td:
                        em = td.select_one("em")
                        data['bps'] = em.text.strip() if em else td.text.strip()
            
            if data['bps'] == '-':
                try:
                    per_table = soup.select_one("table.per_table")
                    if per_table:
                        rows = per_table.select("tr")
                        for r in rows:
                            if "BPS" in r.text:
                                ems = r.select("em")
                                if len(ems) >= 2: data['bps'] = ems[1].text.strip()
                                elif len(ems) == 1: data['bps'] = ems[0].text.strip()
                except: pass

        return data
    except:
        return {'name': ticker, 'overview': "로딩 실패", 'shares': 0}

def get_investor_trend(ticker):
    try:
        url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False)
        trends = []
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            tables = soup.select("table.type2")
            if len(tables) >= 2:
                target_table = tables[1]
                rows = target_table.select("tr")
                for row in rows:
                    cols = row.select("td")
                    if len(cols) == 9:
                        date = cols[0].text.strip()
                        close = cols[1].text.strip()
                        rate = cols[3].text.strip().replace('\n', '').replace('\t', '')
                        inst_net = cols[5].text.strip()
                        frgn_net = cols[6].text.strip()
                        hold_rate = cols[8].text.strip()
                        trends.append({"날짜": date, "종가": close, "등락률": rate, "기관": inst_net, "외국인": frgn_net, "보유율": hold_rate})
                        if len(trends) >= 10: break
        return trends
    except:
        return []

def get_same_industry_comparison(ticker):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            compare_section = soup.select_one("div.section.trade_compare")
            if compare_section:
                table = compare_section.select_one("table")
                if table:
                    headers = ["구분"]
                    thead = table.select_one("thead")
                    for th in thead.select("th"):
                        if th.find("a"):
                            raw_header = th.text.strip()
                            clean_header = raw_header.split('*')[0].strip()
                            headers.append(clean_header)
                    
                    rows_data = []
                    tbody = table.select_one("tbody")
                    for tr in tbody.select("tr"):
                        row_val = []
                        th_item = tr.select_one("th")
                        row_title = ""
                        if th_item:
                            row_title = th_item.text.strip()
                            row_val.append(row_title)
                        
                        for td in tr.select("td"):
                            raw_text = td.text.strip()
                            clean_text = re.sub(r'[\n\t]+', ' ', raw_text)
                            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                            
                            if row_title in ["전일대비", "등락률"]:
                                val_text = re.sub(r'[^0-9.,%]', '', clean_text)
                                if "상향" in clean_text or "상승" in clean_text or "+" in clean_text:
                                    clean_text = f'<span style="color:#d20000">+{val_text}</span>'
                                elif "하향" in clean_text or "하락" in clean_text or "-" in clean_text:
                                    clean_text = f'<span style="color:#0051c7">-{val_text}</span>'
                                elif "보합" in clean_text:
                                    clean_text = val_text
                            
                            row_val.append(clean_text)
                        
                        if len(row_val) == len(headers):
                             rows_data.append(row_val)
                        elif len(row_val) > len(headers):
                             rows_data.append(row_val[:len(headers)])

                    return pd.DataFrame(rows_data, columns=headers)
        return pd.DataFrame()
    except:
        return pd.DataFrame()

def clean_float(text):
    if not text or text.strip() in ['-', 'N/A', '', '.']:
        return 0.0
    try:
        text = text.replace(',', '')
        match = re.search(r'-?\d+\.?\d*', text)
        if match:
            return float(match.group())
        return 0.0
    except:
        return 0.0

def get_financials_from_naver(ticker, current_price=0, shares=0):
    """[v3 개정] 연간/분기 실적 + 컨센서스 추정(E)을 함께 반환.
    기존 버전은 '(E)' 컬럼을 버렸으나, v3 §3.5/§7.2를 위해 estimate_data로 보존."""
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False)
        soup = BeautifulSoup(response.text, 'html.parser')
        finance_table = soup.select_one("div.section.cop_analysis > div.sub_section > table")
        if not finance_table: return [], [], []

        header_rows = finance_table.select("thead > tr")
        date_cols = [th.text.strip() for th in header_rows[1].select("th")]
        
        annual_idxs = []
        quarter_idxs = []
        estimate_idxs = []   # [v3] 추정(E) 컬럼 보존
        
        for i, col in enumerate(date_cols):
             if "(E)" in col:
                 estimate_idxs.append(i)
             elif i < 4:
                 annual_idxs.append(i)
             else:
                 quarter_idxs.append(i)
        
        annual_idxs = annual_idxs[-3:]
        quarter_idxs = quarter_idxs[-5:]

        annual_data = [{'date': date_cols[i].split('(')[0]} for i in annual_idxs]
        quarter_data = [{'date': date_cols[i].split('(')[0]} for i in quarter_idxs]
        estimate_data = [{'date': date_cols[i].split('(')[0].strip()} for i in estimate_idxs]

        rows = finance_table.select("tbody > tr")
        items_map_main = {
            "매출액": "revenue", "영업이익": "op_income", "당기순이익": "net_income",
            "영업이익률": "op_margin", "순이익률": "net_income_margin", "ROE": "roe",
            "부채비율": "debt_ratio", "당좌비율": "quick_ratio", "유보율": "reserve_ratio",
            "EPS": "eps", "BPS": "bps", "PER": "per", "PBR": "pbr",
            "주당배당금": "dps", "배당성향": "payout_ratio", "시가배당률": "dividend_yield",
        }

        def fill_data(target_list, indices):
            for i, idx in enumerate(indices):
                for row in rows:
                    th_text = row.th.text.strip().replace(" ", "")
                    key = None
                    for k_txt, k_key in items_map_main.items():
                        if k_txt in th_text:
                             if k_txt == "영업이익" and "률" in th_text: continue
                             if k_txt == "당기순이익" and "률" in th_text: continue
                             key = k_key
                             break

                    if key:
                        cells = row.select("td")
                        cell_offset = len(date_cols) - len(cells)
                        t_idx = idx - cell_offset
                        if 0 <= t_idx < len(cells):
                            target_list[i][key] = clean_float(cells[t_idx].text.strip())
                
                rev = target_list[i].get('revenue', 0)
                if rev and shares > 0:
                     sps = (rev * 100000000) / shares
                     target_list[i]['sps'] = sps
                     if current_price > 0: target_list[i]['psr'] = current_price / sps
        
        fill_data(annual_data, annual_idxs)
        fill_data(quarter_data, quarter_idxs)
        fill_data(estimate_data, estimate_idxs)
        
        return annual_data, quarter_data, estimate_data

    except:
        return [], [], []

def calculate_srim(bps, roe, rrr):
    if rrr <= 0: return 0
    excess_profit_rate = (roe - rrr) / 100
    fair_value = bps + (bps * excess_profit_rate / (rrr / 100))
    return fair_value


def calculate_srim_w(bps, roe, rrr, w):
    """[v3 §4.B] 초과이익 지속계수 w 반영 S-RIM.
    적정주가 = BPS + BPS×(ROE-r)×w/(1+r-w).  w=1 → 영구지속(기존 식과 동일)."""
    if rrr <= 0 or bps <= 0:
        return 0
    r = rrr / 100.0
    excess = (roe - rrr) / 100.0
    if w >= 1.0:
        return bps + bps * excess / r
    denom = (1 + r - w)
    if denom <= 0:
        return bps + bps * excess / r
    return bps + bps * excess * w / denom


def roe_volatility(roe_values):
    """[v3 §4.B/§3.5] ROE 평균·표준편차·변동계수(CV) + S-RIM 신뢰도 판정."""
    vals = [v for v in roe_values if v is not None]
    n = len(vals)
    if n == 0:
        return None
    mean = sum(vals) / n
    if n >= 2:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        std = var ** 0.5
    else:
        std = 0.0
    cv = (std / abs(mean) * 100) if mean else None
    if cv is None:
        grade = "판정불가"
    elif cv <= 30:
        grade = "안정 → S-RIM 정상 적용"
    elif cv <= 50:
        grade = "보통"
    else:
        grade = "과대 → S-RIM 참고용(신뢰구간 넓음)"
    return {"mean": mean, "std": std, "cv": cv, "grade": grade}

if 'search_key' not in st.session_state:
    st.session_state.search_key = 0 

def reset_search_state():
    st.session_state.search_key += 1 

# --- 메인 UI ---
def main():
    st.set_page_config(page_title="주식 적정주가 분석기", page_icon="📈")

    # [v3] 인쇄(PDF) 친화 + 넓은 표 깨짐 방지 전역 스타일
    st.markdown("""
    <style>
    @media print {
        .scroll-table { overflow: visible !important; white-space: normal !important; }
        .scroll-table table { font-size: 0.68rem !important; table-layout: fixed; word-break: break-all; width: 100% !important; }
        .scroll-table th, .scroll-table td { white-space: normal !important; padding: 4px !important; }
        .scroll-table th:first-child, .scroll-table td:first-child { position: static !important; }
        table { page-break-inside: auto; }
        tr { page-break-inside: avoid; }
        .asof-box { border: 1px solid #888 !important; }
    }
    .asof-box { background: rgba(3,199,90,0.08); border: 1px solid rgba(3,199,90,0.5);
        border-radius: 8px; padding: 10px 14px; margin: 6px 0 14px 0; font-size: 0.9rem; }
    .asof-box code { font-size: 0.8rem; color: #555; }
    @media (prefers-color-scheme: dark) { .asof-box code { color: #bbb; } }
    </style>
    """, unsafe_allow_html=True)
    
    if 'search_list' not in st.session_state:
        with st.spinner('종목 데이터 로딩 중...'):
            st.session_state.search_list, st.session_state.search_map, st.session_state.ticker_to_name = load_stock_data()
    
    search_list = st.session_state.search_list
    search_map = st.session_state.search_map
    ticker_to_name = st.session_state.ticker_to_name

    with st.sidebar:
        st.header("설정")
        required_return = st.number_input("요구수익률 (%)", 1.0, 20.0, 8.0, 0.5)

    st.markdown("##### 종목 검색")
    col_search, col_reset = st.columns([4, 1])
    
    ticker = None
    with col_search:
        if search_list:
            stock_input = st.selectbox(
                "종목을 선택하거나 입력하세요", 
                [""] + search_list,
                index=0,
                key=f"stock_selectbox_{st.session_state.search_key}",
                label_visibility="collapsed",
                placeholder="종목명 또는 코드를 입력하세요..."
            )
            if stock_input:
                ticker = search_map.get(stock_input)
        else:
            ticker_input = st.text_input("종목코드(6자리) 직접 입력")
            if ticker_input and len(ticker_input) == 6 and ticker_input.isdigit():
                ticker = ticker_input
    
    with col_reset:
        if st.button("🔄 초기화"):
            reset_search_state()
            st.cache_data.clear()
            st.rerun()

    if ticker:
        try:
            info = get_naver_stock_details(ticker)
            try: curr_price = float(info['now_price'].replace(',', ''))
            except: curr_price = 0
            
            annual_list, quarter_list, estimate_list = get_financials_from_naver(ticker, curr_price, info.get('shares', 0))
            investor_trends = get_investor_trend(ticker)
            industry_compare_df = get_same_industry_comparison(ticker)

            # [v3] 보조 데이터 (토큰 절감용)
            ts = get_data_timestamp()
            liq = get_liquidity_and_band(ticker, info.get('high_52'), info.get('low_52'), curr_price)
            macro = get_macro_indicators()

            st.markdown(f"### {info['name']} ({ticker})")

            # ============================================================
            # [v3 신설] 데이터 기준 시각 — Claude가 시세·수급 재검색 생략 판단
            # ============================================================
            adtv_txt = f"{liq['adtv']/1e8:,.1f}억원" if liq.get('adtv') else "N/A"
            usdkrw_txt = f"{macro['usdkrw']:,.1f}" if macro.get('usdkrw') else "N/A"
            kr10y_txt = f"{macro['kr10y']:.3f}%" if macro.get('kr10y') else "검색요(미수집)"
            st.markdown(f"""
            <div class="asof-box">
            📌 <b>데이터 기준</b>: {ts['human']} · <b>{ts['session']}</b><br>
            🔗 <b>출처</b>: 네이버 증권(시세·수급·재무·동일업종) / FinanceDataReader(거래대금·환율)<br>
            🌐 <b>시장지표</b>: USD/KRW {usdkrw_txt} · 국고채10Y {kr10y_txt} · 20일 ADTV {adtv_txt}
            <br><code>DATA_AS_OF={ts['iso']} SESSION={ts['session']} SRC=NAVER+FDR</code>
            </div>
            """, unsafe_allow_html=True)
            
            diff_color = "black"
            diff_arrow = ""
            if info['direction'] in ['up', 'upper']:
                diff_color = "#d20000"
                diff_arrow = "▲"
            elif info['direction'] in ['down', 'lower']:
                diff_color = "#0051c7"
                diff_arrow = "▼"
            
            st.markdown(f"""
            <div style="display:flex; align-items:flex-end; gap:10px; margin-bottom:10px;">
                <span style="font-size: 2.5rem; font-weight: bold; color:{diff_color};">{info['now_price']}</span>
                <span style="font-size: 1.2rem; color:{diff_color}; margin-bottom: 8px;">
                    {diff_arrow} {info['diff_amount']} ({info['diff_rate']}%)
                </span>
            </div>
            """, unsafe_allow_html=True)
            
            st.markdown("""
            <style>
            .stock-info-container { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 10px; margin-bottom: 12px; }
            @media (max-width: 600px) { .stock-info-container { grid-template-columns: repeat(2, 1fr); } }
            .stock-info-box { background-color: rgba(128, 128, 128, 0.1); padding: 10px; border-radius: 5px; text-align: center; }
            .stock-info-label { font-size: 12px; color: #666; margin-bottom: 4px; }
            .stock-info-value { font-size: 15px; font-weight: bold; color: #333; white-space: nowrap; }
            @media (prefers-color-scheme: dark) { .stock-info-label { color: #aaa; } .stock-info-value { color: #fff; } }
            </style>
            """, unsafe_allow_html=True)

            info_html = f"""
            <div class="stock-info-container">
                <div class="stock-info-box"><div class="stock-info-label">시가총액</div><div class="stock-info-value">{info['market_cap']}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">외국인소진율</div><div class="stock-info-value">{info['foreign_rate']}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">PER</div><div class="stock-info-value">{info['per']} 배</div></div>
                <div class="stock-info-box"><div class="stock-info-label">PBR</div><div class="stock-info-value">{info['pbr']} 배</div></div>
                <div class="stock-info-box"><div class="stock-info-label">52주 최고</div><div class="stock-info-value">{info['high_52']}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">52주 최저</div><div class="stock-info-value">{info['low_52']}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">EPS</div><div class="stock-info-value">{info['eps']} 원</div></div>
                <div class="stock-info-box"><div class="stock-info-label">배당수익률</div><div class="stock-info-value">{info['dvr']} %</div></div>
            </div>
            """
            st.markdown(info_html, unsafe_allow_html=True)

            # ============================================================
            # [v3 신설 §8.3] 유동성·가격대 (포지션 사이징 유동성 제약용)
            # ============================================================
            adtv_box = f"{liq['adtv']/1e8:,.1f} 억원" if liq.get('adtv') else "N/A"
            volr_box = f"{liq['vol_ratio']*100:,.0f} %" if liq.get('vol_ratio') else "N/A"
            band_box = f"{liq['band_pos']:.1f} %" if liq.get('band_pos') is not None else "N/A"
            st.markdown(f"""
            <div class="stock-info-container" style="grid-template-columns: repeat(3, 1fr);">
                <div class="stock-info-box"><div class="stock-info-label">20일 평균 거래대금(ADTV)</div><div class="stock-info-value">{adtv_box}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">당일 거래량(20일比)</div><div class="stock-info-value">{volr_box}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">52주 밴드 내 위치</div><div class="stock-info-value">{band_box}</div></div>
            </div>
            """, unsafe_allow_html=True)
            st.caption("※ ADTV는 종가×거래량 근사치(원), 출처: FinanceDataReader. 52주 위치 0%=저점·100%=고점.")

            with st.expander("기업 개요 보기"):
                st.write(info['overview'])

            st.markdown(f"""
                <a href="https://m.stock.naver.com/item/main.nhn?code={ticker}#/chart" target="_blank" style="text-decoration:none;">
                    <div style="background-color:#03C75A; color:white; padding:12px; border-radius:8px; text-align:center; font-weight:bold; margin: 15px 0;">
                        📊 네이버 증권 차트 보러가기
                    </div>
                </a>
                """, unsafe_allow_html=True)
            
            t_stamp = int(time.time())
            tab_d, tab_w, tab_m = st.tabs(["일봉", "주봉", "월봉"])
            with tab_d: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/day/{ticker}.png?t={t_stamp}", use_container_width=True)
            with tab_w: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/week/{ticker}.png?t={t_stamp}", use_container_width=True)
            with tab_m: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/month/{ticker}.png?t={t_stamp}", use_container_width=True)

            if investor_trends:
                st.markdown("### 🏢 외국인/기관 매매동향 (최근 10일)")
                total_inst = 0
                total_frgn = 0
                # [v3 §3.2] 순매수 '금액' 근사 (수량×종가) 누적 — 금액 요구 충족
                est_inst_value = 0.0
                est_frgn_value = 0.0
                for row in investor_trends:
                    close_v = _to_num(row.get('종가')) or 0
                    try:
                        iv = int(row['기관'].replace('+', '').replace(',', ''))
                        total_inst += iv
                        est_inst_value += iv * close_v
                    except: pass
                    try:
                        fv = int(row['외국인'].replace('+', '').replace(',', ''))
                        total_frgn += fv
                        est_frgn_value += fv * close_v
                    except: pass
                
                t_inst_color = "text-red" if total_inst > 0 else "text-blue" if total_inst < 0 else "text-black"
                t_inst_prefix = "+" if total_inst > 0 else "-" if total_inst < 0 else ""
                t_frgn_color = "text-red" if total_frgn > 0 else "text-blue" if total_frgn < 0 else "text-black"
                t_frgn_prefix = "+" if total_frgn > 0 else "-" if total_frgn < 0 else ""

                trend_html = """<style>
.trend-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-bottom: 8px; }
.trend-table th { background-color: rgba(128,128,128,0.1); text-align: center; padding: 6px; border-bottom: 1px solid rgba(128,128,128,0.2); }
.trend-table td { text-align: right; padding: 6px; border-bottom: 1px solid rgba(128,128,128,0.2); }
.total-row { background-color: rgba(128, 128, 128, 0.05); font-weight: bold; border-bottom: 2px solid rgba(128, 128, 128, 0.4); }
.text-red { color: #d20000; }
.text-blue { color: #0051c7; }
.text-black { color: inherit; }
@media (prefers-color-scheme: dark) { .text-black { color: #fff; } }
</style>
<div style="overflow-x:auto;">
<table class="trend-table">
<thead><tr><th>날짜</th><th>종가</th><th>등락률</th><th>기관</th><th>외국인</th><th>보유율</th></tr></thead>
<tbody>
"""
                trend_html += f"""<tr class="total-row"><td style="text-align:center;">10일 합계</td><td colspan="2" style="text-align:center;">-</td><td class="{t_inst_color}">{t_inst_prefix}{abs(total_inst):,}</td><td class="{t_frgn_color}">{t_frgn_prefix}{abs(total_frgn):,}</td><td>-</td></tr>"""

                for row in investor_trends:
                    inst_val_str = row['기관'].replace('+', '').replace(',', '')
                    try: inst_val = int(inst_val_str)
                    except: inst_val = 0
                    inst_color = "text-red" if inst_val > 0 else "text-blue" if inst_val < 0 else "text-black"
                    inst_prefix = "+" if inst_val > 0 else "-" if inst_val < 0 else ""
                    
                    frgn_val_str = row['외국인'].replace('+', '').replace(',', '')
                    try: frgn_val = int(frgn_val_str)
                    except: frgn_val = 0
                    frgn_color = "text-red" if frgn_val > 0 else "text-blue" if frgn_val < 0 else "text-black"
                    frgn_prefix = "+" if frgn_val > 0 else "-" if frgn_val < 0 else ""
                    
                    try: rate_val = float(row['등락률'].replace('%', ''))
                    except: rate_val = 0.0
                    rate_color = "text-red" if rate_val > 0 else "text-blue" if rate_val < 0 else "text-black"

                    trend_html += f'<tr><td style="text-align:center;">{row["날짜"]}</td><td style="text-align:right;">{row["종가"]}</td><td class="{rate_color}" style="text-align:right;">{row["등락률"]}</td><td class="{inst_color}" style="text-align:right;">{inst_prefix}{abs(inst_val):,}</td><td class="{frgn_color}" style="text-align:right;">{frgn_prefix}{abs(frgn_val):,}</td><td style="text-align:right;">{row["보유율"]}</td></tr>'
                
                trend_html += "</tbody></table></div>"
                st.markdown(trend_html, unsafe_allow_html=True)

                # [v3 §3.2] 금액 근사 + 4주체 한계 명시
                st.caption(
                    f"※ 10일 누적 순매수 금액(근사, 수량×종가): "
                    f"기관 ≈ {est_inst_value/1e8:,.0f}억원 · 외국인 ≈ {est_frgn_value/1e8:,.0f}억원. "
                    f"단위: 수량=주, 금액=원 근사. "
                    f"※ 네이버 frgn 페이지는 '기관' 합계만 제공 — 연기금 분리·개인은 미포함(필요 시 KRX 4주체 별도 수집)."
                )

            st.markdown("""
            <style>
            .scroll-table { overflow-x: auto; white-space: nowrap; margin-bottom: 10px; }
            .scroll-table table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
            .scroll-table th { text-align: center; padding: 8px; border-bottom: 1px solid #ddd; min-width: 80px; background-color: #f0f2f6; color: #000; }
            .scroll-table td { text-align: right; padding: 8px; border-bottom: 1px solid #ddd; }
            .scroll-table th:first-child, .scroll-table td:first-child { position: sticky; left: 0; z-index: 10; border-right: 2px solid #ccc; text-align: left; font-weight: bold; background-color: #ffffff; color: #000000; }
            @media (prefers-color-scheme: dark) {
                .scroll-table th { background-color: #262730; color: #fff; border-bottom: 1px solid #444; }
                .scroll-table td { border-bottom: 1px solid #444; color: #fff; }
                .scroll-table th:first-child, .scroll-table td:first-child { background-color: #0e1117; color: #fff; border-right: 2px solid #555; }
            }
            </style>
            """, unsafe_allow_html=True)

            # [v3 §5.2] 배당 항목 표시 추가 (DPS·배당성향·시가배당률)
            items_display = [
                ("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("영업이익률(%)", 'op_margin'),
                ("당기순이익(억)", 'net_income'), ("순이익률(%)", 'net_income_margin'),
                ("부채비율(%)", 'debt_ratio'), ("당좌비율(%)", 'quick_ratio'), ("유보율(%)", 'reserve_ratio'),
                ("EPS(원)", 'eps'), ("BPS(원)", 'bps'), ("SPS(원)", 'sps'),
                ("PER(배)", 'per'), ("PBR(배)", 'pbr'), ("PSR(배)", 'psr'),
                ("ROE(%)", 'roe'),
                ("주당배당금(원)", 'dps'), ("배당성향(%)", 'payout_ratio'), ("시가배당률(%)", 'dividend_yield'),
            ]

            def render_fin_table(title, data_list):
                disp = []
                cols = ['항목'] + [d['date'] for d in data_list]
                for label, key in items_display:
                    rowv = [label]
                    is_money = '원' in label or '억' in label
                    for d in data_list:
                        val = d.get(key, 0)
                        if val == 0 and key not in ['op_income', 'net_income']:
                            rowv.append("-")
                        else:
                            rowv.append(f"{val:,.0f}" if is_money else f"{val:,.2f}")
                    disp.append(rowv)
                df_ = pd.DataFrame(disp, columns=cols)
                html_ = df_.to_html(index=False, border=0, classes='scroll-table-content')
                st.markdown(f"### {title}")
                st.markdown(f'<div class="scroll-table">{html_}</div>', unsafe_allow_html=True)

            if annual_list:
                render_fin_table("📊 연간 재무제표 (최근 3년)", annual_list)
                st.caption("출처: 네이버 증권 기업실적분석 · 단위: 억원/원/%/배 · [Source: PDF]")

            if quarter_list:
                render_fin_table("📊 분기 재무제표 (최근 5분기)", quarter_list)
                st.caption("출처: 네이버 증권 기업실적분석 · 단위: 억원/원/%/배 · [Source: PDF]")

            # [v3 신설 §3.5/§7.2] 컨센서스 추정(E) — 기존엔 버려지던 데이터
            if estimate_list:
                render_fin_table("🔮 컨센서스 추정 (E)", estimate_list)
                st.caption("출처: 네이버 증권 추정 컨센서스(E) · forward 지표(§3.5 Y+1~, §7.2)에 사용 · [Source: PDF]")

            if not annual_list and not quarter_list:
                st.warning("재무 데이터를 불러올 수 없습니다.")

            if not industry_compare_df.empty:
                st.markdown("### 👯 동일업종 비교")
                html_compare = industry_compare_df.to_html(index=False, border=0, classes='scroll-table-content', escape=False)
                st.markdown(f'<div class="scroll-table">{html_compare}</div>', unsafe_allow_html=True)
                st.caption("출처: 네이버 증권 동일업종비교 · Peer 후보군(§4.E 선정기준은 분석 단계에서 적용) · [Source: PDF]")

            st.divider()
            st.markdown("### 💰 S-RIM 적정주가 분석")
            st.caption(f"요구수익률(Ke) {required_return:.1f}% 고정 적용 · [Source: PDF] "
                       f"※ Ke는 분석 단계에서 CAPM 재산정·정합성 점검(v3 §4.A) 대상")

            def show_srim_result(title, bps, roe_used, label_roe, roe_list=None):
                val = calculate_srim(bps, roe_used, required_return)
                excess_rate = roe_used - required_return
                
                st.markdown(f"#### {title}")
                if val > 0 and curr_price > 0:
                    diff_rate = (curr_price - val) / val * 100
                    diff_abs = abs(diff_rate)
                    if val > curr_price:
                        st.success(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 저평가** 상태입니다.")
                    else:
                        st.error(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 고평가** 상태입니다.")
                else:
                    st.warning("적정주가를 산출할 수 없습니다.")

                st.markdown("**🧮 산출 근거**")
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("*핵심 변수*")
                    input_df = pd.DataFrame({"구분": ["BPS", f"적용 ROE ({label_roe})"], "값": [f"{bps:,.0f} 원", f"{roe_used:.2f} %"]})
                    st.table(input_df)
                with c2:
                    st.markdown("*ROE 내역*")
                    if roe_list:
                        roe_df = pd.DataFrame(roe_list)
                        roe_df['ROE'] = roe_df['ROE'].apply(lambda x: f"{x:.2f} %")
                        st.table(roe_df)
                    else:
                        st.write(f"적용 ROE: {roe_used:.2f}%")

                # [v3 §4.B] ROE 변동성 진단 (S-RIM 신뢰도 판정 근거)
                if roe_list:
                    vol = roe_volatility([r['ROE'] for r in roe_list])
                    if vol:
                        cv_txt = f"{vol['cv']:.0f}%" if vol['cv'] is not None else "N/A"
                        st.markdown(
                            f"**📐 ROE 변동성**: 평균 {vol['mean']:.2f}% · 표준편차 {vol['std']:.2f}%p · "
                            f"변동계수(CV) {cv_txt} → **{vol['grade']}**"
                        )

                # [v3 §4.B] 지속계수 w 시나리오 (0.0 / 0.8 / 1.0)
                w_rows = []
                for w in [1.0, 0.8, 0.0]:
                    vw = calculate_srim_w(bps, roe_used, required_return, w)
                    gap = f"{(curr_price - vw)/vw*100:+.1f}%" if vw > 0 and curr_price > 0 else "-"
                    w_rows.append({"지속계수 w": f"{w:.1f}", "적정주가(원)": f"{vw:,.0f}", "현재가 괴리": gap})
                st.markdown("*지속계수(w) 시나리오 — w 채택근거는 §3.3 해자 등급과 연동*")
                st.table(pd.DataFrame(w_rows))

                with st.info("계산식"):
                    st.markdown(f"**① 초과이익률** = {roe_used:.2f}% (ROE) - {required_return}% (요구수익률) = **{excess_rate:.2f}%**")
                    st.markdown(f"**② 적정주가(w=1)** = {bps:,.0f} (BPS) + ( {bps:,.0f} × {excess_rate:.2f}% ÷ {required_return}% ) ≈ **{val:,.0f} 원**")

            if annual_list:
                bps_annual = annual_list[-1].get('bps', 0)
                roe_history_annual = []
                for d in annual_list:
                    if d.get('roe'): roe_history_annual.append({'연도': d['date'], 'ROE': d['roe']})
                
                roe_history_annual_3yr = roe_history_annual[-3:]
                avg_roe_annual = sum([r['ROE'] for r in roe_history_annual_3yr]) / len(roe_history_annual_3yr) if roe_history_annual_3yr else 0
                
                show_srim_result("1. 최근 3년 실적 평균 기준 (연간)", bps_annual, avg_roe_annual, "3년 평균", roe_history_annual_3yr)
            
            st.divider()

            if quarter_list:
                bps_quarter = quarter_list[-1].get('bps', 0)
                roe_history_quarter = []
                for d in quarter_list:
                    if d.get('roe'): roe_history_quarter.append({'분기': d['date'], 'ROE': d['roe']})
                
                roe_history_quarter_3q = roe_history_quarter[-3:]
                avg_roe_quarter = sum([r['ROE'] for r in roe_history_quarter_3q]) / len(roe_history_quarter_3q) if roe_history_quarter_3q else 0
                
                show_srim_result("2. 최근 3분기 실적 평균 기준 (분기)", bps_quarter, avg_roe_quarter, "3분기 평균", roe_history_quarter_3q)

        except Exception as e:
            st.error(f"오류 발생: {e}")

if __name__ == "__main__":
    main()
