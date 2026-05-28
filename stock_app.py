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
    if not out["annual"] and out["error"] is None: out["error"] = "CFO 행을 찾지 못함 (
