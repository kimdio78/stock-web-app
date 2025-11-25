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
        df = fdr.StockListing('KRX')
        if not df.empty:
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
        # 항목 매핑 수정 (이자보상배율 정확도 향상)
        items = {
            "매출액": "revenue", "영업이익": "op_income", "당기순이익": "net_income",
            "부채비율": "debt_ratio", 
            "ROE(지배주주)": "roe", "EPS(원)": "eps", "PER(배)": "per", 
            "BPS(원)": "bps", "PBR(배)": "pbr", 
            "이자보상배율": "interest_coverage_ratio" 
        }

        for row in rows:
            th_text = row.th.text.strip()
            # 이자보상배율 등 일부 항목 이름이 조금씩 다를 수 있어 포함 여부로 체크
            key = None
            if th_text in items:
                key = items[th_text]
            
            if key:
                cells = row.select("td")
                for i, idx in enumerate(annual_indices):
                    t_idx = idx - cell_offset
                    if 0 <= t_idx < len(cells):
                        val = cells[t_idx].text.strip().replace(",", "")
                        # N/A, - 처리
                        if val in ['N/A', '-', '', '.']:
                            annual_data[i][key] = 0.0
                        else:
                            try:
                                annual_data[i][key] = float(val)
                            except:
                                annual_data[i][key] = 0.0
                
                if quarter_idx != -1:
                    t_idx = quarter_idx - cell_offset
                    if 0 <= t_idx < len(cells):
                        val = cells[t_idx].text.strip().replace(",", "")
                        if val in ['N/A', '-', '', '.']:
                            quarter_data[key] = 0.0
                        else:
                            try:
                                quarter_data[key] = float(val)
                            except:
                                quarter_data[key] = 0.0
        
        annual_data.reverse()
        return annual_data, quarter_data
    except Exception:
        return [], {}

def calculate_srim(bps, roe, rrr):
    if rrr <= 0: return 0
    excess_profit_rate = (roe - rrr) / 100
    fair_value = bps + (bps * excess_profit_rate / (rrr / 100))
    return fair_value

# --- 콜백 함수 (검색 충돌 방지용) ---
def clear_text_input():
    st.session_state['ticker_input'] = ""

