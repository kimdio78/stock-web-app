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
        # FinanceDataReader로 전체 종목 리스트 가져오기
        df = fdr.StockListing('KRX')
        if not df.empty:
            ticker_to_name = dict(zip(df['Code'], df['Name']))
            name_to_ticker = dict(zip(df['Name'], df['Code']))
            return ticker_to_name, name_to_ticker
    except:
        pass
    return {}, {}

def get_company_info_from_naver(ticker):
    """
    네이버 금융에서 기업 개요, 시가총액, 그리고 **종목명**을 가져옵니다.
    """
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        
        # 기본값 설정
        info = {'name': ticker, 'overview': "정보 없음", 'market_cap': 0}
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 1. 종목명 추출 (h_company 클래스 내부)
            name_tag = soup.select_one(".wrap_company h2 a")
            if name_tag:
                info['name'] = name_tag.text.strip()

            # 2. 기업 개요 추출
            overview_div = soup.select_one("#summary_info")
            if overview_div:
                info['overview'] = "\n ".join([p.text.strip() for p in overview_div.select("p") if p.text.strip()])
            
            # 3. 시가총액 추출
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
        with st.spinner('종목 데이터 로딩 중...'):
            st.session_state.ticker_to_name, st.session_state.name_to_ticker = load_stock_list()
    
    ticker_to_name = st.session_state.ticker_to_name
    name_to_ticker = st.session_state.name_to_ticker

    # 사이드바
    with st.sidebar:
        st.header("설정")
        required_return = st.number_input("요구수익률 (%)", 1.0, 20.0, 8.0, 0.5)

    # --- 입력 방식: 리스트가 비어있으면 직접 입력 창 활성화 ---
    ticker = None
    if ticker_to_name:
        stock_input = st.selectbox("종목 검색", [""] + list(name_to_ticker.keys()))
        if stock_input:
            ticker = name_to_ticker.get(stock_input)
    else:
        st.warning("⚠️ 서버 연결 불안정으로 종목 목록을 불러오지 못했습니다. 아래에 종목코드를 직접 입력해주세요.")
        ticker_input = st.text_input("종목코드 6자리 입력 (예: 005930)", max_chars=6)
        if ticker_input and len(ticker_input) == 6 and ticker_input.isdigit():
            ticker = ticker_input

    if ticker:
        try:
            # 주가 정보 (FinanceDataReader)
            df_price = fdr.DataReader(ticker, datetime.now() - timedelta(days=7))
            
            if df_price.empty:
                st.error(f"데이터를 찾을 수 없습니다. (코드: {ticker})")
                return
            
            curr_price = df_price['Close'].iloc[-1]
            
            # 네이버 크롤링으로 추가 정보 수집 (여기서 종목명을 가져옴)
            naver_info = get_company_info_from_naver(ticker)
            annual, quarter = get_financials_from_naver(ticker)
            
            # 종목명 결정: 리스트에 있으면 리스트 사용, 없으면 크롤링 결과 사용
            display_name = ticker_to_name.get(ticker, naver_info['name'])

            st.divider()
            st.subheader(f"{display_name} ({ticker})")
            
            col1, col2 = st.columns(2)
            col1.metric("현재가", f"{curr_price:,.0f} 원")
            if naver_info['market_cap'] > 0:
                col2.metric("시가총액", f"{naver_info['market_cap']/100000000:,.0f} 억원")

            with st.expander("기업 개요"):
                st.write(naver_info['overview'])

            # 차트 링크
            st.markdown(f"""
                <a href="https://m.stock.naver.com/item/main.nhn?code={ticker}#/chart" target="_blank" style="text-decoration:none;">
                    <div style="background-color:#03C75A; color:white; padding:12px; border-radius:8px; text-align:center; font-weight:bold; margin: 10px 0;">
                        📊 네이버 증권 차트 보기
                    </div>
                </a>
                """, unsafe_allow_html=True)
            
            # 차트 이미지 프리뷰
            t_stamp = int(time.time())
            st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/day/{ticker}.png?t={t_stamp}", use_container_width=True)

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
            st.error(f"오류 발생: {e}")

if __name__ == "__main__":
    main()
