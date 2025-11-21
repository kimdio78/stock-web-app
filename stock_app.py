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
@st.cache_data(ttl=3600)
def load_stock_list():
    try:
        tickers = stock.get_market_ticker_list(market="ALL")
        ticker_to_name = {ticker: stock.get_market_ticker_name(ticker) for ticker in tickers}
        name_to_ticker = {v: k for k, v in ticker_to_name.items()}
        return ticker_to_name, name_to_ticker
    except Exception:
        return {}, {}

def get_ticker(query, ticker_to_name, name_to_ticker):
    query = str(query).strip().upper()
    if query.isdigit() and len(query) == 6 and query in ticker_to_name:
        return query
    elif query in name_to_ticker:
        return name_to_ticker[query]
    return None

def get_company_overview_from_naver(ticker):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        overview_div = soup.select_one("#summary_info")
        if overview_div:
            return "\n ".join([p.text.strip() for p in overview_div.select("p") if p.text.strip()])
        return "기업 개요 정보 없음"
    except Exception:
        return "기업 개요 로딩 실패"

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
    excess_profit_rate = (roe - rrr) / 100
    fair_value = bps + (bps * excess_profit_rate / (rrr / 100))
    return fair_value

# --- 메인 앱 UI ---
def main():
    st.set_page_config(page_title="주식 적정주가 분석기", page_icon="📈")
    
    st.title("📈 주식 적정주가 분석기")
    st.caption("네이버 금융 데이터 기반 S-RIM 가치평가")

    if 'ticker_to_name' not in st.session_state:
        with st.spinner('데이터 로딩 중...'):
            st.session_state.ticker_to_name, st.session_state.name_to_ticker = load_stock_list()
    
    ticker_to_name = st.session_state.ticker_to_name
    name_to_ticker = st.session_state.name_to_ticker

    with st.sidebar:
        st.header("설정")
        required_return = st.number_input("요구수익률 (%)", min_value=1.0, max_value=20.0, value=8.0, step=0.5, 
                                        help="보수적 6~7%, 일반적 8~9%, 공격적 10% 이상")
        st.markdown("---")
        st.info("이 앱은 네이버 금융의 데이터를 실시간으로 활용합니다.")

    stock_input = st.selectbox(
        "종목 검색 (이름 또는 코드)",
        options=[""] + list(name_to_ticker.keys())
    )

    if stock_input:
        ticker = get_ticker(stock_input, ticker_to_name, name_to_ticker)
        
        if ticker:
            try:
                today = datetime.now().strftime("%Y%m%d")
                start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
                
                df_price = stock.get_market_ohlcv_by_date(start_date, today, ticker)
                if df_price.empty:
                    st.error("거래 데이터를 가져올 수 없습니다.")
                    return
                
                current_price = df_price['종가'].iloc[-1]
                last_day = df_price.index[-1].strftime('%Y-%m-%d')
                
                df_cap = stock.get_market_cap_by_date(start_date, today, ticker)
                market_cap = df_cap['시가총액'].iloc[-1]

                annual_data, quarter_data = get_financials_from_naver(ticker)
                overview = get_company_overview_from_naver(ticker)

                st.divider()
                st.subheader(f"{stock_input} ({ticker})")
                st.caption(f"기준일: {last_day}")

                col1, col2 = st.columns(2)
                col1.metric("현재주가", f"{current_price:,.0f} 원")
                col2.metric("시가총액", f"{market_cap/100000000:,.0f} 억원")

                with st.expander("기업 개요 보기"):
                    st.write(overview)

                # --- 차트 이미지 표시 (수정된 부분) ---
                st.subheader("📊 차트 보기")
                # 실시간 갱신을 위한 타임스탬프
                t_stamp = int(time.time())
                
                tab_d, tab_w, tab_m = st.tabs(["일봉 (Daily)", "주봉 (Weekly)", "월봉 (Monthly)"])
                
                with tab_d:
                    st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/day/{ticker}.png?t={t_stamp}", use_container_width=True)
                with tab_w:
                    st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/week/{ticker}.png?t={t_stamp}", use_container_width=True)
                with tab_m:
                    st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/month/{ticker}.png?t={t_stamp}", use_container_width=True)
                
                st.caption(f"출처: 네이버 금융 (업데이트: {datetime.now().strftime('%H:%M:%S')})")
                # ----------------------------------------

                if annual_data:
                    st.markdown("### 📊 재무 하이라이트")
                    display_data = []
                    cols = ['구분'] + [d['date'] for d in annual_data] + ['최근분기']
                    items = [
                        ("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("순이익(억)", 'net_income'),
                        ("ROE(%)", 'roe'), ("부채비율(%)", 'debt_ratio'), 
                        ("BPS(원)", 'bps'), ("PER(배)", 'per'), ("PBR(배)", 'pbr')
                    ]

                    for label, key in items:
                        row = [label]
                        for d in annual_data:
                            val = d.get(key, 0)
                            row.append(f"{val:,.0f}" if '원' in label or '억' in label else f"{val:,.2f}")
                        q_val = quarter_data.get(key, 0)
                        row.append(f"{q_val:,.0f}" if '원' in label or '억' in label else f"{q_val:,.2f}")
                        display_data.append(row)
                    
                    df_table = pd.DataFrame(display_data, columns=cols)
                    st.table(df_table)

                    st.divider()
                    st.markdown("### 💰 적정주가 분석 (S-RIM)")
                    
                    latest_bps = annual_data[-1].get('bps', 0)
                    
                    roes_3yr = [d.get('roe', 0) for d in annual_data if d.get('roe')]
                    avg_roe_3yr = sum(roes_3yr)/len(roes_3yr) if roes_3yr else 0
                    val_3yr = calculate_srim(latest_bps, avg_roe_3yr, required_return)
                    
                    roe_1yr = annual_data[-1].get('roe', 0)
                    val_1yr = calculate_srim(latest_bps, roe_1yr, required_return)

                    tab1, tab2, tab3 = st.tabs(["📉 최근 3년 평균 기준", "🆕 최근 1년 실적 기준", "ℹ️ 산출 근거"])

                    with tab1:
                        st.markdown("#### 장기적 관점의 적정주가")
                        st.write("최근 3년간의 평균 ROE를 적용하여 일시적 실적 변동을 보정한 가치입니다.")
                        col_a, col_b = st.columns(2)
                        col_a.metric("적정주가", f"{val_3yr:,.0f} 원")
                        col_b.metric("적용 ROE (3년 평균)", f"{avg_roe_3yr:.2f} %")
                        
                        if val_3yr > 0:
                            diff_rate = (current_price - val_3yr) / val_3yr * 100
                            if val_3yr > current_price:
                                st.success(f"현재가({current_price:,.0f}원)는 적정주가({val_3yr:,.0f}원) 대비 **{abs(diff_rate):.1f}% 저평가** 상태입니다.")
                            else:
                                st.error(f"현재가({current_price:,.0f}원)는 적정주가({val_3yr:,.0f}원) 대비 **{abs(diff_rate):.1f}% 고평가** 상태입니다.")
                        else:
                            st.warning("적정주가를 산출할 수 없습니다.")

                    with tab2:
                        st.markdown("#### 현재 추세 반영 적정주가")
                        st.write("가장 최근 결산 연도의 ROE를 적용하여 최신 성장성을 반영한 가치입니다.")
                        col_a, col_b = st.columns(2)
                        col_a.metric("적정주가", f"{val_1yr:,.0f} 원")
                        col_b.metric("적용 ROE (최근 1년)", f"{roe_1yr:.2f} %")

                        if val_1yr > 0:
                            diff_rate = (current_price - val_1yr) / val_1yr * 100
                            if val_1yr > current_price:
                                st.success(f"현재가({current_price:,.0f}원)는 적정주가({val_1yr:,.0f}원) 대비 **{abs(diff_rate):.1f}% 저평가** 상태입니다.")
                            else:
                                st.error(f"현재가({current_price:,.0f}원)는 적정주가({val_1yr:,.0f}원) 대비 **{abs(diff_rate):.1f}% 고평가** 상태입니다.")
                        else:
                            st.warning("적정주가를 산출할 수 없습니다.")

                    with tab3:
                        st.markdown("#### 🧮 적정주가 산출 상세 내역")
                        st.markdown(f"""
                        **1. 기본 공식 (S-RIM)**
                        > `적정주가 = BPS + (BPS × (ROE - 요구수익률) / 요구수익률)`
                        
                        **2. 사용된 데이터**
                        * **BPS**: {latest_bps:,.0f} 원
                        * **요구수익률**: {required_return}%
                        * **적용 ROE**: {avg_roe_3yr:.2f}% (3년) / {roe_1yr:.2f}% (1년)
                        """)

                else:
                    st.warning("재무 데이터를 불러올 수 없어 분석할 수 없습니다.")

            except Exception as e:
                st.error(f"분석 중 오류가 발생했습니다: {e}")

if __name__ == "__main__":
    main()