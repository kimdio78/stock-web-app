import streamlit as st
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import urllib3
import FinanceDataReader as fdr
import time
import re
import io
import zipfile
import xml.etree.ElementTree as ET
import json
import os

# SSL 경고 무시 (공공기관 사내망/프록시 환경 우회용)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =====================================================================
# [개선 §1] 네트워크 세션 최적화 (Connection Pooling & 자동 재시도)
# =====================================================================
http = requests.Session()
retry = Retry(connect=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
http.mount('https://', HTTPAdapter(max_retries=retry))
http.mount('http://', HTTPAdapter(max_retries=retry))

# [v3] 한국 표준시 — Streamlit Cloud는 UTC로 구동되므로 명시적으로 KST 변환
KST = timezone(timedelta(hours=9))


# =====================================================================
# 메타/보조 유틸 — 토큰 절감용 (타임스탬프, 유동성, 매크로)
# =====================================================================
def get_data_timestamp():
    now = datetime.now(KST)
    wd = now.weekday()
    hm = now.hour * 100 + now.minute
    if wd >= 5:
        session = "휴장(주말)"
    elif 900 <= hm <= 1530:
        session = "장중"
    elif hm < 900:
        session = "장 시작 전"
    else:
        session = "장 마감 이후"
    return {
        "iso": now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "human": now.strftime("%Y-%m-%d %H:%M KST"),
        "session": session,
    }

def _to_num(text):
    if text is None: return None
    try:
        m = re.search(r'-?\d[\d,]*\.?\d*', str(text))
        if not m: return None
        return float(m.group().replace(',', ''))
    except Exception:
        return None

def get_liquidity_and_band(ticker, high_52=None, low_52=None, curr_price=0):
    out = {"adtv": None, "vol_avg20": None, "vol_today": None, "vol_ratio": None, "band_pos": None}
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=45)
        df = fdr.DataReader(ticker, start, end)
        if df is not None and not df.empty:
            df = df.tail(20)
            tv = (df['Close'] * df['Volume'])
            out["adtv"] = float(tv.mean())
            out["vol_avg20"] = float(df['Volume'].mean())
            out["vol_today"] = float(df['Volume'].iloc[-1])
            if out["vol_avg20"]:
                out["vol_ratio"] = out["vol_today"] / out["vol_avg20"]
    except Exception:
        pass
    try:
        hi = _to_num(high_52)
        lo = _to_num(low_52)
        if hi and lo and hi > lo and curr_price > 0:
            out["band_pos"] = (curr_price - lo) / (hi - lo) * 100
    except Exception:
        pass
    return out

def _kr10y_from_fdr():
    symbols = ['KR10YT=RR', 'KR10Y', 'KR10YT', 'KR3YT=RR']
    for sym in symbols[:3]:
        try:
            end = datetime.now(KST).date()
            start = end - timedelta(days=20)
            df = fdr.DataReader(sym, start, end)
            if df is not None and not df.empty:
                col = 'Close' if 'Close' in df.columns else df.columns[0]
                val = float(df[col].dropna().iloc[-1])
                if 0 < val < 20: return val, f"FDR:{sym}"
        except Exception: continue
    return None, None

def _kr10y_from_naver():
    try:
        url = "https://finance.naver.com/marketindex/bondList.naver"
        r = http.get(url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            for tr in soup.select("tr"):
                txt = tr.get_text()
                if "국고채" in txt and "10년" in txt:
                    nums = re.findall(r'\d+\.\d+', txt)
                    for n in nums:
                        v = float(n)
                        if 0 < v < 20: return v, "네이버 시장지표"
    except Exception: pass
    return None, None

def _kr10y_from_ecos(ecos_key):
    if not ecos_key: return None, None
    try:
        end = datetime.now(KST)
        start = end - timedelta(days=30)
        s = start.strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")
        url = f"https://ecos.bok.or.kr/api/StatisticSearch/{ecos_key}/json/kr/1/100/817Y002/D/{s}/{e}/010210000"
        j = http.get(url, timeout=12).json()
        rows = j.get("StatisticSearch", {}).get("row", [])
        vals = [float(r["DATA_VALUE"]) for r in rows if r.get("DATA_VALUE")]
        if vals: return vals[-1], "한국은행 ECOS"
    except Exception: pass
    return None, None

def get_macro_indicators(ecos_key=""):
    out = {"usdkrw": None, "kr10y": None, "kr10y_src": None}
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=10)
        fx = fdr.DataReader('USD/KRW', start, end)
        if fx is not None and not fx.empty: out["usdkrw"] = float(fx['Close'].iloc[-1])
    except Exception: pass
    
    val, src = None, None
    if ecos_key: val, src = _kr10y_from_ecos(ecos_key)
    if val is None: val, src = _kr10y_from_naver()
    if val is None: val, src = _kr10y_from_fdr()
    
    out["kr10y"] = val
    out["kr10y_src"] = src
    return out


MOBILE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
    'Referer': 'https://m.stock.naver.com/',
}

def _fetch_json(url):
    try:
        r = http.get(url, headers=MOBILE_HEADERS, verify=False, timeout=10)
        if r.status_code == 200: return r.json()
    except Exception: return None
    return None

def _deep_find(obj, key_substrings, results=None, path="", capture_all=False):
    if results is None: results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            matched = any(s.lower() in kl for s in key_substrings)
            newpath = f"{path}.{k}"
            if isinstance(v, (dict, list)): _deep_find(v, key_substrings, results, newpath, capture_all or matched)
            elif matched or capture_all: results.append((newpath, v))
    elif isinstance(obj, list):
        for i, item in enumerate(obj): _deep_find(item, key_substrings, results, f"{path}[{i}]", capture_all)
    return results

def _recomm_label(score):
    if score is None: return None
    s = float(score)
    if s >= 4.5: return "강력매수"
    if s >= 3.5: return "매수"
    if s >= 2.5: return "중립"
    if s >= 1.5: return "매도"
    return "강력매도"

