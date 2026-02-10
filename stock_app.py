import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import urllib3
import FinanceDataReader as fdr
import time
import re
import webbrowser

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
            'market_cap': '-', 'foreign_rate': '-', 
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
                per_el = soup.select_one("#_per")
                if per_el: data['per'] = per_el.text.strip()
                eps_el = soup.select_one("#_eps")
                if eps_el: data['eps'] = eps_el.text.strip()
                pbr_el = soup.select_one("#_pbr")
                if pbr_el: data['pbr'] = pbr_el.text.strip()
                dvr_el = soup.select_one("#_dvr")
                if dvr_el: data['dvr'] = dvr_el.text.strip()
            except: pass

            # 테이블 매칭 로직 (외국인소진율, 52주, BPS 등)
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
        return {'name': ticker, 'overview': "로딩 실패"}

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

def get_financials_from_naver(ticker):
    """
    네이버 금융에서 연간(최근 3년), 분기(최근 3분기) 데이터를 분리하여 가져옵니다.
    """
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False)
        soup = BeautifulSoup(response.text, 'html.parser')
        finance_table = soup.select_one("div.section.cop_analysis > div.sub_section > table")
        if not finance_table: return [], []

        header_rows = finance_table.select("thead > tr")
        if len(header_rows) < 2: return [], []

        # 1. 헤더 분석 (연간 vs 분기 구분)
        # 보통 첫 번째 tr의 th colspan으로 연간/분기 구간 확인
        # 구조: [주요재무정보] [최근 연간 실적(4칸)] [최근 분기 실적(6칸)]
        
        main_headers = header_rows[0].select("th")
        date_headers = header_rows[1].select("th")
        
        # 날짜 컬럼 텍스트 추출
        date_cols = [th.text.strip() for th in date_headers]
        
        # 연간/분기 컬럼 인덱스 찾기
        annual_cols_idx = []
        quarter_cols_idx = []
        
        current_idx = 0
        
        # 첫 번째 열(주요재무정보 등) 건너뛰기 로직 보정
        # date_headers의 개수가 실제 데이터 열 개수와 일치한다고 가정
        
        for th in main_headers:
            colspan = int(th.get('colspan', 1))
            text = th.text.strip()
            
            if "연간" in text:
                # 해당 구간의 인덱스 수집
                for i in range(colspan):
                    if current_idx < len(date_cols):
                        # (E) 추정치가 아닌 최근 3개년 확보를 위해 전체 수집 후 후처리
                         annual_cols_idx.append(current_idx)
                    current_idx += 1
            elif "분기" in text:
                for i in range(colspan):
                    if current_idx < len(date_cols):
                         quarter_cols_idx.append(current_idx)
                    current_idx += 1
            else:
                # 데이터 열이 아닌 경우 (첫번째 컬럼 등) 인덱스만 증가시키지 않거나 상황에 따라 처리
                # 보통 네이버 테이블은 첫 열이 row header이므로 date_headers는 데이터 열만 가짐
                # 하지만 thead 구조상 2줄이므로 정확히 매칭해야 함.
                # 편의상 date_cols 전체를 순회하며 (E) 제외 로직 적용
                pass
        
        # 만약 위 로직으로 인덱스를 못 잡았다면(구조 변경 등), 단순 개수 기반 접근 (Fall-back)
        if not annual_cols_idx and not quarter_cols_idx:
             # 보통 앞쪽 4개가 연간, 뒤쪽 6개가 분기
             annual_cols_idx = [0, 1, 2, 3]
             quarter_cols_idx = [4, 5, 6, 7, 8, 9]

        # 2. 인덱스 필터링 (최근 3개년/3분기)
        # 연간: (E) 제외하고 최근 3개
        final_annual_idx = []
        for i in annual_cols_idx:
            if i < len(date_cols):
                if "(E)" not in date_cols[i]:
                     final_annual_idx.append(i)
                else:
                    # 추정치도 포함하고 싶다면 여기 수정. 일단 확정치 기준
                    pass
        # 뒤에서 3개 선택 (과거 -> 최근 순이므로)
        final_annual_idx = final_annual_idx[-3:]
        
        # 분기: (E) 제외하고 최근 3개
        final_quarter_idx = []
        for i in quarter_cols_idx:
             if i < len(date_cols):
                if "(E)" not in date_cols[i]:
                    final_quarter_idx.append(i)
        final_quarter_idx = final_quarter_idx[-3:]

        # 3. 데이터 추출
        annual_data = [{'date': date_cols[i].split('(')[0]} for i in final_annual_idx]
        quarter_data = [{'date': date_cols[i].split('(')[0]} for i in final_quarter_idx]

        rows = finance_table.select("tbody > tr")
        
        # 매핑 정의 (요청하신 항목 추가)
        items_map = {
            "매출액": "revenue", "영업이익": "op_income", "영업이익률": "op_margin",
            "당기순이익": "net_income", "순이익률": "net_income_margin",
            "부채비율": "debt_ratio", "당좌비율": "quick_ratio", "유보율": "reserve_ratio",
            "ROE": "roe", "EPS": "eps", "PER": "per", "BPS": "bps", "PBR": "pbr",
            "이자보상배율": "interest_coverage_ratio",
            # 추가 요청 항목
            "CPS": "cps", "SPS": "sps", 
            "PCR": "pcr", "PSR": "psr", "EV/EBITDA": "ev_ebitda"
        }

        for row in rows:
            th_text = row.th.text.strip()
            th_clean = th_text.replace("\n", "").replace(" ", "").upper() # 영어 대문자 변환
            
            key = None
            for k_text, k_code in items_map.items():
                # 한글/영문 혼용 매칭
                if k_text.upper().replace(" ", "") in th_clean:
                    # 예외 처리
                    if k_text == "영업이익" and "률" in th_clean: continue
                    if k_text == "당기순이익" and "률" in th_clean: continue
                    key = k_code
                    break
            
            # 이자보상배율 별도 체크
            if "이자보상배율" in th_clean: key = "interest_coverage_ratio"

            if key:
                cells = row.select("td")
                
                # 연간 데이터 채우기
                for i, idx in enumerate(final_annual_idx):
                    if idx < len(cells):
                        val_text = cells[idx].text.strip()
                        annual_data[i][key] = clean_float(val_text)
                
                # 분기 데이터 채우기
                for i, idx in enumerate(final_quarter_idx):
                    if idx < len(cells):
                        val_text = cells[idx].text.strip()
                        quarter_data[i][key] = clean_float(val_text)
        
        # 최신순 정렬 (최근 데이터가 왼쪽/위로 오게 하려면 reverse)
        # 하지만 보통 표는 과거 -> 현재(오른쪽) 이므로 그대로 둠
        # UI 표출 시에는 컬럼 순서대로 나옴
        return annual_data, quarter_data
    except Exception:
        return [], []

