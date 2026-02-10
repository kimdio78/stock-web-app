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
    """
    네이버 금융 메인 페이지에서 상세 주가 정보를 크롤링합니다.
    """
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        
        # 기본값 초기화
        data = {
            'name': ticker, 'overview': "정보 없음", 
            'now_price': '0', 'diff_rate': '0.00', 'diff_amount': '0', 'direction': 'flat',
            'market_cap': '-', 'foreign_rate': '-', 
            'per': '-', 'eps': '-', 'pbr': '-', 'bps': '-', 'dvr': '-',
            'high_52': '-', 'low_52': '-'
        }
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 1. 종목명
            name_tag = soup.select_one(".wrap_company h2 a")
            if name_tag:
                data['name'] = name_tag.text.strip()

            # 2. 기업 개요
            overview_div = soup.select_one("#summary_info")
            if overview_div:
                data['overview'] = "\n ".join([p.text.strip() for p in overview_div.select("p") if p.text.strip()])

            # 3. 현재가 및 등락률
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

            # 4. 시가총액
            try:
                mc_element = soup.select_one("#_market_sum")
                if mc_element:
                    data['market_cap'] = mc_element.text.strip().replace('\t', '').replace('\n', '') + " 억원"
            except: pass

            # 5. 투자정보 (PER, EPS, PBR, 배당수익률)
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

            # 6. 외국인소진율 (수정됨: table.lwidth 내에서 탐색)
            try:
                # 'lwidth' 클래스를 가진 테이블 안에서 '외국인소진율' 텍스트가 포함된 행 찾기
                lwidth_table = soup.select_one("table.lwidth")
                if lwidth_table:
                    for tr in lwidth_table.select("tr"):
                        if "외국인소진율" in tr.text:
                            # 해당 행의 em 태그값 추출
                            em = tr.select_one("td em")
                            if em:
                                data['foreign_rate'] = em.text.strip()
                            break
            except: pass

            # 7. 52주 최고/최저 (수정됨: table.rwidth 내에서 탐색)
            try:
                # 'rwidth' 클래스를 가진 테이블 안에서 '52주최고' 텍스트가 포함된 행 찾기
                rwidth_table = soup.select_one("table.rwidth")
                if rwidth_table:
                    for tr in rwidth_table.select("tr"):
                        if "52주최고" in tr.text:
                            # 해당 행의 td 안에 있는 em 태그들 추출 (순서대로 최고, 최저)
                            ems = tr.select("td em")
                            if len(ems) >= 2:
                                data['high_52'] = ems[0].text.strip()
                                data['low_52'] = ems[1].text.strip()
                            break
            except: pass
            
            # 8. BPS (table.per_table 내에서 탐색)
            try:
                per_table = soup.select_one("table.per_table")
                if per_table:
                    rows = per_table.select("tr")
                    for r in rows:
                        if "BPS" in r.text:
                            ems = r.select("em")
                            if len(ems) >= 2: # 보통 PBR 옆에 BPS가 위치함 (두 번째 em)
                                data['bps'] = ems[1].text.strip()
                            break
            except: pass

        return data
    except:
        return {'name': ticker, 'overview': "로딩 실패"}

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
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False)
        soup = BeautifulSoup(response.text, 'html.parser')
        finance_table = soup.select_one("div.section.cop_analysis > div.sub_section > table")
        if not finance_table: return [], {}

        header_rows = finance_table.select("thead > tr")
        date_columns = [th.text.strip() for th in header_rows[1].select("th")]
        first_data_row_cells = finance_table.select("tbody > tr:first-child > td")
        cell_offset = len(date_columns) - len(first_data_row_cells)

        num_annual_cols = 4
        for header in header_rows[0].select("th"):
            if "최근 연간 실적" in header.text:
                try: num_annual_cols = int(header['colspan'])
                except: pass
                break
        
        annual_indices = []
        search_end = cell_offset + num_annual_cols
        if len(date_columns) >= search_end:
            for i in range(search_end - 1, cell_offset - 1, -1):
                if "(E)" not in date_columns[i]: annual_indices.append(i)
        annual_indices = annual_indices[:3]

        quarter_idx = -1
        for i in range(len(date_columns)-1, -1, -1):
             if "(E)" not in date_columns[i] and i > search_end:
                 quarter_idx = i
                 break
        
        if not annual_indices: return [], {}

        annual_data = [{'date': date_columns[i].split('(')[0]} for i in annual_indices]
        quarter_data = {'date': date_columns[quarter_idx].split('(')[0]} if quarter_idx != -1 else {}

        rows = finance_table.select("tbody > tr")
        
        items_map = {
            "매출액": "revenue", "영업이익": "op_income", "영업이익률": "op_margin",
            "당기순이익": "net_income", "순이익률": "net_income_margin", "부채비율": "debt_ratio",
            "당좌비율": "quick_ratio", "유보율": "reserve_ratio",
            "ROE": "roe", "EPS": "eps", "PER": "per", "BPS": "bps", "PBR": "pbr"
        }

        for row in rows:
            th_text = row.th.text.strip()
            th_clean = th_text.replace("\n", "").replace(" ", "")
            key = None
            for k_text, k_code in items_map.items():
                if k_text in th_clean:
                    if k_text == "영업이익" and "률" in th_clean: continue
                    if k_text == "당기순이익" and "률" in th_clean: continue
                    key = k_code
                    break
            
            if key:
                cells = row.select("td")
                for i, idx in enumerate(annual_indices):
                    t_idx = idx - cell_offset
                    if 0 <= t_idx < len(cells):
                        val_text = cells[t_idx].text.strip()
                        annual_data[i][key] = clean_float(val_text)
                
                if quarter_idx != -1:
                    t_idx = quarter_idx - cell_offset
                    if 0 <= t_idx < len(cells):
                        val_text = cells[t_idx].text.strip()
                        quarter_data[key] = clean_float(val_text)
        
        annual_data.reverse()
        return annual_data, quarter_data
    except Exception:
        return [], {}

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
            # 1. 상세 정보 크롤링 (네이버)
            info = get_naver_stock_details(ticker)
            annual, quarter = get_financials_from_naver(ticker)
            
            # --- 상단 상세 정보 패널 ---
            st.markdown(f"### {info['name']} ({ticker})")
            
            # 가격 및 등락 표시
            diff_color = "black"
            diff_arrow = ""
            if info['direction'] in ['up', 'upper']:
                diff_color = "#d20000" # 빨강
                diff_arrow = "▲"
            elif info['direction'] in ['down', 'lower']:
                diff_color = "#0051c7" # 파랑
                diff_arrow = "▼"
            
            st.markdown(f"""
            <div style="display:flex; align-items:flex-end; gap:10px; margin-bottom:10px;">
                <span style="font-size: 2.5rem; font-weight: bold; color:{diff_color};">{info['now_price']}</span>
                <span style="font-size: 1.2rem; color:{diff_color}; margin-bottom: 8px;">
                    {diff_arrow} {info['diff_amount']} ({info['diff_rate']}%)
                </span>
            </div>
            """, unsafe_allow_html=True)
            
            # --- 상세 정보 그리드 (CSS 커스텀 디자인) ---
            # st.metric 대신 HTML/CSS Grid를 사용하여 폰트 크기 조정 및 잘림 방지
            st.markdown("""
            <style>
            .stock-info-container {
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 8px;
                margin-top: 10px;
                margin-bottom: 20px;
            }
            @media (max-width: 600px) {
                .stock-info-container {
                    grid-template-columns: repeat(2, 1fr);
                }
            }
            .stock-info-box {
                background-color: rgba(128, 128, 128, 0.1);
                padding: 10px;
                border-radius: 5px;
                text-align: center;
            }
            .stock-info-label {
                font-size: 12px;
                color: #666;
                margin-bottom: 4px;
            }
            .stock-info-value {
                font-size: 15px;
                font-weight: bold;
                color: #333;
                white-space: nowrap; /* 줄바꿈 방지 */
            }
            /* 다크모드 대응 */
            @media (prefers-color-scheme: dark) {
                .stock-info-label { color: #aaa; }
                .stock-info-value { color: #fff; }
            }
            </style>
            """, unsafe_allow_html=True)

            info_html = f"""
            <div class="stock-info-container">
                <div class="stock-info-box">
                    <div class="stock-info-label">시가총액</div>
                    <div class="stock-info-value">{info['market_cap']}</div>
                </div>
                <div class="stock-info-box">
                    <div class="stock-info-label">외국인소진율</div>
                    <div class="stock-info-value">{info['foreign_rate']}</div>
                </div>
                <div class="stock-info-box">
                    <div class="stock-info-label">PER</div>
                    <div class="stock-info-value">{info['per']} 배</div>
                </div>
                <div class="stock-info-box">
                    <div class="stock-info-label">PBR</div>
                    <div class="stock-info-value">{info['pbr']} 배</div>
                </div>
                <div class="stock-info-box">
                    <div class="stock-info-label">52주 최고</div>
                    <div class="stock-info-value">{info['high_52']}</div>
                </div>
                <div class="stock-info-box">
                    <div class="stock-info-label">52주 최저</div>
                    <div class="stock-info-value">{info['low_52']}</div>
                </div>
                <div class="stock-info-box">
                    <div class="stock-info-label">EPS</div>
                    <div class="stock-info-value">{info['eps']} 원</div>
                </div>
                <div class="stock-info-box">
                    <div class="stock-info-label">배당수익률</div>
                    <div class="stock-info-value">{info['dvr']} %</div>
                </div>
            </div>
            """
            st.markdown(info_html, unsafe_allow_html=True)

            with st.expander("기업 개요 보기"):
                st.write(info['overview'])

            # 차트 링크
            st.markdown(f"""
                <a href="https://m.stock.naver.com/item/main.nhn?code={ticker}#/chart" target="_blank" style="text-decoration:none;">
                    <div style="background-color:#03C75A; color:white; padding:12px; border-radius:8px; text-align:center; font-weight:bold; margin: 15px 0;">
                        📊 네이버 증권 차트 보러가기
                    </div>
                </a>
                """, unsafe_allow_html=True)
            
            # 차트 이미지
            t_stamp = int(time.time())
            tab_d, tab_w, tab_m = st.tabs(["일봉", "주봉", "월봉"])
            with tab_d: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/day/{ticker}.png?t={t_stamp}", use_container_width=True)
            with tab_w: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/week/{ticker}.png?t={t_stamp}", use_container_width=True)
            with tab_m: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/month/{ticker}.png?t={t_stamp}", use_container_width=True)

            if annual:
                st.markdown("### 📊 재무 요약")
                disp_data = []
                cols = ['항목'] + [d['date'] for d in annual] + ['최근분기']
                
                items_display = [
                    ("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("영업이익률(%)", 'op_margin'),
                    ("당기순이익(억)", 'net_income'), ("순이익률(%)", 'net_income_margin'),
                    ("부채비율(%)", 'debt_ratio'), ("당좌비율(%)", 'quick_ratio'), ("유보율(%)", 'reserve_ratio'),
                    ("EPS(원)", 'eps'), ("BPS(원)", 'bps'), ("PER(배)", 'per'), ("PBR(배)", 'pbr'), ("ROE(%)", 'roe')
                ]
                
                for label, key in items_display:
                    row = [label]
                    is_money = '원' in label or '억' in label
                    
                    for d in annual:
                        val = d.get(key, 0)
                        if val == 0 and key not in ['op_income', 'net_income']: row.append("-")
                        else: row.append(f"{val:,.0f}" if is_money else f"{val:,.2f}")
                    
                    q_val = quarter.get(key, 0)
                    if q_val == 0 and key not in ['op_income', 'net_income']: row.append("-")
                    else: row.append(f"{q_val:,.0f}" if is_money else f"{q_val:,.2f}")
                        
                    disp_data.append(row)
                
                df_table = pd.DataFrame(disp_data, columns=cols)
                
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
                
                html = df_table.to_html(index=False, border=0, classes='scroll-table-content')
                st.markdown(f'<div class="scroll-table">{html}</div>', unsafe_allow_html=True)

                st.divider()
                st.markdown("### 💰 S-RIM 적정주가 분석")
                
                bps = annual[-1].get('bps', 0)
                roe_history = []
                for d in annual:
                    if d.get('roe'): roe_history.append({'연도': d['date'], 'ROE': d['roe']})
                roe_history = roe_history[-3:]
                avg_roe = sum([r['ROE'] for r in roe_history]) / len(roe_history) if roe_history else 0
                roe_1yr = annual[-1].get('roe', 0)

                val_3yr = calculate_srim(bps, avg_roe, required_return)
                val_1yr = calculate_srim(bps, roe_1yr, required_return)
                
                # 현재가 업데이트 (크롤링한 최신값 사용)
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
