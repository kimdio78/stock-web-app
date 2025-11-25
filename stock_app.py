import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import urllib3
import FinanceDataReader as fdr
import time
import re

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

def clean_float(text):
    """문자열에서 숫자만 추출하여 float로 변환 (이자보상배율 오류 수정용)"""
    if not text or text.strip() in ['-', 'N/A', '', '.']:
        return 0.0
    try:
        # 쉼표 제거
        text = text.replace(',', '')
        # 숫자, 소수점, 마이너스 부호만 남김
        import re
        # 정규식: 음수 부호 가능, 숫자, 소수점 포함
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
        
        # 사용자가 요청한 모든 항목 매핑 (네이버 페이지에 존재하는 것만 매칭됨)
        # 공백을 제거하고 비교하여 매칭 정확도 향상
        items_map = {
            "매출액": "revenue",
            "매출원가": "cost_of_sales",
            "매출총이익": "gross_profit",
            "판매비와관리비": "sga", # 띄어쓰기 제거 버전
            "영업이익": "op_income",
            "영업이익률": "op_margin", # 네이버 표기는 '영업이익률' 
            "당기순이익": "net_income",
            "당기순이익(지배)": "net_income_controlling",
            "순이익률": "net_income_margin", # 네이버 표기 기준
            "자산총계": "assets", # 네이버 표기는 자산총계
            "부채총계": "liabilities",
            "자본총계": "equity",
            "자본총계(지배)": "equity_controlling",
            "유동비율": "current_ratio",
            "이자보상배율": "interest_coverage_ratio",
            "부채비율": "debt_ratio",
            "자기자본비율": "equity_ratio",
            "EPS": "eps",
            "SPS": "sps",
            "BPS": "bps",
            "주당배당금": "dps",
            "배당성향": "payout_ratio",
            "PER": "per",
            "PSR": "psr",
            "PBR": "pbr",
            "EV/EBITDA": "ev_ebitda",
            "ROE": "roe"
        }

        for row in rows:
            th_text = row.th.text.strip()
            # 텍스트 전처리: 줄바꿈 제거, 공백 제거 (매칭 확률 높임)
            th_clean = th_text.replace("\n", "").replace(" ", "")
            
            key = None
            # 부분 일치 등으로 키 찾기
            for k_text, k_code in items_map.items():
                # 정확히 포함되는지 확인 (예: 'ROE' in 'ROE(지배주주)')
                # 단, '영업이익'과 '영업이익률' 구분 필요
                if k_text in th_clean:
                    # 영업이익 vs 영업이익률 구분
                    if k_text == "영업이익" and "률" in th_clean: continue
                    if k_text == "당기순이익" and "률" in th_clean: continue
                    
                    key = k_code
                    break
            
            # 이자보상배율 별도 체크 (확실하게)
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
    st.title("📈 주식 적정주가 분석기")

    if 'search_list' not in st.session_state:
        with st.spinner('종목 데이터 로딩 중...'):
            st.session_state.search_list, st.session_state.search_map, st.session_state.ticker_to_name = load_stock_list()
    
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
                
                # --- 요청하신 순서대로 항목 배치 ---
                # 참고: 네이버 메인 요약표에 없는 데이터는 0 또는 N/A로 나올 수 있습니다.
                items_display = [
                    ("매출액(억)", 'revenue'), 
                    ("매출원가(억)", 'cost_of_sales'), 
                    ("매출총이익(억)", 'gross_profit'),
                    ("판매비와관리비(억)", 'sga'),
                    ("영업이익(억)", 'op_income'), 
                    ("영업이익률(%)", 'op_margin'),
                    ("당기순이익(억)", 'net_income'), 
                    ("당기순이익(지배)(억)", 'net_income_controlling'),
                    ("당기순이익률(지배)(%)", 'net_income_margin'),
                    ("자산총계(억)", 'assets'), 
                    ("부채총계(억)", 'liabilities'), 
                    ("자본총계(억)", 'equity'),
                    ("자본총계(지배)(억)", 'equity_controlling'),
                    ("유동비율(%)", 'current_ratio'),
                    ("이자보상배율(배)", 'interest_coverage_ratio'),
                    ("부채비율(%)", 'debt_ratio'), 
                    ("자기자본비율(%)", 'equity_ratio'),
                    ("EPS(원)", 'eps'), 
                    ("SPS(원)", 'sps'),
                    ("BPS(원)", 'bps'), 
                    ("주당배당금(원)", 'dps'),
                    ("배당성향(%)", 'payout_ratio'),
                    ("PER(배)", 'per'), 
                    ("PSR(배)", 'psr'),
                    ("PBR(배)", 'pbr'), 
                    ("EV/EBITDA(배)", 'ev_ebitda'),
                    ("ROE(%)", 'roe')
                ]
                
                for label, key in items_display:
                    row = [label]
                    # 데이터 포맷팅 (금액은 정수, 비율은 소수점)
                    is_money = '원' in label or '억' in label
                    
                    for d in annual:
                        val = d.get(key, 0)
                        # 데이터가 0이면 '-' 표시 (가독성 위해)
                        if val == 0 and key not in ['op_income', 'net_income']: # 이익은 0일수도 있으므로 제외
                            row.append("-")
                        else:
                            row.append(f"{val:,.0f}" if is_money else f"{val:,.2f}")
                    
                    q_val = quarter.get(key, 0)
                    if q_val == 0 and key not in ['op_income', 'net_income']:
                        row.append("-")
                    else:
                        row.append(f"{q_val:,.0f}" if is_money else f"{q_val:,.2f}")
                        
                    disp_data.append(row)
                
                st.table(pd.DataFrame(disp_data, columns=cols))

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