# [개선 §2] 개별 데이터 조회 함수에 단기 캐싱 (ttl=300초) 적용
@st.cache_data(ttl=300)
def get_naver_mobile_consensus(ticker):
    out = {"target_avg": None, "target_high": None, "target_low": None, "opinion": None, "opinion_score": None, "create_date": None, "raw": None, "found": []}
    data = _fetch_json(f"https://m.stock.naver.com/api/stock/{ticker}/integration")
    if data is None: data = _fetch_json(f"https://m.stock.naver.com/api/stock/{ticker}/basic")
    if data is None: return out
    out["raw"] = data

    def _find_consensus_node(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k).lower() == "consensusinfo" and isinstance(v, dict): return v
                r = _find_consensus_node(v)
                if r is not None: return r
        elif isinstance(o, list):
            for item in o:
                r = _find_consensus_node(item)
                if r is not None: return r
        return None

    cnode = _find_consensus_node(data)
    if cnode:
        out["target_avg"] = _to_num(cnode.get("priceTargetMean"))
        out["target_high"] = _to_num(cnode.get("priceTargetHigh"))
        out["target_low"] = _to_num(cnode.get("priceTargetLow"))
        score = _to_num(cnode.get("recommMean"))
        if score is not None:
            out["opinion_score"] = score
            out["opinion"] = _recomm_label(score)
        if cnode.get("createDate"): out["create_date"] = str(cnode["createDate"])

    out["found"] = _deep_find(data, ["target", "목표", "consensus", "컨센", "opinion", "투자의견", "recomm", "pricetarget", "estimateprice"])[:50]
    return out


@st.cache_data(ttl=86400)
def dart_load_corpcode_map(api_key):
    try:
        url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={api_key}"
        r = http.get(url, timeout=20, verify=False)
        if r.status_code != 200: return {}
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml_name = zf.namelist()[0]
        root = ET.fromstring(zf.read(xml_name).decode("utf-8"))
        mapping = {}
        for item in root.iter("list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_code = (item.findtext("corp_code") or "").strip()
            if stock_code and corp_code and stock_code != " ": mapping[stock_code] = corp_code
        return mapping
    except Exception: return {}

def _dart_amount(row, key):
    v = row.get(key, "")
    if v in ("", "-", None): return None
    return _to_num(v)

@st.cache_data(ttl=300)
def dart_get_cashflow(api_key, corp_code, years=3):
    out = {"annual": [], "source": None, "raw_sample": None, "error": None}
    if not api_key or not corp_code:
        out["error"] = "API 키 또는 corp_code 없음"
        return out
    this_year = datetime.now(KST).year
    cfo_by_year = {}
    raw_sample = None
    for by in range(this_year - 1, this_year - 1 - years - 1, -1):
        if len([y for y in cfo_by_year if y is not None]) >= years: break
        data = None
        for fs_div in ("CFS", "OFS"):
            url = f"https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?crtfc_key={api_key}&corp_code={corp_code}&bsns_year={by}&reprt_code=11011&fs_div={fs_div}"
            try:
                resp = http.get(url, timeout=15, verify=False)
                j = resp.json()
            except Exception: continue
            if j.get("status") == "013": continue
            if j.get("list"):
                data = j["list"]
                out["source"] = f"DART {fs_div} (사업보고서 11011)"
                break
        if not data: continue
        if raw_sample is None: raw_sample = [r for r in data if r.get("sj_div") == "CF"][:8]
        for row in data:
            if row.get("sj_div") != "CF": continue
            nm = (row.get("account_nm") or "").replace(" ", "")
            if "영업활동" in nm and not any(x in nm for x in ("투자활동", "재무활동")):
                amt_t = _dart_amount(row, "thstrm_amount")
                amt_f = _dart_amount(row, "frmtrm_amount")
                amt_b = _dart_amount(row, "bfefrmtrm_amount")
                if amt_t is not None: cfo_by_year[by] = amt_t
                if amt_f is not None: cfo_by_year.setdefault(by - 1, amt_f)
                if amt_b is not None: cfo_by_year.setdefault(by - 2, amt_b)
                break
        if cfo_by_year: break
    out["raw_sample"] = raw_sample
    for y in sorted(cfo_by_year.keys())[-years:]:
        val_won = cfo_by_year[y]
        out["annual"].append({"date": f"{y}.12", "value": val_won / 1e8})
    if not out["annual"] and out["error"] is None: out["error"] = "CFO 행을 찾지 못함 (CF 항목 확인 필요)"
    return out

def _dart_fetch_report(api_key, endpoint, corp_code, prefer_years=2):
    this_year = datetime.now(KST).year
    for by in range(this_year - 1, this_year - 1 - prefer_years - 1, -1):
        url = f"https://opendart.fss.or.kr/api/{endpoint}.json?crtfc_key={api_key}&corp_code={corp_code}&bsns_year={by}&reprt_code=11011"
        try: j = http.get(url, timeout=15, verify=False).json()
        except Exception: continue
        if j.get("status") == "013": continue
        if j.get("list"): return j["list"], by
    return [], None

@st.cache_data(ttl=300)
def dart_get_shareholders(api_key, corp_code):
    out = {"top": None, "rows": [], "total_rate": None, "year": None, "error": None, "raw": None}
    if not api_key or not corp_code: return {"error": "키/코드 없음"}
    rows, by = _dart_fetch_report(api_key, "hyslrSttus", corp_code)
    out["year"], out["raw"] = by, rows[:10]
    if not rows: return {"error": "최대주주 데이터 없음"}
    parsed = []
    for r in rows:
        nm, relate = r.get("nm"), r.get("relate")
        rate = _to_num(r.get("trmend_posesn_stock_qota_rt")) or _to_num(r.get("bsis_posesn_stock_qota_rt"))
        if nm and rate is not None: parsed.append({"이름": nm, "관계": relate or "-", "지분율": rate})
    total = next((r["지분율"] for r in parsed if r["이름"] and any(x in str(r["이름"]) for x in ("계", "합계"))), None)
    if total is None and parsed: total = sum(p["지분율"] for p in parsed if not any(x in str(p["이름"]) for x in ("계", "합계")))
    indiv = [p for p in parsed if not any(x in str(p["이름"]) for x in ("계", "합계"))]
    if indiv: out["top"] = max(indiv, key=lambda x: x["지분율"])
    out["rows"], out["total_rate"] = parsed[:8], total
    return out

@st.cache_data(ttl=300)
def dart_get_dividend(api_key, corp_code):
    out = {"items": {}, "year": None, "error": None, "raw": None}
    if not api_key or not corp_code: return {"error": "키/코드 없음"}
    rows, by = _dart_fetch_report(api_key, "alotMatter", corp_code)
    out["year"], out["raw"] = by, rows[:20]
    if not rows: return {"error": "배당 데이터 없음"}
    wanted = {"주당 현금배당금": "DPS", "주당현금배당금": "DPS", "현금배당성향": "배당성향", "배당성향": "배당성향", "현금배당수익률": "배당수익률", "배당수익률": "배당수익률"}
    for r in rows:
        se = (r.get("se") or "").replace(" ", "")
        for k, label in wanted.items():
            if k.replace(" ", "") in se:
                vals = {"당기": _to_num(r.get("thstrm")), "전기": _to_num(r.get("frmtrm")), "전전기": _to_num(r.get("lwfr"))}
                if label not in out["items"] or all(v is None for v in out["items"][label].values()): out["items"][label] = vals
                break
    if not out["items"]: out["error"] = "배당 항목 파싱 실패 (raw 확인)"
    return out

@st.cache_data(ttl=300)
def dart_get_treasury(api_key, corp_code):
    out = {"hold": None, "rows": [], "year": None, "error": None, "raw": None}
    if not api_key or not corp_code: return {"error": "키/코드 없음"}
    rows, by = _dart_fetch_report(api_key, "tesstkAcqsDspsSttus", corp_code)
    out["year"], out["raw"] = by, rows[:15]
    if not rows: return {"error": "자기주식 데이터 없음"}
    for r in rows:
        label = " ".join(str(r.get(k, "")) for k in ("acqs_mth1", "acqs_mth2", "acqs_mth3", "se"))
        qty = _to_num(r.get("trmend_qy")) or _to_num(r.get("hold_qy"))
        if qty is not None: out["rows"].append({"구분": label.strip() or "-", "수량": qty})
    for r in out["rows"]:
        if any(x in r["구분"] for x in ("보유", "총계", "합계", "계")): out["hold"] = r["수량"]
    if out["hold"] is None and out["rows"]: out["hold"] = out["rows"][-1]["수량"]
    return out

@st.cache_data(ttl=300)
def get_recent_disclosures(ticker, limit=10):
    try:
        url = f"https://finance.naver.com/item/news_notice.naver?code={ticker}"
        r = http.get(url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=10)
        items = []
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            for tr in soup.select("table tr"):
                a = tr.select_one("a")
                tds = tr.select("td")
                if a and tds:
                    title = re.sub(r'\s+', ' ', a.text.strip())
                    date = tds[-1].text.strip()
                    if title and not title.startswith("연관기사"): items.append({"title": title, "date": date})
                if len(items) >= limit: break
        return items
    except Exception: return []

@st.cache_data(ttl=86400)
def get_beta(ticker):
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=365 * 5 + 30)
        s = fdr.DataReader(ticker, start, end)['Close'].resample('ME').last()
        m = fdr.DataReader('KS11', start, end)['Close'].resample('ME').last()
        df = pd.concat([s, m], axis=1, keys=['s', 'm']).dropna()
        rs = df['s'].pct_change().dropna()
        rm = df['m'].pct_change().dropna()
        common = rs.index.intersection(rm.index)
        rs, rm = rs.loc[common], rm.loc[common]
        if len(rs) < 24: return None
        var = ((rm - rm.mean()) ** 2).mean()
        if var == 0: return None
        cov = ((rs - rs.mean()) * (rm - rm.mean())).mean()
        return float(cov / var)
    except Exception: return None

