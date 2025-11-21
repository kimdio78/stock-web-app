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
            ticker_to_name = dict(zip(df['Code'], df['Name']))
            name_to_ticker = dict(zip(df['Name'], df['Code']))
            return ticker_to_name, name_to_ticker
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
        # --- 추가된 재무 지표 (이자보상배율, 유보율 등) ---
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

    if 'ticker_to_name' not in st.session_state:
        with st.spinner('종목 데이터 로딩 중...'):
            st.session_state.ticker_to_name, st.session_state.name_to_ticker = load_stock_list()
    
    ticker_to_name = st.session_state.ticker_to_name
    name_to_ticker = st.session_state.name_to_ticker

    with st.sidebar:
        st.header("설정")
        required_return = st.number_input("요구수익률 (%)", 1.0, 20.0, 8.0, 0.5)
        st.info("보수적: 6~7% | 일반적: 8~9% | 공격적: 10%+")

    ticker = None
    if ticker_to_name:
        stock_input = st.selectbox("종목 검색", [""] + list(name_to_ticker.keys()))
        if stock_input:
            ticker = name_to_ticker.get(stock_input)
    else:
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
                st.markdown("### 📊 재무 요약 (확장)")
                disp_data = []
                cols = ['항목'] + [d['date'] for d in annual] + ['최근분기']
                # --- 추가된 지표 포함 ---
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
                        # 소수점 처리 로직
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

                tab1, tab2 = st.tabs(["📉 3년 실적 평균 기준", "🆕 최근 1년 실적 기준"])
                
                def show_detailed_result(val, roe_used, label_roe):
                    # 결과 메트릭
                    col_a, col_b = st.columns(2)
                    col_a.metric("적정주가", f"{val:,.0f} 원")
                    col_b.metric(f"적용 ROE ({label_roe})", f"{roe_used:.2f} %")
                    
                    # 판정 및 괴리율
                    if val > 0:
                        diff_rate = (curr_price - val) / val * 100
                        diff_abs = abs(diff_rate)
                        if val > curr_price:
                            st.success(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 저평가** 상태입니다.")
                        else:
                            st.error(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 고평가** 상태입니다.")
                    else:
                        st.warning("적정주가를 산출할 수 없습니다 (ROE가 너무 낮거나 데이터 부족).")

                    # 산출 근거 상세 표시
                    st.markdown("---")
                    st.markdown("#### 🧮 산출 근거 (Calculation)")
                    st.code(f"""
# 1. 입력 변수
BPS (최근 결산 기준 주당순자산) : {bps:,.0f} 원
ROE (적용된 자기자본이익률)     : {roe_used:.2f} % ({label_roe})
RRR (요구수익률)                : {required_return} %

# 2. 초과이익률 계산 (Excess Return)
초과이익률 = ROE - 요구수익률
           = {roe_used:.2f}% - {required_return}% = {roe_used - required_return:.2f}%

# 3. 적정주가 계산 (S-RIM 공식)
적정주가 = BPS + (BPS × 초과이익률 / 요구수익률)
         = {bps:,.0f} + ({bps:,.0f} × {roe_used - required_return:.2f}% / {required_return}%)
         = {bps:,.0f} + {bps * (roe_used - required_return) / required_return :,.0f}
         = {val:,.0f} 원
                    """)

                with tab1:
                    st.write("최근 3년간의 평균 ROE를 적용하여 장기적인 기업 가치를 평가합니다.")
                    show_detailed_result(val_3yr, avg_roe, "3년 평균")
                    
                with tab2:
                    st.write("가장 최근 결산 연도의 ROE를 적용하여 최신 실적 추세를 반영합니다.")
                    show_detailed_result(val_1yr, roe_1yr, "최근 1년")

        except Exception as e:
            st.error(f"오류 발생: {e}")

if __name__ == "__main__":
    main()
