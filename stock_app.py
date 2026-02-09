import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import urllib3
import FinanceDataReader as fdr
from pykrx import stock
import time
import re
import webbrowser

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 데이터 수집 함수들 ---
@st.cache_data(ttl=3600)
def load_stock_data():
    """
    종목 리스트를 불러옵니다.
    1차로 FinanceDataReader를 시도하고, 실패 시 pykrx로 2차 시도합니다.
    """
    # 1. FinanceDataReader 시도
    try:
        df = fdr.StockListing('KRX')
        if not df.empty:
            df['Search_Key'] = df['Name'] + " (" + df['Code'] + ")"
            search_map = dict(zip(df['Search_Key'], df['Code']))
            ticker_to_name = dict(zip(df['Code'], df['Name']))
            search_list = list(search_map.keys())
            return search_list, search_map, ticker_to_name
    except Exception:
        pass
    
    # 2. pykrx 시도 (Fallback)
    try:
        # 최근 영업일을 찾기 위해 오늘부터 7일 전까지 역순으로 조회
        for i in range(7):
            target_date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            try:
                # 전체 종목 시가총액 조회 (여기에 종목명이 포함됨)
                df = stock.get_market_cap_by_ticker(target_date, market="ALL")
                if not df.empty:
                    df = df.reset_index() # 티커를 컬럼으로 변환
                    df['Search_Key'] = df['종목명'] + " (" + df['티커'] + ")"
                    search_map = dict(zip(df['Search_Key'], df['티커']))
                    ticker_to_name = dict(zip(df['티커'], df['종목명']))
                    search_list = list(search_map.keys())
                    return search_list, search_map, ticker_to_name
            except:
                continue
    except Exception:
        pass

    return [], {}, {}