def calculate_srim(bps, roe, rrr):
    if rrr <= 0: return 0
    excess_profit_rate = (roe - rrr) / 100
    fair_value = bps + (bps * excess_profit_rate / (rrr / 100))
    return fair_value

if 'search_key' not in st.session_state:
    st.session_state.search_key = 0 

def reset_search_state():
    st.session_state.search_key += 1 

# --- 메인 UI ---
def main():
    st.set_page_config(page_title="주식 적정주가 분석기", page_icon="📈")
    
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
            annual_list, quarter_list = get_financials_from_naver(ticker)
            investor_trends = get_investor_trend(ticker)
            
            st.markdown(f"### {info['name']} ({ticker})")
            
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
            .stock-info-container { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 10px; margin-bottom: 20px; }
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
                for row in investor_trends:
                    try: total_inst += int(row['기관'].replace('+', '').replace(',', ''))
                    except: pass
                    try: total_frgn += int(row['외국인'].replace('+', '').replace(',', ''))
                    except: pass
                
                t_inst_color = "text-red" if total_inst > 0 else "text-blue" if total_inst < 0 else "text-black"
                t_inst_prefix = "+" if total_inst > 0 else ""
                t_frgn_color = "text-red" if total_frgn > 0 else "text-blue" if total_frgn < 0 else "text-black"
                t_frgn_prefix = "+" if total_frgn > 0 else ""

                trend_html = """<style>
.trend-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-bottom: 20px; }
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
                trend_html += f"""<tr class="total-row"><td style="text-align:center;">10일 합계</td><td colspan="2" style="text-align:center;">-</td><td class="{t_inst_color}">{t_inst_prefix}{total_inst:,}</td><td class="{t_frgn_color}">{t_frgn_prefix}{total_frgn:,}</td><td>-</td></tr>"""

                for row in investor_trends:
                    inst_val_str = row['기관'].replace('+', '').replace(',', '')
                    try: inst_val = int(inst_val_str)
                    except: inst_val = 0
                    inst_color = "text-red" if inst_val > 0 else "text-blue" if inst_val < 0 else "text-black"
                    inst_prefix = "+" if inst_val > 0 else ""
                    
                    frgn_val_str = row['외국인'].replace('+', '').replace(',', '')
                    try: frgn_val = int(frgn_val_str)
                    except: frgn_val = 0
                    frgn_color = "text-red" if frgn_val > 0 else "text-blue" if frgn_val < 0 else "text-black"
                    frgn_prefix = "+" if frgn_val > 0 else ""
                    
                    try: rate_val = float(row['등락률'].replace('%', ''))
                    except: rate_val = 0.0
                    rate_color = "text-red" if rate_val > 0 else "text-blue" if rate_val < 0 else "text-black"

                    trend_html += f'<tr><td style="text-align:center;">{row["날짜"]}</td><td style="text-align:right;">{row["종가"]}</td><td class="{rate_color}" style="text-align:right;">{row["등락률"]}</td><td class="{inst_color}" style="text-align:right;">{inst_prefix}{abs(inst_val):,}</td><td class="{frgn_color}" style="text-align:right;">{frgn_prefix}{abs(frgn_val):,}</td><td style="text-align:right;">{row["보유율"]}</td></tr>'
                
                trend_html += "</tbody></table></div>"
                st.markdown(trend_html, unsafe_allow_html=True)

            if annual_list:
                # --- 공통 스타일 (가로 스크롤) ---
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

                items_display = [
                    ("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("영업이익률(%)", 'op_margin'),
                    ("당기순이익(억)", 'net_income'), ("순이익률(%)", 'net_income_margin'),
                    ("부채비율(%)", 'debt_ratio'), ("당좌비율(%)", 'quick_ratio'), ("유보율(%)", 'reserve_ratio'),
                    ("EPS(원)", 'eps'), ("BPS(원)", 'bps'), ("CPS(원)", 'cps'), ("SPS(원)", 'sps'),
                    ("PER(배)", 'per'), ("PBR(배)", 'pbr'), ("PCR(배)", 'pcr'), ("PSR(배)", 'psr'),
                    ("EV/EBITDA(배)", 'ev_ebitda'), ("ROE(%)", 'roe')
                ]

                # --- 1. 연간 재무제표 (최근 3년) ---
                st.markdown("### 📊 연간 재무제표 (최근 3년)")
                disp_annual = []
                cols_annual = ['항목'] + [d['date'] for d in annual_list]
                
                for label, key in items_display:
                    row = [label]
                    is_money = '원' in label or '억' in label
                    
                    for d in annual_list:
                        val = d.get(key, 0)
                        if val == 0 and key not in ['op_income', 'net_income']: row.append("-")
                        else: row.append(f"{val:,.0f}" if is_money else f"{val:,.2f}")
                    disp_annual.append(row)
                
                df_annual = pd.DataFrame(disp_annual, columns=cols_annual)
                html_annual = df_annual.to_html(index=False, border=0, classes='scroll-table-content')
                st.markdown(f'<div class="scroll-table">{html_annual}</div>', unsafe_allow_html=True)

                # --- 2. 분기 재무제표 (최근 3분기) ---
                if quarter_list:
                    st.markdown("### 📊 분기 재무제표 (최근 3분기)")
                    disp_quarter = []
                    cols_quarter = ['항목'] + [d['date'] for d in quarter_list]
                    
                    for label, key in items_display:
                        row = [label]
                        is_money = '원' in label or '억' in label
                        
                        for d in quarter_list:
                            val = d.get(key, 0)
                            if val == 0 and key not in ['op_income', 'net_income']: row.append("-")
                            else: row.append(f"{val:,.0f}" if is_money else f"{val:,.2f}")
                        disp_quarter.append(row)

                    df_quarter = pd.DataFrame(disp_quarter, columns=cols_quarter)
                    html_quarter = df_quarter.to_html(index=False, border=0, classes='scroll-table-content')
                    st.markdown(f'<div class="scroll-table">{html_quarter}</div>', unsafe_allow_html=True)

                st.divider()
                st.markdown("### 💰 S-RIM 적정주가 분석")
                
                # 적정주가 계산은 연간 데이터의 가장 최근 BPS와 ROE 사용 (또는 3년 평균)
                if annual_list:
                    bps = annual_list[-1].get('bps', 0)
                    roe_history = []
                    for d in annual_list:
                        if d.get('roe'): roe_history.append({'연도': d['date'], 'ROE': d['roe']})
                    
                    avg_roe = sum([r['ROE'] for r in roe_history]) / len(roe_history) if roe_history else 0
                    roe_1yr = annual_list[-1].get('roe', 0)

                    val_3yr = calculate_srim(bps, avg_roe, required_return)
                    val_1yr = calculate_srim(bps, roe_1yr, required_return)
                    
                    try: curr_price_float = float(info['now_price'].replace(',', ''))
                    except: curr_price_float = 0

                    def show_analysis_result(val, roe_used, label_roe, roe_table_data=None):
                        if val > 0 and curr_price_float > 0:
                            diff_rate = (curr_price_float - val) / val * 100
                            diff_abs = abs(diff_rate)
                            if val > curr_price_float:
                                st.success(f"현재가({curr_price_float:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 저평가** 상태입니다.")
                            else:
                                st.error(f"현재가({curr_price_float:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 고평가** 상태입니다.")
                        else:
                            st.warning("적정주가를 산출할 수 없습니다.")

                        st.markdown("#### 🧮 산출 근거")
                        col_input1, col_input2 = st.columns(2)
                        with col_input1:
                            st.markdown("**1. 핵심 변수**")
                            input_df = pd.DataFrame({
                                "구분": ["BPS", f"ROE ({label_roe})"],
                                "값": [f"{bps:,.0f} 원", f"{roe_used:.2f} %"]
                            })
                            st.table(input_df)
                        with col_input2:
                            if roe_table_data:
                                st.markdown("**2. ROE 상세 내역 (최근 3년)**")
                                roe_df = pd.DataFrame(roe_table_data)
                                roe_df['ROE'] = roe_df['ROE'].apply(lambda x: f"{x:.2f} %")
                                st.table(roe_df)
                            else:
                                st.markdown("**2. ROE 상세 내역**")
                                st.write(f"최근 결산 ROE: {roe_used:.2f}%")

                        st.markdown("**3. 계산 과정**")
                        excess_rate = roe_used - required_return
                        with st.info("상세 계산 내역"):
                            st.markdown(f"**① 초과이익률**")
                            st.latex(rf" \text{{ROE}} ({roe_used:.2f}\%) - \text{{요구수익률}} ({required_return}\%) = \mathbf{{{excess_rate:.2f}\%}}")
                            st.markdown(f"**② 적정주가 (S-RIM)**")
                            st.latex(rf" {bps:,.0f} + \left( {bps:,.0f} \times \frac{{{excess_rate:.2f}\%}}{{{required_return}\%}} \right) \approx \mathbf{{{val:,.0f} \text{{ 원}}}}")

                    tab1, tab2 = st.tabs(["📉 3년 실적 평균 기준", "🆕 최근 1년 실적 기준"])
                    with tab1:
                        st.caption("최근 3년간의 평균 ROE를 사용하여 실적 변동성을 줄인 장기 가치입니다.")
                        show_analysis_result(val_3yr, avg_roe, "3년 평균", roe_table_data=roe_history)
                    with tab2:
                        st.caption("가장 최근 결산 연도의 ROE만을 사용하여 최신 실적 추세를 반영한 가치입니다.")
                        show_analysis_result(val_1yr, roe_1yr, "최근 1년")

        except Exception as e:
            st.error(f"오류 발생: {e}")

if __name__ == "__main__":
    main()
