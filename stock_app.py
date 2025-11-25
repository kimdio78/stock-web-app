import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import urllib3
import FinanceDataReader as fdr
import time

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 데이터 수집 함수들 ---
@st.cache_data(ttl=3600)
def load_stock_list():
    try:
        # KRX 전체 종목 리스트 가져오기
        df = fdr.StockListing('KRX')
        if not df.empty:
            # 검색 편의성을 위해 '종목명 (종목코드)' 형태의 키 생성
            # 이 리스트는 selectbox에서 검색 대상이 됩니다.
            df['Search_Key'] = df['Name'] + " (" + df['Code'] + ")"
            search_map = dict(zip(df['Search_Key'], df['Code']))
            ticker_to_name = dict(zip(df['Code'], df['Name']))
            return search_map, ticker_to_name
    except:
        pass
    return {}, {}

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
                    raw_mc = mc_element.text.strip().replace(',', '').replace('조', '').replace(' ', '')
                    parts = raw_mc.split('조')
                    trillion = int(parts[0]) if parts[0] else 0
                    billion = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                    info['market_cap'] = (trillion * 10000 + billion) * 100000000
            except:
                pass
        return info
    except:
        return {'name': ticker, 'overview': "로딩 실패", 'market_cap': 0}

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
        
        # 재무 항목 매핑 (이자보상배율 등 텍스트 매칭 유연성 확보)
        for row in rows:
            th_text = row.th.text.strip()
            key = None
            
            if "매출액" in th_text: key = "revenue"
            elif "영업이익" in th_text and "률" not in th_text: key = "op_income" # 영업이익률 제외
            elif "당기순이익" in th_text and "률" not in th_text: key = "net_income"
            elif "부채비율" in th_text: key = "debt_ratio"
            elif "ROE" in th_text: key = "roe"
            elif "EPS" in th_text: key = "eps"
            elif "PER" in th_text: key = "per"
            elif "BPS" in th_text: key = "bps"
            elif "PBR" in th_text: key = "pbr"
            elif "이자보상배율" in th_text: key = "interest_coverage_ratio"
            
            if key:
                cells = row.select("td")
                for i, idx in enumerate(annual_indices):
                    t_idx = idx - cell_offset
                    if 0 <= t_idx < len(cells):
                        val = cells[t_idx].text.strip().replace(",", "")
                        if val in ['N/A', '-', '', '.', '완전잠식']:
                            annual_data[i][key] = 0.0
                        else:
                            try: annual_data[i][key] = float(val)
                            except: annual_data[i][key] = 0.0
                
                if quarter_idx != -1:
                    t_idx = quarter_idx - cell_offset
                    if 0 <= t_idx < len(cells):
                        val = cells[t_idx].text.strip().replace(",", "")
                        if val in ['N/A', '-', '', '.', '완전잠식']:
                            quarter_data[key] = 0.0
                        else:
                            try: quarter_data[key] = float(val)
                            except: quarter_data[key] = 0.0
        
        annual_data.reverse()
        return annual_data, quarter_data
    except Exception:
        return [], {}

def calculate_srim(bps, roe, rrr):
    if rrr <= 0: return 0
    excess_profit_rate = (roe - rrr) / 100
    fair_value = bps + (bps * excess_profit_rate / (rrr / 100))
    return fair_value