def get_company_info_from_naver(ticker):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        
        info = {'name': ticker, 'overview': "정보 없음", 'market_cap': 0}
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            name_tag = soup.select_one(".wrap_company h2 a")
            if name_tag:
                info['name'] = name_tag.text.strip()

            overview_div = soup.select_one("#summary_info")
            if overview_div:
                info['overview'] = "\n ".join([p.text.strip() for p in overview_div.select("p") if p.text.strip()])
            
            try:
                mc_element = soup.select_one("#_market_sum")
                if mc_element:
                    raw_mc = mc_element.text.strip()
                    market_cap_okwon = 0
                    if '조' in raw_mc:
                        parts = raw_mc.split('조')
                        trillion_part = parts[0].strip().replace(',', '')
                        billion_part = parts[1].strip().replace(',', '')
                        trillion = int(trillion_part) if trillion_part else 0
                        billion = int(billion_part) if billion_part else 0
                        market_cap_okwon = trillion * 10000 + billion
                    else:
                        market_cap_okwon = int(raw_mc.replace(',', ''))
                    
                    info['market_cap'] = market_cap_okwon * 100000000
            except:
                pass
        return info
    except:
        return {'name': ticker, 'overview': "로딩 실패", 'market_cap': 0}

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
            "매출액": "revenue",
            "영업이익": "op_income",
            "영업이익률": "op_margin",
            "당기순이익": "net_income",
            "순이익률": "net_income_margin",
            "부채비율": "debt_ratio",
            "당좌비율": "quick_ratio",
            "유보율": "reserve_ratio",
            "ROE": "roe",
            "EPS": "eps",
            "PER": "per",
            "BPS": "bps",
            "PBR": "pbr",
            "이자보상배율": "interest_coverage_ratio"
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
            
            if "이자보상배율" in th_clean:
                key = "interest_coverage_ratio"

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
            st.warning("종목 목록을 불러오지 못했습니다. 코드를 직접 입력해주세요.")
            ticker_input = st.text_input("종목코드(6자리) 입력", max_chars=6, placeholder="예: 005930")
            if ticker_input and len(ticker_input) == 6 and ticker_input.isdigit():
                ticker = ticker_input
    
    with col_reset:
        if st.button("🔄 초기화"):
            reset_search_state()
            st.cache_data.clear()
            if 'search_list' in st.session_state:
                del st.session_state['search_list']
            st.rerun()

    if ticker:
        try:
            df_price = fdr.DataReader(ticker, datetime.now() - timedelta(days=7))
            if df_price.empty:
                st.error(f"데이터를 찾을 수 없습니다. (코드: {ticker})")
                return
            
            curr_price = df_price['Close'].iloc[-1]
            naver_info = get_company_info_from_naver(ticker)
            annual, quarter = get_financials_from_naver(ticker)
            display_name = ticker_to_name.get(ticker, naver_info['name'])

            st.divider()
            st.subheader(f"{display_name} ({ticker})")
            
            col1, col2 = st.columns(2)
            col1.metric("현재가", f"{curr_price:,.0f} 원")
            if naver_info['market_cap'] > 0:
                col2.metric("시가총액", f"{naver_info['market_cap']/100000000:,.0f} 억원")

            with st.expander("기업 개요"):
                st.write(naver_info['overview'])

            st.markdown(f"""
                <a href="https://finance.naver.com/item/fchart.naver?code={ticker}" target="_blank" style="text-decoration:none;">
                    <div style="background-color:#03C75A; color:white; padding:12px; border-radius:8px; text-align:center; font-weight:bold; margin: 10px 0;">
                        📊 네이버 증권 차트 보러가기
                    </div>
                </a>
                """, unsafe_allow_html=True)
            
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
                    ("매출액(억)", 'revenue'), 
                    ("영업이익(억)", 'op_income'), 
                    ("영업이익률(%)", 'op_margin'),
                    ("당기순이익(억)", 'net_income'), 
                    ("순이익률(%)", 'net_income_margin'),
                    ("부채비율(%)", 'debt_ratio'), 
                    ("당좌비율(%)", 'quick_ratio'), 
                    ("유보율(%)", 'reserve_ratio'),
                    ("EPS(원)", 'eps'), 
                    ("BPS(원)", 'bps'), 
                    ("PER(배)", 'per'), 
                    ("PBR(배)", 'pbr'), 
                    ("ROE(%)", 'roe')
                ]
                
                for label, key in items_display:
                    row = [label]
                    is_money = '원' in label or '억' in label
                    
                    for d in annual:
                        val = d.get(key, 0)
                        if val == 0 and key not in ['op_income', 'net_income']:
                            row.append("-")
                        else:
                            row.append(f"{val:,.0f}" if is_money else f"{val:,.2f}")
                    
                    q_val = quarter.get(key, 0)
                    if q_val == 0 and key not in ['op_income', 'net_income']:
                        row.append("-")
                    else:
                        row.append(f"{q_val:,.0f}" if is_money else f"{q_val:,.2f}")
                        
                    disp_data.append(row)
                
                df_table = pd.DataFrame(disp_data, columns=cols)
                
                st.markdown("""
                <style>
                .scroll-table {
                    overflow-x: auto;
                    white-space: nowrap;
                    margin-bottom: 10px;
                }
                .scroll-table table {
                    width: 100%;
                    border-collapse: collapse;
                    font-size: 0.9rem;
                }
                .scroll-table th {
                    text-align: center;
                    padding: 8px;
                    border-bottom: 1px solid #ddd;
                    min-width: 80px;
                    background-color: #f0f2f6;
                    color: #000;
                }
                .scroll-table td {
                    text-align: right;
                    padding: 8px;
                    border-bottom: 1px solid #ddd;
                }
                .scroll-table th:first-child, 
                .scroll-table td:first-child {
                    position: sticky;
                    left: 0;
                    z-index: 10;
                    border-right: 2px solid #ccc;
                    text-align: left;
                    font-weight: bold;
                    background-color: #ffffff;
                    color: #000000;
                }
                @media (prefers-color-scheme: dark) {
                    .scroll-table th {
                        background-color: #262730;
                        color: #fff;
                        border-bottom: 1px solid #444;
                    }
                    .scroll-table td {
                        border-bottom: 1px solid #444;
                        color: #fff;
                    }
                    .scroll-table th:first-child, 
                    .scroll-table td:first-child {
                        background-color: #0e1117;
                        color: #fff;
                        border-right: 2px solid #555;
                    }
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
                    if d.get('roe'):
                        roe_history.append({'연도': d['date'], 'ROE': d['roe']})
                roe_history = roe_history[-3:]
                
                avg_roe = sum([r['ROE'] for r in roe_history]) / len(roe_history) if roe_history else 0
                roe_1yr = annual[-1].get('roe', 0)

                val_3yr = calculate_srim(bps, avg_roe, required_return)
                val_1yr = calculate_srim(bps, roe_1yr, required_return)

                def show_analysis_result(val, roe_used, label_roe, roe_table_data=None):
                    if val > 0:
                        diff_rate = (curr_price - val) / val * 100
                        diff_abs = abs(diff_rate)
                        if val > curr_price:
                            st.success(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 저평가** 상태입니다.")
                        else:
                            st.error(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 고평가** 상태입니다.")
                    else:
                        st.warning("적정주가를 산출할 수 없습니다.")

                    st.markdown("#### 🧮 산출 근거")
                    
                    col_input1, col_input2 = st.columns(2)
                    
                    with col_input1:
                        st.markdown("**1. 핵심 변수**")
                        input_df = pd.DataFrame({
                            "구분": ["BPS (주당순자산)", f"적용 ROE ({label_roe})"],
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
                        st.latex(r" \text{BPS} + \left( \text{BPS} \times \frac{\text{초과이익률}}{\text{요구수익률}} \right) ")
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