@st.cache_data(ttl=3600)
def load_listing_df():
    try: return fdr.StockListing('KRX')
    except Exception: return pd.DataFrame()

def get_sector_and_rank(ticker, listing_df):
    out = {"sector": None, "industry": None, "market": None, "marcap_rank": None}
    try:
        if listing_df is None or listing_df.empty: return out
        code_col = 'Code' if 'Code' in listing_df.columns else listing_df.columns[0]
        row = listing_df[listing_df[code_col] == ticker]
        if row.empty: return out
        r0 = row.iloc[0]
        for c in ['Sector', 'sector', '업종']:
            if c in listing_df.columns: out["sector"] = r0.get(c); break
        for c in ['Industry', 'industry']:
            if c in listing_df.columns: out["industry"] = r0.get(c); break
        for c in ['Market', 'market']:
            if c in listing_df.columns: out["market"] = r0.get(c); break
        if 'Marcap' in listing_df.columns and out["market"] and 'Market' in listing_df.columns:
            sub = listing_df[listing_df['Market'] == out["market"]].sort_values('Marcap', ascending=False).reset_index(drop=True)
            pos = sub.index[sub[code_col] == ticker].tolist()
            if pos: out["marcap_rank"] = int(pos[0]) + 1
    except Exception: pass
    return out

@st.cache_data(ttl=3600)
def load_stock_data():
    try:
        df = fdr.StockListing('KRX')
        if not df.empty:
            df['Search_Key'] = df['Name'] + " (" + df['Code'] + ")"
            search_map = dict(zip(df['Search_Key'], df['Code']))
            ticker_to_name = dict(zip(df['Code'], df['Name']))
            return list(search_map.keys()), search_map, ticker_to_name
    except: pass
    return [], {}, {}

@st.cache_data(ttl=300)
def get_naver_stock_details(ticker):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        response = http.get(url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=10)
        data = {'name': ticker, 'overview': "정보 없음", 'now_price': '0', 'diff_rate': '0.00', 'diff_amount': '0', 'direction': 'flat', 'market_cap': '-', 'shares': 0, 'foreign_rate': '-', 'per': '-', 'eps': '-', 'pbr': '-', 'bps': '-', 'dvr': '-', 'high_52': '-', 'low_52': '-'}
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            name_tag = soup.select_one(".wrap_company h2 a")
            if name_tag: data['name'] = name_tag.text.strip()
            overview_div = soup.select_one("#summary_info")
            if overview_div: data['overview'] = "\n ".join([p.text.strip() for p in overview_div.select("p") if p.text.strip()])
            try:
                now_tag = soup.select_one(".no_today .blind")
                if now_tag: data['now_price'] = now_tag.text.strip()
                exday_tag = soup.select_one(".no_exday")
                if exday_tag:
                    spans = exday_tag.select("span.blind")
                    if len(spans) >= 2: data['diff_amount'], data['diff_rate'] = spans[0].text.strip(), spans[1].text.strip()
                    if exday_tag.select_one(".ico.up"): data['direction'] = 'up'
                    elif exday_tag.select_one(".ico.down"): data['direction'] = 'down'
                    elif exday_tag.select_one(".ico.upper"): data['direction'] = 'upper'
                    elif exday_tag.select_one(".ico.lower"): data['direction'] = 'lower'
            except: pass
            try:
                mc = soup.select_one("#_market_sum")
                if mc: data['market_cap'] = mc.text.strip().replace('\t', '').replace('\n', '') + " 억원"
            except: pass
            try:
                for tr in soup.select("div.first table tr"):
                    if "상장주식수" in tr.text:
                        em = tr.select_one("em")
                        if em: data['shares'] = int(em.text.strip().replace(',', ''))
                        break
            except: pass
            for k, s in [('per', '#_per'), ('eps', '#_eps'), ('pbr', '#_pbr'), ('dvr', '#_dvr')]:
                el = soup.select_one(s)
                if el: data[k] = el.text.strip()
            for th in soup.select("th"):
                th_text = th.text.strip()
                if "외국인소진율" in th_text:
                    td = th.find_next_sibling("td")
                    if td: data['foreign_rate'] = td.select_one("em").text.strip() if td.select_one("em") else td.text.strip()
                elif "52주최고" in th_text:
                    td = th.find_next_sibling("td")
                    if td and len(td.select("em")) >= 2: data['high_52'], data['low_52'] = td.select("em")[0].text.strip(), td.select("em")[1].text.strip()
                elif "BPS" in th_text and "PBR" not in th_text:
                    td = th.find_next_sibling("td")
                    if td: data['bps'] = td.select_one("em").text.strip() if td.select_one("em") else td.text.strip()
        return data
    except: return {'name': ticker, 'overview': "로딩 실패", 'shares': 0}