def clear_selectbox():
    st.session_state['stock_input'] = ""

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
        # 2. 요구수익률 하단 설명 삭제
        required_return = st.number_input("요구수익률 (%)", 1.0, 20.0, 8.0, 0.5)

    # --- 1. & 4. 검색 방식 개선 및 충돌 해결 ---
    st.markdown("##### 종목 검색")
    
    # 탭 대신 두 입력 방식을 나란히 배치하지 않고, 기능적으로 분리
    # selectbox 선택 시 text_input 초기화, text_input 입력 시 selectbox 초기화
    
    col_search1, col_search2 = st.columns(2)
    
    ticker = None
    
    with col_search1:
        if search_map:
            stock_input = st.selectbox(
                "목록에서 선택 (이름/코드)", 
                [""] + list(search_map.keys()),
                index=0,
                key='stock_input',
                on_change=clear_text_input # 변경 시 텍스트 입력 초기화
            )
            if stock_input:
                ticker = search_map.get(stock_input)
        else:
            st.warning("목록 로딩 중...")

    with col_search2:
        ticker_input = st.text_input(
            "코드 직접 입력 (6자리)", 
            max_chars=6,
            key='ticker_input',
            on_change=clear_selectbox # 변경 시 선택 상자 초기화
        )
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

            # 7. 차트 링크 수정 (네이버 증권 차트 탭으로 바로 연결)
            st.markdown(f"""
                <a href="https://m.stock.naver.com/item/main.nhn?code={ticker}#/chart" target="_blank" style="text-decoration:none;">
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
                # 6. 재무요약 항목 수정 (당좌비율, 유보율 삭제)
                items = [
                    ("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("순이익(억)", 'net_income'),
                    ("ROE(%)", 'roe'), ("부채비율(%)", 'debt_ratio'),
                    ("이자보상배율(배)", 'interest_coverage_ratio'), # 5. 이자보상배율 표시 문제 해결 (크롤링 로직 개선됨)
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
                # 1. 최근 3년치 ROE 데이터 준비
                roe_data_3yr = [(d['date'], d.get('roe', 0)) for d in annual if d.get('roe')]
                # 최근 3개만 사용 (이미 역순 정렬되어 있으므로 앞 3개는 최근 3년이 아닐 수 있음 -> annual_data는 get_financials에서 reverse()되어 최근이 마지막임.
                # annual_data는 과거->최신 순. 따라서 뒤에서 3개 가져옴.
                roe_data_3yr = roe_data_3yr[-3:]
                
                roes = [r[1] for r in roe_data_3yr]
                avg_roe = sum(roes)/len(roes) if roes else 0
                roe_1yr = annual[-1].get('roe', 0)

                val_3yr = calculate_srim(bps, avg_roe, required_return)
                val_1yr = calculate_srim(bps, roe_1yr, required_return)

                # 3. 폰트 통일을 위한 CSS 스타일
                st.markdown("""
                <style>
                .calc-box {
                    background-color: #f0f2f6;
                    border-radius: 10px;
                    padding: 20px;
                    font-family: "Source Sans Pro", sans-serif;
                    margin-bottom: 20px;
                }
                .calc-line {
                    margin-bottom: 10px;
                    line-height: 1.6;
                }
                .highlight {
                    color: #0068c9;
                    font-weight: bold;
                }
                </style>
                """, unsafe_allow_html=True)

                def show_analysis_result(val, roe_used, label_roe, roe_details=None):
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

                    st.markdown("#### 🧮 산출 근거")
                    
                    # 입력 변수 테이블
                    st.markdown("**1. 입력 변수**")
                    
                    roe_desc = f"{roe_used:.2f} %"
                    if roe_details:
                        # 1. 최근 3년 ROE 내역 표시
                        roe_desc += f" (평균: {', '.join([f'{y}: {r:.2f}%' for y, r in roe_details])})"

                    input_data = {
                        "항목": ["BPS (주당순자산)", f"ROE ({label_roe})", "요구수익률"],
                        "값": [f"{bps:,.0f} 원", roe_desc, f"{required_return} %"],
                        "비고": ["최근 결산 자본총계 ÷ 주식수", "적용된 자기자본이익률", "투자자 기대 최소 수익률"]
                    }
                    st.table(pd.DataFrame(input_data))

                    # 3. 계산 과정 (폰트 통일 및 가독성 개선)
                    st.markdown("**2. 계산 과정**")
                    excess_rate = roe_used - required_return
                    
                    # HTML/CSS로 깔끔하게 수식 표현
                    st.markdown(f"""
                    <div class="calc-box">
                        <div class="calc-line">
                            <strong>① 초과이익률</strong> = ROE - 요구수익률<br>
                            &nbsp;&nbsp;&nbsp;&nbsp;= {roe_used:.2f}% - {required_return}% = <span class="highlight">{excess_rate:.2f}%</span>
                        </div>
                        <div class="calc-line">
                            <strong>② 적정주가 (S-RIM)</strong> = BPS + ( BPS × 초과이익률 ÷ 요구수익률 )<br>
                            &nbsp;&nbsp;&nbsp;&nbsp;= {bps:,.0f} + ( {bps:,.0f} × {excess_rate:.2f}% ÷ {required_return}% )<br>
                            &nbsp;&nbsp;&nbsp;&nbsp;= <strong style="font-size: 1.2em;">{val:,.0f} 원</strong>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                tab1, tab2 = st.tabs(["📉 3년 실적 평균 기준", "🆕 최근 1년 실적 기준"])
                
                with tab1:
                    st.caption("최근 3년간의 평균 ROE를 적용하여 장기적인 기업 가치를 평가합니다.")
                    # 3년치 데이터 전달
                    show_analysis_result(val_3yr, avg_roe, "3년 평균", roe_details=roe_data_3yr)
                    
                with tab2:
                    st.caption("가장 최근 결산 연도의 ROE를 적용하여 최신 실적 추세를 반영합니다.")
                    show_analysis_result(val_1yr, roe_1yr, "최근 1년")

        except Exception as e:
            st.error(f"오류 발생: {e}")

if __name__ == "__main__":
    main()
