import streamlit as st
from pykrx import stock
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import urllib3
import time

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 데이터 수집 함수들 ---
@st.cache_data(ttl=3600) # 1시간 캐싱
def load_stock_list():
    # 최신 영업일 데이터를 찾기 위해 오늘부터 과거로 10일간 탐색
    date = datetime.now()
    for i in range(10): 
        target_date = date.strftime("%Y%m%d")
        try:
            # 전체 종목의 시가총액 데이터를 한 번에 가져옴 (여기에 종목명이 포함됨) -> 속도 획기적 개선
            df = stock.get_market_cap_by_ticker(target_date, market="ALL")
            if not df.empty:
                ticker_to_name = df['종목명'].to_dict()
                name_to_ticker = {v: k for k, v in ticker_to_name.items()}
                return ticker_to_name, name_to_ticker
        except Exception:
            pass # 에러 발생 시 하루 전으로 이동
        date -= timedelta(days=1)
        
    return {}, {} # 실패 시 빈 딕셔너리 반환

def get_ticker(query, ticker_to_name, name_to_ticker):
    query = str(query).strip().upper()
    # 입력값이 종목코드인 경우 (6자리 숫자)
    if query.isdigit() and len(query) == 6:
        if query in ticker_to_name:
            return query
    # 입력값이 종목명인 경우
    elif query in name_to_ticker:
        return name_to_ticker[query]
    return None

def get_company_overview_from_naver(ticker):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            overview_div = soup.select_one("#summary_info")
            if overview_div:
                return "\n ".join([p.text.strip() for p in overview_div.select("p") if p.text.strip()])
        return "기업 개요 정보 없음"
    except:
        return "정보를 불러올 수 없습니다."

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
            "부채비율": "debt_ratio", "ROE(지배주주)": "roe", "EPS(원)": "eps",
            "PER(배)": "per", "BPS(원)": "bps", "PBR(배)": "pbr"
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
    excess = (roe - rrr) / 100
    return bps + (bps * excess / (rrr / 100))

# --- 메인 UI ---
def main():
    st.set_page_config(page_title="주식 적정주가 분석기", page_icon="📈")
    st.title("📈 주식 적정주가 분석기")

    if 'ticker_to_name' not in st.session_state:
        with st.spinner('종목 데이터 로딩 중... (최대 10초 소요)'):
            st.session_state.ticker_to_name, st.session_state.name_to_ticker = load_stock_list()
    
    ticker_to_name = st.session_state.ticker_to_name
    name_to_ticker = st.session_state.name_to_ticker

    # 데이터 로드 실패 시 재시도 버튼
    if not ticker_to_name:
        st.error("종목 정보를 불러오지 못했습니다.")
        if st.button("다시 시도"):
            st.cache_data.clear()
            st.rerun()
        return

    with st.sidebar:
        st.header("설정")
        required_return = st.number_input("요구수익률 (%)", 1.0, 20.0, 8.0, 0.5)

    stock_input = st.selectbox("종목 검색", [""] + list(name_to_ticker.keys()))

    if stock_input:
        ticker = get_ticker(stock_input, ticker_to_name, name_to_ticker)
        if ticker:
            try:
                today = datetime.now().strftime("%Y%m%d")
                start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
                
                df_price = stock.get_market_ohlcv_by_date(start, today, ticker)
                if df_price.empty:
                    st.error("거래 데이터를 가져올 수 없습니다.")
                    return
                
                curr_price = df_price['종가'].iloc[-1]
                
                # 시가총액 정보가 없는 경우 예외처리
                try:
                    df_cap = stock.get_market_cap_by_date(start, today, ticker)
                    market_cap = df_cap['시가총액'].iloc[-1]
                except:
                    market_cap = 0

                annual, quarter = get_financials_from_naver(ticker)
                overview = get_company_overview_from_naver(ticker)

                st.divider()
                st.subheader(f"{stock_input} ({ticker})")
                col1, col2 = st.columns(2)
                col1.metric("현재가", f"{curr_price:,.0f} 원")
                if market_cap > 0:
                    col2.metric("시가총액", f"{market_cap/100000000:,.0f} 억원")

                with st.expander("기업 개요"):
                    st.write(overview)

                # --- 차트 이미지 표시 ---
                st.subheader("📊 차트 보기")
                t_stamp = int(time.time())
                
                tab_d, tab_w, tab_m = st.tabs(["일봉", "주봉", "월봉"])
                
                with tab_d:
                    st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/day/{ticker}.png?t={t_stamp}", use_container_width=True)
                with tab_w:
                    st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/week/{ticker}.png?t={t_stamp}", use_container_width=True)
                with tab_m:
                    st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/month/{ticker}.png?t={t_stamp}", use_container_width=True)
                
                st.caption("출처: 네이버 금융")
                # ----------------------------------------

                if annual:
                    st.markdown("### 📊 재무 요약")
                    disp_data = []
                    cols = ['항목'] + [d['date'] for d in annual] + ['최근분기']
                    items = [("매출(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("ROE(%)", 'roe'), 
                             ("부채비율(%)", 'debt_ratio'), ("BPS(원)", 'bps'), ("PER(배)", 'per'), ("PBR(배)", 'pbr')]
                    
                    for label, key in items:
                        row = [label]
                        for d in annual:
                            val = d.get(key, 0)
                            row.append(f"{val:,.0f}" if '원' in label or '억' in label else f"{val:,.2f}")
                        q_val = quarter.get(key, 0)
                        row.append(f"{q_val:,.0f}" if '원' in label or '억' in label else f"{q_val:,.2f}")
                        disp_data.append(row)
                    
                    st.table(pd.DataFrame(disp_data, columns=cols))

                    st.divider()
                    st.markdown("### 💰 S-RIM 적정주가")
                    
                    bps = annual[-1].get('bps', 0)
                    roes = [d.get('roe', 0) for d in annual if d.get('roe')]
                    avg_roe = sum(roes)/len(roes) if roes else 0
                    roe_1yr = annual[-1].get('roe', 0)

                    val_3yr = calculate_srim(bps, avg_roe, required_return)
                    val_1yr = calculate_srim(bps, roe_1yr, required_return)

                    tab1, tab2 = st.tabs(["📉 3년 평균 기준", "🆕 1년 실적 기준"])
                    
                    def show_result(val, roe_used):
                        st.metric("적정주가", f"{val:,.0f} 원")
                        st.caption(f"적용 ROE: {roe_used:.2f}%")
                        if val > 0:
                            diff = (curr_price - val) / val * 100
                            if val > curr_price:
                                st.success(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{abs(diff):.1f}% 저평가** 상태입니다.")
                            else:
                                st.error(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{abs(diff):.1f}% 고평가** 상태입니다.")

                    with tab1: show_result(val_3yr, avg_roe)
                    with tab2: show_result(val_1yr, roe_1yr)

            except Exception as e:
                st.error(f"오류: {e}")

if __name__ == "__main__":
    main()