@st.cache_data(ttl=300)
def get_investor_trend(ticker):
    try:
        url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
        response = http.get(url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=10)
        trends = []
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            tables = soup.select("table.type2")
            if len(tables) >= 2:
                for row in tables[1].select("tr"):
                    cols = row.select("td")
                    if len(cols) == 9:
                        trends.append({"날짜": cols[0].text.strip(), "종가": cols[1].text.strip(), "등락률": cols[3].text.strip().replace('\n', '').replace('\t', ''), "기관": cols[5].text.strip(), "외국인": cols[6].text.strip(), "보유율": cols[8].text.strip()})
                        if len(trends) >= 10: break
        return trends
    except: return []

@st.cache_data(ttl=300)
def get_same_industry_comparison(ticker):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        response = http.get(url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            table = soup.select_one("div.section.trade_compare table")
            if table:
                headers = ["구분"] + [th.text.strip().split('*')[0].strip() for th in table.select("thead th") if th.find("a")]
                rows_data = []
                for tr in table.select("tbody tr"):
                    th_item = tr.select_one("th")
                    row_title = th_item.text.strip() if th_item else ""
                    row_val = [row_title] if row_title else []
                    for td in tr.select("td"):
                        clean_text = re.sub(r'\s+', ' ', td.text.strip())
                        if row_title in ["전일대비", "등락률"]:
                            val_text = re.sub(r'[^0-9.,%]', '', clean_text)
                            if any(x in clean_text for x in ["상향", "상승", "+"]): clean_text = f'<span style="color:#d20000">+{val_text}</span>'
                            elif any(x in clean_text for x in ["하향", "하락", "-"]): clean_text = f'<span style="color:#0051c7">-{val_text}</span>'
                            elif "보합" in clean_text: clean_text = val_text
                        row_val.append(clean_text)
                    if len(row_val) >= len(headers): rows_data.append(row_val[:len(headers)])
                return pd.DataFrame(rows_data, columns=headers)
        return pd.DataFrame()
    except: return pd.DataFrame()

def clean_float(text):
    if not text or text.strip() in ['-', 'N/A', '', '.']: return 0.0
    try:
        match = re.search(r'-?\d+\.?\d*', text.replace(',', ''))
        return float(match.group()) if match else 0.0
    except: return 0.0

@st.cache_data(ttl=300)
def get_financials_from_naver(ticker, current_price=0, shares=0):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        response = http.get(url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        finance_table = soup.select_one("div.section.cop_analysis > div.sub_section > table")
        if not finance_table: return [], [], []

        date_cols = [th.text.strip() for th in finance_table.select("thead > tr")[1].select("th")]
        annual_idxs, quarter_idxs, estimate_idxs = [], [], []
        
        for i, col in enumerate(date_cols):
             if "(E)" in col: estimate_idxs.append(i)
             elif i < 4: annual_idxs.append(i)
             else: quarter_idxs.append(i)
        
        annual_idxs, quarter_idxs = annual_idxs[-3:], quarter_idxs[-5:]
        annual_data = [{'date': date_cols[i].split('(')[0]} for i in annual_idxs]
        quarter_data = [{'date': date_cols[i].split('(')[0]} for i in quarter_idxs]
        estimate_data = [{'date': date_cols[i].split('(')[0].strip()} for i in estimate_idxs]

        items_map_main = {"매출액": "revenue", "영업이익": "op_income", "당기순이익": "net_income", "영업이익률": "op_margin", "순이익률": "net_income_margin", "ROE": "roe", "부채비율": "debt_ratio", "당좌비율": "quick_ratio", "유보율": "reserve_ratio", "EPS": "eps", "BPS": "bps", "PER": "per", "PBR": "pbr", "주당배당금": "dps", "배당성향": "payout_ratio", "시가배당률": "dividend_yield"}

        def fill_data(target_list, indices):
            for i, idx in enumerate(indices):
                for row in finance_table.select("tbody > tr"):
                    th_text = row.th.text.strip().replace(" ", "")
                    key = next((k_key for k_txt, k_key in items_map_main.items() if k_txt in th_text and not (k_txt in ["영업이익", "당기순이익"] and "률" in th_text)), None)
                    if key:
                        cells = row.select("td")
                        t_idx = idx - (len(date_cols) - len(cells))
                        if 0 <= t_idx < len(cells): target_list[i][key] = clean_float(cells[t_idx].text.strip())
                
                rev = target_list[i].get('revenue', 0)
                if rev and shares > 0:
                     sps = (rev * 100000000) / shares
                     target_list[i]['sps'] = sps
                     if current_price > 0: target_list[i]['psr'] = current_price / sps
        
        fill_data(annual_data, annual_idxs); fill_data(quarter_data, quarter_idxs); fill_data(estimate_data, estimate_idxs)
        return annual_data, quarter_data, estimate_data
    except: return [], [], []

def calculate_srim(bps, roe, rrr):
    if rrr <= 0: return 0
    return bps + (bps * ((roe - rrr) / 100) / (rrr / 100))

def calculate_srim_w(bps, roe, rrr, w):
    if rrr <= 0 or bps <= 0: return 0
    r, excess = rrr / 100.0, (roe - rrr) / 100.0
    denom = (1 + r - w)
    if w >= 1.0 or denom <= 0: return bps + bps * excess / r
    return bps + bps * excess * w / denom

def roe_volatility(roe_values):
    vals = [v for v in roe_values if v is not None]
    n = len(vals)
    if n == 0: return None
    mean = sum(vals) / n
    std = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n >= 2 else 0.0
    cv = (std / abs(mean) * 100) if mean else None
    grade = "판정불가" if cv is None else ("안정 → S-RIM 정상 적용" if cv <= 30 else ("보통" if cv <= 50 else "과대 → S-RIM 참고용(신뢰구간 넓음)"))
    return {"mean": mean, "std": std, "cv": cv, "grade": grade}


# --- 메인 UI ---
def main():
    st.set_page_config(page_title="주식 적정주가 분석기", page_icon="📈", layout="wide")

    st.markdown("""
    <style>
    @media print {
        .scroll-table { overflow: visible !important; white-space: normal !important; }
        .scroll-table table { font-size: 0.68rem !important; table-layout: fixed; word-break: break-all; width: 100% !important; }
        .scroll-table th, .scroll-table td { white-space: normal !important; padding: 4px !important; }
        .scroll-table th:first-child, .scroll-table td:first-child { position: static !important; }
        table { page-break-inside: auto; }
        tr { page-break-inside: avoid; }
        .asof-box { border: 1px solid #888 !important; }
    }
    .asof-box { background: rgba(3,199,90,0.08); border: 1px solid rgba(3,199,90,0.5); border-radius: 8px; padding: 10px 14px; margin: 6px 0 14px 0; font-size: 0.9rem; }
    .asof-box code { font-size: 0.8rem; color: #555; }
    @media (prefers-color-scheme: dark) { .asof-box code { color: #bbb; } }
    </style>
    """, unsafe_allow_html=True)
    
    if 'search_list' not in st.session_state:
        with st.spinner('종목 데이터 로딩 중...'):
            st.session_state.search_list, st.session_state.search_map, st.session_state.ticker_to_name = load_stock_data()
    
    search_list = st.session_state.search_list
    search_map = st.session_state.search_map

    with st.sidebar:
        st.header("설정")
        
        # [개선 §3] CAPM 기반 요구수익률 자동 산출 옵션 적용
        use_capm = st.checkbox(
            "CAPM 기반 요구수익률 자동 산출", 
            value=False, 
            help="체크 시 국고채 10년물 금리 + (개별 종목 베타 × 시장위험프리미엄 5.5%) 공식으로 자동 계산됩니다."
        )
        if not use_capm:
            required_return = st.number_input("요구수익률 (%)", 1.0, 20.0, 8.0, 0.5)
        else:
            required_return = None # 분석 수행 단계에서 API 획득 시점 이후 자동 산정됨
            
        st.divider()
        st.markdown("**API 인증키 설정**")

        CONFIG_FILE = "api_config.json"
        saved_dart, saved_ecos = "", ""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    config = json.load(f)
                    saved_dart, saved_ecos = config.get("DART_API_KEY", ""), config.get("ECOS_API_KEY", "")
            except Exception: pass

        try: secret_dart = st.secrets.get("DART_API_KEY", "")
        except: secret_dart = ""
        try: secret_ecos = st.secrets.get("ECOS_API_KEY", "")
        except: secret_ecos = ""

        if secret_dart: dart_api_key = secret_dart; st.success("DART 인증키 적용됨 (Secrets)")
        else: dart_api_key = st.text_input("DART 인증키", value=saved_dart, type="password")
            
        if secret_ecos: ecos_api_key = secret_ecos; st.success("ECOS 인증키 적용됨 (Secrets)")
        else: ecos_api_key = st.text_input("ECOS 인증키 (선택)", value=saved_ecos, type="password")

        if (dart_api_key != saved_dart) or (ecos_api_key != saved_ecos):
            with open(CONFIG_FILE, "w") as f: json.dump({"DART_API_KEY": dart_api_key, "ECOS_API_KEY": ecos_api_key}, f)
            st.rerun()

    st.markdown("##### 종목 검색")
    col_search, col_reset = st.columns([4, 1])
    
    ticker = None
    with col_search:
        # [개선 §4] st.session_state 키 조작 대신 index=None 속성을 이용해 깔끔한 초기화 적용
        if search_list:
            stock_input = st.selectbox(
                "종목을 선택하거나 입력하세요", 
                options=search_list,
                index=None,
                label_visibility="collapsed",
                placeholder="종목명 또는 코드를 입력하세요..."
            )
            if stock_input: ticker = search_map.get(stock_input)
        else:
            ticker_input = st.text_input("종목코드(6자리) 직접 입력")
            if ticker_input and len(ticker_input) == 6 and ticker_input.isdigit(): ticker = ticker_input
    
    with col_reset:
        if st.button("🔄 초기화"):
            st.cache_data.clear()
            st.rerun()

    if ticker:
        try:
            info = get_naver_stock_details(ticker)
            try: curr_price = float(info['now_price'].replace(',', ''))
            except: curr_price = 0
            
            annual_list, quarter_list, estimate_list = get_financials_from_naver(ticker, curr_price, info.get('shares', 0))
            investor_trends = get_investor_trend(ticker)
            industry_compare_df = get_same_industry_comparison(ticker)
            ts = get_data_timestamp()
            liq = get_liquidity_and_band(ticker, info.get('high_52'), info.get('low_52'), curr_price)
            macro = get_macro_indicators(ecos_key)
            listing_df = load_listing_df()
            sector_info = get_sector_and_rank(ticker, listing_df)
            beta = get_beta(ticker)
            consensus = get_naver_mobile_consensus(ticker)
            
            cashflow = {"annual": [], "source": None, "error": None, "raw_sample": None}
            dart_gov = {"top": None, "rows": [], "total_rate": None, "error": "키 없음"}
            dart_div = {"items": {}, "error": "키 없음"}
            dart_tres = {"hold": None, "rows": [], "error": "키 없음"}
            if dart_api_key:
                corp_map = dart_load_corpcode_map(dart_api_key)
                corp_code = corp_map.get(ticker)
                if corp_code:
                    cashflow = dart_get_cashflow(dart_api_key, corp_code, years=3)
                    cashflow["corp_code"] = corp_code
                    dart_gov = dart_get_shareholders(dart_api_key, corp_code)
                    dart_div = dart_get_dividend(dart_api_key, corp_code)
                    dart_tres = dart_get_treasury(dart_api_key, corp_code)
                else: cashflow["error"] = "종목코드→DART 고유번호 매핑 실패"
            disclosures = get_recent_disclosures(ticker)

            st.markdown(f"### {info['name']} ({ticker})")

            # [개선 §3 - CAPM 수행] 산출 로직
            if use_capm:
                kr10y = macro.get('kr10y')
                if kr10y is not None and beta is not None:
                    erp = 5.5 # 통상 실무 기준 한국 시장위험프리미엄 5.5% 가정
                    required_return = kr10y + (beta * erp)
                    st.sidebar.success(f"**CAPM 요구수익률:** {required_return:.2f}% 산출 적용")
                else:
                    required_return = 8.0
                    st.sidebar.warning("금리 또는 베타 누락으로 요구수익률 8.0% 강제 적용")

            adtv_txt = f"{liq['adtv']/1e8:,.1f}억원" if liq.get('adtv') else "N/A"
            usdkrw_txt = f"{macro['usdkrw']:,.1f}" if macro.get('usdkrw') else "N/A"
            kr10y_txt = f"{macro['kr10y']:.3f}% ({macro.get('kr10y_src','')})" if macro.get('kr10y') else "검색요(미수집)"
            sector_txt = sector_info.get('sector') or sector_info.get('industry') or "N/A"
            rank_txt = f"{sector_info['market'] or ''} {sector_info['marcap_rank']}위" if sector_info.get('marcap_rank') else "N/A"
            beta_txt = f"{beta:.2f}" if beta is not None else "N/A"
            
            st.markdown(f"""
            <div class="asof-box">
            📌 <b>데이터 기준</b>: {ts['human']} · <b>{ts['session']}</b><br>
            🔗 <b>출처</b>: 네이버 증권 / 네이버 모바일API / FinanceDataReader<br>
            🏷️ <b>업종</b>: {sector_txt} · <b>시총순위</b>: {rank_txt} · <b>베타(5Y月)</b>: {beta_txt}<br>
            🌐 <b>시장지표</b>: USD/KRW {usdkrw_txt} · 국고채10Y {kr10y_txt} · 20일 ADTV {adtv_txt}
            </div>
            """, unsafe_allow_html=True)
            
            diff_color = "#d20000" if info['direction'] in ['up', 'upper'] else "#0051c7" if info['direction'] in ['down', 'lower'] else "black"
            diff_arrow = "▲" if info['direction'] in ['up', 'upper'] else "▼" if info['direction'] in ['down', 'lower'] else ""
            
            st.markdown(f"""
            <div style="display:flex; align-items:flex-end; gap:10px; margin-bottom:10px;">
                <span style="font-size: 2.5rem; font-weight: bold; color:{diff_color};">{info['now_price']}</span>
                <span style="font-size: 1.2rem; color:{diff_color}; margin-bottom: 8px;">{diff_arrow} {info['diff_amount']} ({info['diff_rate']}%)</span>
            </div>
            """, unsafe_allow_html=True)
            
            st.markdown("""
            <style>
            .stock-info-container { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 10px; margin-bottom: 12px; }
            @media (max-width: 600px) { .stock-info-container { grid-template-columns: repeat(2, 1fr); } }
            .stock-info-box { background-color: rgba(128, 128, 128, 0.1); padding: 10px; border-radius: 5px; text-align: center; }
            .stock-info-label { font-size: 12px; color: #666; margin-bottom: 4px; }
            .stock-info-value { font-size: 15px; font-weight: bold; color: #333; white-space: nowrap; }
            @media (prefers-color-scheme: dark) { .stock-info-label { color: #aaa; } .stock-info-value { color: #fff; } }
            </style>
            """, unsafe_allow_html=True)

            info_html = f"""
            <div class="stock-info-container">
                <div class="stock-info-box"><div class="stock-info-label">시가총액</div><div class="stock-info-value">{info['market_cap']}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">외국인소진율</div><div class="stock-info-value">{info['foreign_rate']}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">PER</div><div class="stock-info-value">{info['per']} 배</div></div>
                <div class="stock-info-box"><div class="stock-info-label">PBR</div><div class="stock-info-value">{info['pbr']} 배</div></div>
                <div class="stock-info-box"><div class="stock-info-label">52주 최고</div><div class="stock-info-value">{info['high_52']}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">52주 최저</div><div class="stock-info-value">{info['low_52']}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">EPS</div><div class="stock-info-value">{info['eps']} 원</div></div>
                <div class="stock-info-box"><div class="stock-info-label">배당수익률</div><div class="stock-info-value">{info['dvr']} %</div></div>
            </div>
            """
            st.markdown(info_html, unsafe_allow_html=True)

            adtv_box = f"{liq['adtv']/1e8:,.1f} 억원" if liq.get('adtv') else "N/A"
            volr_box = f"{liq['vol_ratio']*100:,.0f} %" if liq.get('vol_ratio') else "N/A"
            band_box = f"{liq['band_pos']:.1f} %" if liq.get('band_pos') is not None else "N/A"
            st.markdown(f"""
            <div class="stock-info-container" style="grid-template-columns: repeat(3, 1fr);">
                <div class="stock-info-box"><div class="stock-info-label">20일 평균 거래대금(ADTV)</div><div class="stock-info-value">{adtv_box}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">당일 거래량(20일比)</div><div class="stock-info-value">{volr_box}</div></div>
                <div class="stock-info-box"><div class="stock-info-label">52주 밴드 내 위치</div><div class="stock-info-value">{band_box}</div></div>
            </div>
            """, unsafe_allow_html=True)
            st.caption("※ ADTV는 종가×거래량 근사치(원). 52주 위치 0%=저점·100%=고점.")

            with st.expander("기업 개요 보기"): st.write(info['overview'])

            st.markdown(f"""
                <a href="https://m.stock.naver.com/item/main.nhn?code={ticker}#/chart" target="_blank" style="text-decoration:none;">
                    <div style="background-color:#03C75A; color:white; padding:12px; border-radius:8px; text-align:center; font-weight:bold; margin: 15px 0;">📊 네이버 증권 차트 보러가기</div>
                </a>
                """, unsafe_allow_html=True)
            
            t_stamp = int(time.time())
            tab_d, tab_w, tab_m = st.tabs(["일봉", "주봉", "월봉"])
            with tab_d: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/day/{ticker}.png?t={t_stamp}", use_container_width=True)
            with tab_w: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/week/{ticker}.png?t={t_stamp}", use_container_width=True)
            with tab_m: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/month/{ticker}.png?t={t_stamp}", use_container_width=True)

            if investor_trends:
                st.markdown("### 🏢 외국인/기관 매매동향 (최근 10일)")
                total_inst, total_frgn, est_inst_value, est_frgn_value = 0, 0, 0.0, 0.0
                for row in investor_trends:
                    close_v = _to_num(row.get('종가')) or 0
                    try: iv = int(row['기관'].replace('+', '').replace(',', '')); total_inst += iv; est_inst_value += iv * close_v
                    except: pass
                    try: fv = int(row['외국인'].replace('+', '').replace(',', '')); total_frgn += fv; est_frgn_value += fv * close_v
                    except: pass
                
                t_inst_color = "text-red" if total_inst > 0 else "text-blue" if total_inst < 0 else "text-black"
                t_inst_prefix = "+" if total_inst > 0 else "-" if total_inst < 0 else ""
                t_frgn_color = "text-red" if total_frgn > 0 else "text-blue" if total_frgn < 0 else "text-black"
                t_frgn_prefix = "+" if total_frgn > 0 else "-" if total_frgn < 0 else ""

                trend_html = """<style>
.trend-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-bottom: 8px; }
.trend-table th { background-color: rgba(128,128,128,0.1); text-align: center; padding: 6px; border-bottom: 1px solid rgba(128,128,128,0.2); }
.trend-table td { text-align: right; padding: 6px; border-bottom: 1px solid rgba(128,128,128,0.2); }
.total-row { background-color: rgba(128, 128, 128, 0.05); font-weight: bold; border-bottom: 2px solid rgba(128, 128, 128, 0.4); }
.text-red { color: #d20000; } .text-blue { color: #0051c7; } .text-black { color: inherit; }
@media (prefers-color-scheme: dark) { .text-black { color: #fff; } }
</style>
<div style="overflow-x:auto;">
<table class="trend-table">
<thead><tr><th>날짜</th><th>종가</th><th>등락률</th><th>기관</th><th>외국인</th><th>보유율</th></tr></thead>
<tbody>
"""
                trend_html += f"""<tr class="total-row"><td style="text-align:center;">10일 합계</td><td colspan="2" style="text-align:center;">-</td><td class="{t_inst_color}">{t_inst_prefix}{abs(total_inst):,}</td><td class="{t_frgn_color}">{t_frgn_prefix}{abs(total_frgn):,}</td><td>-</td></tr>"""

                for row in investor_trends:
                    try: inst_val = int(row['기관'].replace('+', '').replace(',', ''))
                    except: inst_val = 0
                    inst_color = "text-red" if inst_val > 0 else "text-blue" if inst_val < 0 else "text-black"
                    inst_prefix = "+" if inst_val > 0 else "-" if inst_val < 0 else ""
                    try: frgn_val = int(row['외국인'].replace('+', '').replace(',', ''))
                    except: frgn_val = 0
                    frgn_color = "text-red" if frgn_val > 0 else "text-blue" if frgn_val < 0 else "text-black"
                    frgn_prefix = "+" if frgn_val > 0 else "-" if frgn_val < 0 else ""
                    try: rate_val = float(row['등락률'].replace('%', ''))
                    except: rate_val = 0.0
                    rate_color = "text-red" if rate_val > 0 else "text-blue" if rate_val < 0 else "text-black"
                    trend_html += f'<tr><td style="text-align:center;">{row["날짜"]}</td><td style="text-align:right;">{row["종가"]}</td><td class="{rate_color}" style="text-align:right;">{row["등락률"]}</td><td class="{inst_color}" style="text-align:right;">{inst_prefix}{abs(inst_val):,}</td><td class="{frgn_color}" style="text-align:right;">{frgn_prefix}{abs(frgn_val):,}</td><td style="text-align:right;">{row["보유율"]}</td></tr>'
                
                trend_html += "</tbody></table></div>"
                st.markdown(trend_html, unsafe_allow_html=True)
                st.caption(f"※ 누적 순매수: 기관 ≈ {est_inst_value/1e8:,.0f}억원 · 외국인 ≈ {est_frgn_value/1e8:,.0f}억원.")

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

            items_display = [("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("영업이익률(%)", 'op_margin'), ("당기순이익(억)", 'net_income'), ("순이익률(%)", 'net_income_margin'), ("부채비율(%)", 'debt_ratio'), ("당좌비율(%)", 'quick_ratio'), ("유보율(%)", 'reserve_ratio'), ("EPS(원)", 'eps'), ("BPS(원)", 'bps'), ("SPS(원)", 'sps'), ("PER(배)", 'per'), ("PBR(배)", 'pbr'), ("PSR(배)", 'psr'), ("ROE(%)", 'roe'), ("주당배당금(원)", 'dps'), ("배당성향(%)", 'payout_ratio'), ("시가배당률(%)", 'dividend_yield')]

            def render_fin_table(title, data_list):
                disp = []
                cols = ['항목'] + [d['date'] for d in data_list]
                for label, key in items_display:
                    rowv = [label]
                    is_money = '원' in label or '억' in label
                    for d in data_list:
                        val = d.get(key, 0)
                        if val == 0 and key not in ['op_income', 'net_income']: rowv.append("-")
                        else: rowv.append(f"{val:,.0f}" if is_money else f"{val:,.2f}")
                    disp.append(rowv)
                html_ = pd.DataFrame(disp, columns=cols).to_html(index=False, border=0, classes='scroll-table-content')
                st.markdown(f"### {title}")
                st.markdown(f'<div class="scroll-table">{html_}</div>', unsafe_allow_html=True)

            if annual_list: render_fin_table("📊 연간 재무제표 (최근 3년)", annual_list)
            if quarter_list: render_fin_table("📊 분기 재무제표 (최근 5분기)", quarter_list)
            if estimate_list: render_fin_table("🔮 컨센서스 추정 (E)", estimate_list)

            st.markdown("### 🎯 컨센서스 목표주가 / 투자의견")
            if consensus.get("target_avg") or consensus.get("target_high") or consensus.get("target_low") or consensus.get("opinion"):
                c_avg, c_hi, c_lo, c_op, c_score, c_date = consensus.get("target_avg"), consensus.get("target_high"), consensus.get("target_low"), consensus.get("opinion"), consensus.get("opinion_score"), consensus.get("create_date")
                avg_s = f"{c_avg:,.0f}원" if c_avg else "N/A"
                hi_s = f"{c_hi:,.0f}원" if c_hi else "N/A"
                lo_s = f"{c_lo:,.0f}원" if c_lo else "N/A"
                up_s = f"{(c_avg/curr_price-1)*100:+.1f}%" if (c_avg and curr_price > 0) else "N/A"
                op_disp = f"{c_op} ({c_score:.2f})" if c_op and c_score is not None else f"{c_score:.2f}" if c_score is not None else c_op or "N/A"
                hilo_disp = f"{hi_s}/{lo_s}" if (c_hi or c_lo) else "평균만 제공"
                st.markdown(f"""
                <div class="stock-info-container" style="grid-template-columns: repeat(4, 1fr);">
                    <div class="stock-info-box"><div class="stock-info-label">목표가 평균</div><div class="stock-info-value">{avg_s}</div></div>
                    <div class="stock-info-box"><div class="stock-info-label">현재가 대비</div><div class="stock-info-value">{up_s}</div></div>
                    <div class="stock-info-box"><div class="stock-info-label">목표가 최고/최저</div><div class="stock-info-value">{hilo_disp}</div></div>
                    <div class="stock-info-box"><div class="stock-info-label">투자의견(5점척도)</div><div class="stock-info-value">{op_disp}</div></div>
                </div>
                """, unsafe_allow_html=True)
            else: st.info("컨센서스 목표가를 자동 추출하지 못했습니다.")

            st.markdown("### 💵 영업활동현금흐름(CFO) & 이익의 질")
            if cashflow.get("annual"):
                cfo_rows = [{"기간": r["date"], "CFO(억원)": (f"{r['value']:,.0f}" if r.get("value") is not None else "-")} for r in cashflow["annual"]]
                st.table(pd.DataFrame(cfo_rows))
                try:
                    for d in reversed(annual_list or []):
                        yr = re.search(r'20\d{2}', str(d.get("date", "")))
                        ni = d.get("net_income", 0)
                        if yr and ni:
                            cfo_v = next((r["value"] for r in cashflow["annual"] if yr.group() in str(r["date"])), None)
                            if cfo_v is not None:
                                ratio = cfo_v / ni
                                st.markdown(f"**CFO/순이익 ≈ {ratio:.2f}** ({d['date']} 기준) → {'양호(≥0.8)' if ratio >= 0.8 else ('주의(<0.5)' if ratio < 0.5 else '보통')}")
                                break
                except Exception: pass
            else:
                if not dart_api_key: st.info("사이드바에 DART 인증키를 입력하면 영업활동현금흐름을 표시합니다.")
                else: st.warning(f"CFO 수집 실패: {cashflow.get('error', '원인 미상')}")

            if dart_api_key:
                st.markdown("### 🇰🇷 지배구조·밸류업 (DART)")
                col_g, col_d = st.columns(2)
                with col_g:
                    st.markdown("**최대주주 현황 (§5.1)**")
                    if dart_gov.get("top"): st.markdown(f"최대주주: **{dart_gov['top']['이름']}** ({dart_gov['top']['관계']}) {dart_gov['top']['지분율']:.2f}%")
                    if dart_gov.get("total_rate") is not None: st.markdown(f"특수관계인 합계: **{dart_gov['total_rate']:.2f}%**")
                    if dart_gov.get("rows"):
                        gov_df = pd.DataFrame(dart_gov["rows"]); gov_df["지분율"] = gov_df["지분율"].apply(lambda x: f"{x:.2f}%")
                        st.table(gov_df)
                with col_d:
                    st.markdown("**배당 추이 (§5.2)**")
                    if dart_div.get("items"):
                        div_rows = []
                        for label, vals in dart_div["items"].items():
                            row = {"항목": label}
                            for t in ["당기", "전기", "전전기"]:
                                v = vals.get(t)
                                row[t] = (f"{v:,.0f}" if (v is not None and label == "DPS") else (f"{v:.2f}" if v is not None else "-"))
                            div_rows.append(row)
                        st.table(pd.DataFrame(div_rows))
                
                st.markdown("**자기주식 현황 (§5.2)**")
                if dart_tres.get("hold") is not None: st.markdown(f"보유 자기주식: **{dart_tres['hold']:,.0f}주**")

            if not industry_compare_df.empty:
                st.markdown("### 👯 동일업종 비교")
                html_compare = industry_compare_df.to_html(index=False, border=0, classes='scroll-table-content', escape=False)
                st.markdown(f'<div class="scroll-table">{html_compare}</div>', unsafe_allow_html=True)

            st.divider()
            st.markdown("### 💰 S-RIM 적정주가 분석")
            capm_text = f" (CAPM 산출 적용)" if use_capm else ""
            st.caption(f"요구수익률 {required_return:.2f}% 적용{capm_text}")

            def show_srim_result(title, bps, roe_used, label_roe, roe_list=None):
                val = calculate_srim(bps, roe_used, required_return)
                excess_rate = roe_used - required_return
                st.markdown(f"#### {title}")
                if val > 0 and curr_price > 0:
                    diff_rate, diff_abs = (curr_price - val) / val * 100, abs((curr_price - val) / val * 100)
                    if val > curr_price: st.success(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 저평가** 상태입니다.")
                    else: st.error(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 고평가** 상태입니다.")
                
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("*핵심 변수*")
                    st.table(pd.DataFrame({"구분": ["BPS", f"적용 ROE ({label_roe})"], "값": [f"{bps:,.0f} 원", f"{roe_used:.2f} %"]}))
                with c2:
                    st.markdown("*ROE 내역*")
                    if roe_list:
                        roe_df = pd.DataFrame(roe_list)
                        roe_df['ROE'] = roe_df['ROE'].apply(lambda x: f"{x:.2f} %")
                        st.table(roe_df)
                
                if roe_list:
                    vol = roe_volatility([r['ROE'] for r in roe_list])
                    if vol: st.markdown(f"**📐 ROE 변동성**: 평균 {vol['mean']:.2f}% · 변동계수(CV) {vol['cv']:.0f}% → **{vol['grade']}**")
                
                w_rows = []
                for w in [1.0, 0.8, 0.0]:
                    vw = calculate_srim_w(bps, roe_used, required_return, w)
                    w_rows.append({"지속계수 w": f"{w:.1f}", "적정주가(원)": f"{vw:,.0f}", "현재가 괴리": f"{(curr_price - vw)/vw*100:+.1f}%" if vw > 0 and curr_price > 0 else "-"})
                st.markdown("*지속계수(w) 시나리오*")
                st.table(pd.DataFrame(w_rows))

            if annual_list:
                bps_annual = annual_list[-1].get('bps', 0)
                roe_history_annual = [{'연도': d['date'], 'ROE': d['roe']} for d in annual_list if d.get('roe')]
                roe_history_annual_3yr = roe_history_annual[-3:]
                avg_roe_annual = sum([r['ROE'] for r in roe_history_annual_3yr]) / len(roe_history_annual_3yr) if roe_history_annual_3yr else 0
                show_srim_result("1. 최근 3년 실적 평균 기준 (연간)", bps_annual, avg_roe_annual, "3년 평균", roe_history_annual_3yr)
            
            st.divider()

            if quarter_list:
                bps_quarter = quarter_list[-1].get('bps', 0)
                roe_history_quarter = [{'분기': d['date'], 'ROE': d['roe']} for d in quarter_list if d.get('roe')]
                roe_history_quarter_3q = roe_history_quarter[-3:]
                avg_roe_quarter = sum([r['ROE'] for r in roe_history_quarter_3q]) / len(roe_history_quarter_3q) if roe_history_quarter_3q else 0
                show_srim_result("2. 최근 3분기 실적 평균 기준 (분기)", bps_quarter, avg_roe_quarter, "3분기 평균", roe_history_quarter_3q)

            st.divider()
            st.markdown("### 📰 최근 공시 (네이버 종목 공시 탭)")
            if disclosures:
                disc_df = pd.DataFrame(disclosures)[["date", "title"]]; disc_df.columns = ["날짜", "제목"]
                st.table(disc_df)

        except Exception as e:
            st.error(f"오류 발생: {e}")

if __name__ == "__main__":
    main()
