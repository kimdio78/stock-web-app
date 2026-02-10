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

            # 상장주식수 추출
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
    """
    1차: WiseReport 크롤링 (상세, 5년)
    2차: 네이버 금융 메인 (백업, 3~4년)
    """
    # 1차 시도: WiseReport
    try:
        url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False, timeout=5)
        
        if response.status_code == 200:
            dfs = pd.read_html(response.text, encoding='utf-8')
            df_fin = None
            for df in dfs:
                if df.shape[1] > 0 and df.iloc[:, 0].astype(str).str.contains('매출액').any():
                    df_fin = df
                    break
            
            if df_fin is not None:
                # WiseReport 데이터 처리 로직 (기존과 동일)
                df_fin = df_fin.set_index(df_fin.columns[0])
                cols = df_fin.columns
                if isinstance(cols, pd.MultiIndex): date_cols = [c[1] for c in cols]
                else: date_cols = cols

                n_cols = len(cols)
                mid_point = n_cols // 2
                
                annual_data = []
                for i in range(mid_point):
                    col_name = str(date_cols[i])
                    if "(E)" not in col_name: annual_data.append({'date': col_name, 'col_idx': i})
                annual_data = annual_data[-5:]

                quarter_data = []
                for i in range(mid_point, n_cols):
                    col_name = str(date_cols[i])
                    if "(E)" not in col_name: quarter_data.append({'date': col_name, 'col_idx': i})
                quarter_data = quarter_data[-5:]

                items_map = {
                    "매출액": "revenue", "영업이익": "op_income", "당기순이익": "net_income",
                    "영업이익률": "op_margin", "순이익률": "net_income_margin", "ROE": "roe",
                    "부채비율": "debt_ratio", "당좌비율": "quick_ratio", "유보율": "reserve_ratio",
                    "EPS": "eps", "BPS": "bps", "PER": "per", "PBR": "pbr",
                    "주당배당금": "dps", "배당성향": "payout_ratio", "시가배당률": "dividend_yield",
                    "이자보상배율": "interest_coverage_ratio", "EV/EBITDA": "ev_ebitda", 
                    "영업활동현금흐름": "operating_cash_flow" 
                }
                
                def extract_data(data_list, is_quarter=False):
                    result = []
                    for d in data_list:
                        item_dict = {'date': d['date']}
                        for idx_name, row in df_fin.iterrows():
                            idx_clean = str(idx_name).replace(" ", "").replace("\xa0", "")
                            val = row.iloc[d['col_idx']]
                            for k_txt, k_key in items_map.items():
                                if k_txt in idx_clean:
                                    if k_txt == "영업이익" and "률" in idx_clean: continue
                                    if k_txt == "당기순이익" and "률" in idx_clean: continue
                                    item_dict[k_key] = clean_float(str(val))
                                    break
                        
                        # 지표 계산
                        revenue = item_dict.get('revenue', 0)
                        if revenue and shares > 0:
                            sps = (revenue * 100000000) / shares
                            item_dict['sps'] = sps if not is_quarter else sps * 4 # 분기는 연환산? 단순화
                        
                        ocf = item_dict.get('operating_cash_flow', 0)
                        if ocf and shares > 0:
                            item_dict['cps'] = (ocf * 100000000) / shares

                        if current_price > 0:
                            if item_dict.get('sps'): item_dict['psr'] = current_price / item_dict['sps']
                            if item_dict.get('cps'): item_dict['pcr'] = current_price / item_dict['cps']

                        result.append(item_dict)
                    return result

                return extract_data(annual_data), extract_data(quarter_data, is_quarter=True)
    except:
        pass

    # 2차 시도: 네이버 금융 메인 (Fallback)
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False)
        soup = BeautifulSoup(response.text, 'html.parser')
        finance_table = soup.select_one("div.section.cop_analysis > div.sub_section > table")
        if not finance_table: return [], []

        header_rows = finance_table.select("thead > tr")
        date_cols = [th.text.strip() for th in header_rows[1].select("th")]
        
        # 인덱스 구분 (단순화: 앞 4개 연간, 뒤 6개 분기 가정)
        annual_idxs = [i for i, x in enumerate(date_cols[:4]) if "(E)" not in x]
        quarter_idxs = [i+4 for i, x in enumerate(date_cols[4:]) if "(E)" not in x]

        annual_data = [{'date': date_cols[i].split('(')[0]} for i in annual_idxs]
        quarter_data = [{'date': date_cols[i].split('(')[0]} for i in quarter_idxs]

        rows = finance_table.select("tbody > tr")
        items_map_main = {
            "매출액": "revenue", "영업이익": "op_income", "당기순이익": "net_income",
            "영업이익률": "op_margin", "순이익률": "net_income_margin", "ROE": "roe",
            "부채비율": "debt_ratio", "당좌비율": "quick_ratio", "유보율": "reserve_ratio",
            "EPS": "eps", "BPS": "bps", "PER": "per", "PBR": "pbr",
            "주당배당금": "dps", "배당성향": "payout_ratio", "시가배당률": "dividend_yield",
            "이자보상배율": "interest_coverage_ratio"
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
                    if "이자보상배율" in th_text: key = "interest_coverage_ratio"

                    if key:
                        cells = row.select("td")
                        cell_offset = len(date_cols) - len(cells) # 보통 offset 0
                        t_idx = idx - cell_offset
                        if 0 <= t_idx < len(cells):
                            target_list[i][key] = clean_float(cells[t_idx].text.strip())
                
                # 추가 지표 계산 (SPS, PSR 등)
                rev = target_list[i].get('revenue', 0)
                if rev and shares > 0:
                     sps = (rev * 100000000) / shares
                     target_list[i]['sps'] = sps
                     if current_price > 0: target_list[i]['psr'] = current_price / sps
        
        fill_data(annual_data, annual_idxs)
        fill_data(quarter_data, quarter_idxs)
        
        return annual_data, quarter_data

    except:
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
            try: curr_price = float(info['now_price'].replace(',', ''))
            except: curr_price = 0
            
            annual_list, quarter_list = get_financials_from_naver(ticker, curr_price, info.get('shares', 0))
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
            .result-text { font-size: 1.1em; line-height: 1.6; color: #333333; }
            .calc-box { background-color: #f8f9fa; border-radius: 8px; padding: 15px; margin-top: 10px; font-family: sans-serif; color: #333333; }
            .calc-box strong { color: #000000; }
            </style>
            """, unsafe_allow_html=True)

            items_display = [
                ("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("영업이익률(%)", 'op_margin'),
                ("당기순이익(억)", 'net_income'), ("순이익률(%)", 'net_income_margin'),
                ("부채비율(%)", 'debt_ratio'), ("당좌비율(%)", 'quick_ratio'), ("유보율(%)", 'reserve_ratio'),
                ("EPS(원)", 'eps'), ("BPS(원)", 'bps'), ("CPS(원)", 'cps'), ("SPS(원)", 'sps'),
                ("PER(배)", 'per'), ("PBR(배)", 'pbr'), ("PCR(배)", 'pcr'), ("PSR(배)", 'psr'),
                ("EV/EBITDA(배)", 'ev_ebitda'), ("ROE(%)", 'roe'), ("이자보상배율(배)", 'interest_coverage_ratio')
            ]

            if annual_list:
                st.markdown("### 📊 연간 재무제표 (최근 5년)")
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

            if quarter_list:
                st.markdown("### 📊 분기 재무제표 (최근 5분기)")
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

            if not annual_list and not quarter_list:
                st.warning("재무 데이터를 불러올 수 없습니다.")

            st.divider()
            st.markdown("### 💰 S-RIM 적정주가 분석")

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

                with st.info("계산식"):
                    st.markdown(f"**① 초과이익률** = {roe_used:.2f}% (ROE) - {required_return}% (요구수익률) = **{excess_rate:.2f}%**")
                    st.markdown(f"**② 적정주가** = {bps:,.0f} (BPS) + ( {bps:,.0f} × {excess_rate:.2f}% ÷ {required_return}% ) ≈ **{val:,.0f} 원**")

            # 1. 최근 3년 실적 평균 기준 (연간)
            if annual_list:
                bps_annual = annual_list[-1].get('bps', 0)
                roe_history_annual = []
                for d in annual_list:
                    if d.get('roe'): roe_history_annual.append({'연도': d['date'], 'ROE': d['roe']})
                
                roe_history_annual_3yr = roe_history_annual[-3:]
                avg_roe_annual = sum([r['ROE'] for r in roe_history_annual_3yr]) / len(roe_history_annual_3yr) if roe_history_annual_3yr else 0
                
                show_srim_result("1. 최근 3년 실적 평균 기준 (연간)", bps_annual, avg_roe_annual, "3년 평균", roe_history_annual_3yr)
            
            st.divider()

            # 2. 최근 3분기 실적 평균 기준 (분기)
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
