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
            # 검색 키 생성: "삼성전자 (005930)"
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
        items = {
            "매출액": "revenue", "영업이익": "op_income", "당기순이익": "net_income",
            "부채비율": "debt_ratio", "당좌비율": "quick_ratio", "유보율": "reserve_ratio",
            "ROE(지배주주)": "roe", "EPS(원)": "eps", "PER(배)": "per", 
            "BPS(원)": "bps", "PBR(배)": "pbr", "이자보상배율": "interest_coverage_ratio"
        }

        for row in rows:
            th = row.th.text.strip()
            if th in items:
                key = items[th]
                cells = row.select("td")
                for i, idx in enumerate(annual_indices):
                    t_idx = idx - cell_offset
                    if 0 <= t_idx < len(cells):
                        val = cells[t_idx].text.strip().replace(",", "")
                        annual_data[i][key] = float(val) if val and val not in ['N/A','-'] else 0.0
                
                if quarter_idx != -1:
                    t_idx = quarter_idx - cell_offset
                    if 0 <= t_idx < len(cells):
                        val = cells[t_idx].text.strip().replace(",", "")
                        quarter_data[key] = float(val) if val and val not in ['N/A','-'] else 0.0
        
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
        st.info("보수적: 6~7% | 일반적: 8~9% | 공격적: 10%+")

    # --- 1. 검색 방식 개선 (탭으로 분리) ---
    search_tab1, search_tab2 = st.tabs(["🔍 종목명/코드로 검색", "🔢 코드 직접 입력"])
    
    ticker = None
    
    with search_tab1:
        if search_map:
            # selectbox는 텍스트 검색(filtering)을 지원합니다.
            stock_input = st.selectbox(
                "종목을 선택하거나 검색하세요 (예: 삼성전자, 005930)", 
                [""] + list(search_map.keys()),
                index=0
            )
            if stock_input:
                ticker = search_map.get(stock_input)
        else:
            st.warning("종목 목록을 불러오는 중입니다. 잠시 후 다시 시도하거나 '코드 직접 입력'을 이용하세요.")

    with search_tab2:
        ticker_input = st.text_input("종목코드 6자리 입력 (예: 005930)", max_chars=6)
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

            st.markdown(f"""
                <a href="https://m.stock.naver.com/item/main.nhn?code={ticker}#/chart" target="_blank" style="text-decoration:none;">
                    <div style="background-color:#03C75A; color:white; padding:12px; border-radius:8px; text-align:center; font-weight:bold; margin: 10px 0;">
                        📊 네이버 증권 차트 보기
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
                items = [
                    ("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("순이익(억)", 'net_income'),
                    ("ROE(%)", 'roe'), ("부채비율(%)", 'debt_ratio'), ("당좌비율(%)", 'quick_ratio'), ("유보율(%)", 'reserve_ratio'),
                    ("이자보상배율(배)", 'interest_coverage_ratio'),
                    ("EPS(원)", 'eps'), ("BPS(원)", 'bps'), ("PER(배)", 'per'), ("PBR(배)", 'pbr')
                ]
                
                for label, key in items:
                    row = [label]
                    for d in annual:
                        val = d.get(key, 0)
                        if '원' in label or '억' in label:
                            row.append(f"{val:,.0f}")
                        else:
                            row.append(f"{val:,.2f}")
                    q_val = quarter.get(key, 0)
                    if '원' in label or '억' in label:
                        row.append(f"{q_val:,.0f}")
                    else:
                        row.append(f"{q_val:,.2f}")
                    disp_data.append(row)
                
                st.table(pd.DataFrame(disp_data, columns=cols))

                st.divider()
                st.markdown("### 💰 S-RIM 적정주가 분석")
                
                bps = annual[-1].get('bps', 0)
                roes = [d.get('roe', 0) for d in annual if d.get('roe')]
                avg_roe = sum(roes)/len(roes) if roes else 0
                roe_1yr = annual[-1].get('roe', 0)

                val_3yr = calculate_srim(bps, avg_roe, required_return)
                val_1yr = calculate_srim(bps, roe_1yr, required_return)

                # --- 결과 및 산출 근거 표시 함수 (디자인 개선) ---
                def show_analysis_result(val, roe_used, label_roe):
                    # 1. 결과 판정
                    if val > 0:
                        diff_rate = (curr_price - val) / val * 100
                        diff_abs = abs(diff_rate)
                        if val > curr_price:
                            st.success(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 저평가** 상태입니다.")
                        else:
                            st.error(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 고평가** 상태입니다.")
                    else:
                        st.warning("적정주가를 산출할 수 없습니다 (ROE가 너무 낮거나 데이터 부족).")

                    # 2. 산출 근거 시각화 (테이블 + 수식)
                    st.markdown("#### 🧮 산출 근거")
                    
                    # 입력 변수 테이블
                    st.markdown("**1. 입력 변수**")
                    input_data = {
                        "항목": ["BPS (주당순자산)", f"ROE ({label_roe})", "요구수익률"],
                        "값": [f"{bps:,.0f} 원", f"{roe_used:.2f} %", f"{required_return} %"],
                        "비고": ["최근 결산 자본총계 ÷ 주식수", "적용된 자기자본이익률", "투자자 기대 최소 수익률"]
                    }
                    st.table(pd.DataFrame(input_data))

                    # 계산 과정 수식
                    st.markdown("**2. 계산 과정**")
                    excess_rate = roe_used - required_return
                    
                    st.latex(r'''
                    \text{초과이익률} = \text{ROE} - \text{요구수익률}
                    ''')
                    st.info(f"{roe_used:.2f}% - {required_return}% = **{excess_rate:.2f}%**")

                    st.latex(r'''
                    \text{적정주가} = \text{BPS} + \left( \text{BPS} \times \frac{\text{초과이익률}}{\text{요구수익률}} \right)
                    ''')
                    
                    # 최종 계산식 보여주기
                    calc_detail = f"{bps:,.0f} + ({bps:,.0f} \\times \\frac{{{excess_rate:.2f}\\%}}{{{required_return}\\%}})"
                    st.latex(f"\\approx {calc_detail}")
                    st.success(f"**= {val:,.0f} 원**")

                # 탭 구성
                tab1, tab2 = st.tabs(["📉 3년 실적 평균 기준", "🆕 최근 1년 실적 기준"])
                
                with tab1:
                    st.caption("최근 3년간의 평균 ROE를 적용하여 장기적인 기업 가치를 평가합니다.")
                    show_analysis_result(val_3yr, avg_roe, "3년 평균")
                    
                with tab2:
                    st.caption("가장 최근 결산 연도의 ROE를 적용하여 최신 실적 추세를 반영합니다.")
                    show_analysis_result(val_1yr, roe_1yr, "최근 1년")

        except Exception as e:
            st.error(f"오류 발생: {e}")

if __name__ == "__main__":
    main()