# --- 메인 UI ---
def main():
    st.set_page_config(page_title="주식 적정주가 분석기", page_icon="📈")
    st.title("📈 주식 적정주가 분석기")

    if 'search_map' not in st.session_state:
        with st.spinner('종목 데이터 로딩 중...'):
            st.session_state.search_map, st.session_state.ticker_to_name = load_stock_list()
    
    search_map = st.session_state.search_map
    ticker_to_name = st.session_state.ticker_to_name

    with st.sidebar:
        st.header("설정")
        required_return = st.number_input("요구수익률 (%)", 1.0, 20.0, 8.0, 0.5)

    # --- 1. & 2. 검색 기능 통합 (이름/코드 모두 검색 가능, 단일 창) ---
    ticker = None
    if search_map:
        # selectbox에서 타이핑으로 검색 가능 (종목명 또는 코드 포함된 문자열 검색)
        stock_input = st.selectbox(
            "종목 검색 (이름 또는 코드를 입력하세요)", 
            [""] + list(search_map.keys()),
            index=0,
            placeholder="종목명 또는 코드를 입력하세요..."
        )
        if stock_input:
            ticker = search_map.get(stock_input)
    else:
        # 목록 로딩 실패 시 비상용 입력창
        ticker_input = st.text_input("종목코드 6자리 직접 입력")
        if ticker_input and len(ticker_input) == 6 and ticker_input.isdigit():
            ticker = ticker_input

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

            # 3. 차트 링크 수정 (해당 종목 차트 탭으로 직접 연결)
            st.markdown(f"""
                <a href="https://m.stock.naver.com/item/main.naver?code={ticker}#/chart" target="_blank" style="text-decoration:none;">
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
                
                # 4. & 6. 재무요약 항목 수정 (당좌비율, 유보율 삭제 / 이자보상배율 포함)
                items = [
                    ("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("순이익(억)", 'net_income'),
                    ("ROE(%)", 'roe'), ("부채비율(%)", 'debt_ratio'),
                    ("이자보상배율(배)", 'interest_coverage_ratio'),
                    ("EPS(원)", 'eps'), ("BPS(원)", 'bps'), ("PER(배)", 'per'), ("PBR(배)", 'pbr')
                ]
                
                for label, key in items:
                    row = [label]
                    for d in annual:
                        val = d.get(key, 0)
                        if '원' in label or '억' in label: row.append(f"{val:,.0f}")
                        else: row.append(f"{val:,.2f}")
                    q_val = quarter.get(key, 0)
                    if '원' in label or '억' in label: row.append(f"{q_val:,.0f}")
                    else: row.append(f"{q_val:,.2f}")
                    disp_data.append(row)
                
                st.table(pd.DataFrame(disp_data, columns=cols))

                st.divider()
                st.markdown("### 💰 S-RIM 적정주가 분석")
                
                bps = annual[-1].get('bps', 0)
                
                # 3년 ROE 데이터 추출 (최신순 정렬되어 있으므로 뒤에서부터 3개)
                roe_history = []
                for d in annual:
                    if d.get('roe'):
                        roe_history.append({'연도': d['date'], 'ROE': d['roe']})
                roe_history = roe_history[-3:] # 최근 3년치만 유지
                
                avg_roe = sum([r['ROE'] for r in roe_history]) / len(roe_history) if roe_history else 0
                roe_1yr = annual[-1].get('roe', 0)

                val_3yr = calculate_srim(bps, avg_roe, required_return)
                val_1yr = calculate_srim(bps, roe_1yr, required_return)

                # 폰트 스타일 (일반 폰트 사용)
                st.markdown("""
                <style>
                .calc-box {
                    background-color: #f8f9fa;
                    border-radius: 8px;
                    padding: 15px;
                    margin-top: 10px;
                    font-family: sans-serif;
                }
                .result-text {
                    font-size: 1.1em;
                    line-height: 1.6;
                }
                </style>
                """, unsafe_allow_html=True)

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
                    
                    # 5. 입력 변수 표 (요구수익률 삭제, ROE 데이터 별도 표)
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

                    # 계산 과정 (수식 폰트 통일)
                    st.markdown("**3. 계산 과정**")
                    excess_rate = roe_used - required_return
                    
                    st.markdown(f"""
                    <div class="calc-box">
                        <div class="result-text">
                            <strong>① 초과이익률</strong> = ROE ({roe_used:.2f}%) - 요구수익률 ({required_return}%) = <strong>{excess_rate:.2f}%</strong><br><br>
                            <strong>② 적정주가</strong> = BPS + ( BPS × 초과이익률 ÷ 요구수익률 )<br>
                            &nbsp;&nbsp;&nbsp;&nbsp;= {bps:,.0f} + ( {bps:,.0f} × {excess_rate:.2f}% ÷ {required_return}% )<br>
                            &nbsp;&nbsp;&nbsp;&nbsp;= <strong>{val:,.0f} 원</strong>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

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
