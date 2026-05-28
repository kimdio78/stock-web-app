import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import urllib3
import FinanceDataReader as fdr
import time
import re
import io
import zipfile
import xml.etree.ElementTree as ET

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# [v3] 한국 표준시 — Streamlit Cloud는 UTC로 구동되므로 명시적으로 KST 변환
KST = timezone(timedelta(hours=9))


# =====================================================================
# [v3 신설] 메타/보조 유틸 — 토큰 절감용 (타임스탬프, 유동성, 매크로)
# =====================================================================
def get_data_timestamp():
    """크롤링(데이터 기준) 시각과 장 세션 상태를 KST로 반환.
    Claude가 'PDF가 당일 최신인지'를 판단해 시세·수급 재검색을 생략하는 근거."""
    now = datetime.now(KST)
    wd = now.weekday()  # 0=월 ... 6=일
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
    """'10,270' / '1.29 %' 같은 문자열 → float (실패 시 None)"""
    if text is None:
        return None
    try:
        m = re.search(r'-?\d[\d,]*\.?\d*', str(text))
        if not m:
            return None
        return float(m.group().replace(',', ''))
    except Exception:
        return None


def get_liquidity_and_band(ticker, high_52=None, low_52=None, curr_price=0):
    """[v3 §8.3] 20일 평균 거래대금(ADTV), 거래량 비율, 52주 밴드 내 위치.
    거래대금은 종가×거래량 근사(원). FinanceDataReader 사용."""
    out = {"adtv": None, "vol_avg20": None, "vol_today": None,
           "vol_ratio": None, "band_pos": None}
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=45)
        df = fdr.DataReader(ticker, start, end)
        if df is not None and not df.empty:
            df = df.tail(20)
            tv = (df['Close'] * df['Volume'])           # 근사 거래대금(원)
            out["adtv"] = float(tv.mean())
            out["vol_avg20"] = float(df['Volume'].mean())
            out["vol_today"] = float(df['Volume'].iloc[-1])
            if out["vol_avg20"]:
                out["vol_ratio"] = out["vol_today"] / out["vol_avg20"]
    except Exception:
        pass
    # 52주 밴드 내 위치 (추가 호출 없이 이미 크롤링한 값으로 계산)
    try:
        hi = _to_num(high_52)
        lo = _to_num(low_52)
        if hi and lo and hi > lo and curr_price > 0:
            out["band_pos"] = (curr_price - lo) / (hi - lo) * 100
    except Exception:
        pass
    return out


def _kr10y_from_fdr():
    """국고채 10년물 — FinanceDataReader 심볼 여러 개 순차 시도."""
    symbols = ['KR10YT=RR', 'KR10Y', 'KR10YT', 'KR3YT=RR']  # 마지막은 3년물(최후 보루 아님, 제외용 표시)
    for sym in symbols[:3]:  # 10년물 후보만
        try:
            end = datetime.now(KST).date()
            start = end - timedelta(days=20)
            df = fdr.DataReader(sym, start, end)
            if df is not None and not df.empty:
                col = 'Close' if 'Close' in df.columns else df.columns[0]
                val = float(df[col].dropna().iloc[-1])
                if 0 < val < 20:   # 금리 합리성 체크 (%)
                    return val, f"FDR:{sym}"
        except Exception:
            continue
    return None, None


def _kr10y_from_naver():
    """국고채 10년물 — 네이버 시장지표 채권 페이지 크롤링 (키 불필요)."""
    try:
        url = "https://finance.naver.com/marketindex/bondList.naver"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            # '국고채(10년)' 행 탐색
            for tr in soup.select("tr"):
                txt = tr.get_text()
                if "국고채" in txt and "10년" in txt:
                    nums = re.findall(r'\d+\.\d+', txt)
                    for n in nums:
                        v = float(n)
                        if 0 < v < 20:
                            return v, "네이버 시장지표"
    except Exception:
        pass
    return None, None


def _kr10y_from_ecos(ecos_key):
    """국고채 10년물 — 한국은행 ECOS API (인증키 있을 때만, 가장 안정적)."""
    if not ecos_key:
        return None, None
    try:
        end = datetime.now(KST)
        start = end - timedelta(days=30)
        s = start.strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")
        # 통계표 817Y002(시장금리), 국고채(10년) item code 010210000
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{ecos_key}/json/kr/1/100/"
               f"817Y002/D/{s}/{e}/010210000")
        j = requests.get(url, timeout=12).json()
        rows = j.get("StatisticSearch", {}).get("row", [])
        vals = [float(r["DATA_VALUE"]) for r in rows if r.get("DATA_VALUE")]
        if vals:
            return vals[-1], "한국은행 ECOS"
    except Exception:
        pass
    return None, None


def get_macro_indicators(ecos_key=""):
    """[v3 §4.A/§5.3] USD/KRW + 국고채 10년물(다중 폴백).
    [v3.6] 국고채는 FDR→네이버→ECOS 순으로 시도해 확보율 개선."""
    out = {"usdkrw": None, "kr10y": None, "kr10y_src": None}
    # USD/KRW
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=10)
        fx = fdr.DataReader('USD/KRW', start, end)
        if fx is not None and not fx.empty:
            out["usdkrw"] = float(fx['Close'].iloc[-1])
    except Exception:
        pass
    # 국고채 10년물 — 폴백 체인 (FDR → 네이버 → ECOS)
    val, src = _kr10y_from_fdr()
    if val is None:
        val, src = _kr10y_from_naver()
    if val is None:
        val, src = _kr10y_from_ecos(ecos_key)
    out["kr10y"] = val
    out["kr10y_src"] = src
    return out


# =====================================================================
# [v3 추가] 네이버 모바일 API (JSON) + 베타/공시/업종 — 추가 크롤링 항목
#   ① 컨센서스 목표가·투자의견  ② 영업현금흐름(CFO)
#   ③ 베타  ④ 최근 공시  ⑤ 업종·시총순위
#  ※ ①②는 m.stock.naver API 키 구조 미검증(빌드환경 접근차단) → 방어적 파서 +
#    화면 하단 '디버그' 확장창에 원본 JSON을 띄워 실제 키를 확인/수정할 수 있게 함.
# =====================================================================
MOBILE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
    'Referer': 'https://m.stock.naver.com/',
}


def _fetch_json(url):
    try:
        r = requests.get(url, headers=MOBILE_HEADERS, verify=False, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _deep_find(obj, key_substrings, results=None, path="", capture_all=False):
    """JSON을 재귀 순회하며 키에 부분문자열이 포함된 (path, value)를 수집(스칼라).
    매칭된 키의 값이 dict/list면 그 하위 스칼라도 모두 수집(capture_all)."""
    if results is None:
        results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            matched = any(s.lower() in kl for s in key_substrings)
            newpath = f"{path}.{k}"
            if isinstance(v, (dict, list)):
                _deep_find(v, key_substrings, results, newpath, capture_all or matched)
            elif matched or capture_all:
                results.append((newpath, v))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _deep_find(item, key_substrings, results, f"{path}[{i}]", capture_all)
    return results


def _recomm_label(score):
    """[v3] FnGuide 5점 척도(5=강력매수 ~ 1=강력매도) → 한글 라벨.
    공식 문서로 확정된 척도는 아니므로 업계 통용 해석으로 표시, 원본 점수도 병기."""
    if score is None:
        return None
    s = float(score)
    if s >= 4.5: return "강력매수"
    if s >= 3.5: return "매수"
    if s >= 2.5: return "중립"
    if s >= 1.5: return "매도"
    return "강력매도"


def get_naver_mobile_consensus(ticker):
    """[① §4.H 확률앵커/§7.2] 네이버 모바일 API 컨센서스.
    실제 확인된 키 구조 (디버그 패널로 검증, 005930 기준):
      consensusInfo.priceTargetMean   = 목표가 평균
      consensusInfo.priceTargetHigh   = 목표가 최고 (있을 때만, fallback)
      consensusInfo.priceTargetLow    = 목표가 최저 (있을 때만, fallback)
      consensusInfo.recommMean        = 투자의견 점수(5점 척도)
      consensusInfo.createDate        = 컨센서스 생성일
    """
    out = {"target_avg": None, "target_high": None, "target_low": None,
           "opinion": None, "opinion_score": None, "create_date": None,
           "raw": None, "found": []}
    data = _fetch_json(f"https://m.stock.naver.com/api/stock/{ticker}/integration")
    if data is None:
        data = _fetch_json(f"https://m.stock.naver.com/api/stock/{ticker}/basic")
    if data is None:
        return out
    out["raw"] = data

    # 1) 확인된 키 직접 조회 (consensusInfo 객체 위치 탐색)
    def _find_consensus_node(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k).lower() == "consensusinfo" and isinstance(v, dict):
                    return v
                r = _find_consensus_node(v)
                if r is not None:
                    return r
        elif isinstance(o, list):
            for item in o:
                r = _find_consensus_node(item)
                if r is not None:
                    return r
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
        if cnode.get("createDate"):
            out["create_date"] = str(cnode["createDate"])

    # 2) 디버그용 후보 목록 (키 변경 시 사용자가 확인 가능)
    out["found"] = _deep_find(data, ["target", "목표", "consensus", "컨센", "opinion",
                                     "투자의견", "recomm", "pricetarget", "estimateprice"])[:50]

    # 3) Fallback: consensusInfo가 없는 경우 휴리스틱 (방어적)
    if cnode is None:
        for p, v in out["found"]:
            leaf = p.rsplit(".", 1)[-1].lower()
            num = _to_num(v)
            if num is None:
                continue
            if "pricetargetmean" in leaf and out["target_avg"] is None:
                out["target_avg"] = num
            elif "pricetargethigh" in leaf and out["target_high"] is None:
                out["target_high"] = num
            elif "pricetargetlow" in leaf and out["target_low"] is None:
                out["target_low"] = num
            elif "recommmean" in leaf and out["opinion_score"] is None:
                out["opinion_score"] = num
                out["opinion"] = _recomm_label(num)

    return out


def _build_header_map(data):
    """[v3.3] 재무 API의 날짜 헤더 리스트(trTitleList 등)에서 {컬럼키: 날짜라벨} 맵 생성.
    네이버 구조: rowList의 columns 키가 헤더의 key/id와 매칭됨."""
    header_map = {}

    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                np = f"{path}.{k}" if path else k
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    if "title" in np.lower():
                        for idx, item in enumerate(v):
                            if not isinstance(item, dict):
                                continue
                            ttl = item.get("title") or item.get("name")
                            key = item.get("key") or item.get("id") or item.get("code")
                            if ttl is None:
                                continue
                            # 키가 있으면 키로, 없으면 순번(문자)으로 매핑
                            if key is not None:
                                header_map[str(key)] = str(ttl)
                            header_map[str(idx)] = str(ttl)  # 순번 fallback
                walk(v, np)
        elif isinstance(o, list):
            for it in o:
                walk(it, path)

    walk(data)
    return header_map


def _extract_finance_rows(data, title_must_include, title_must_exclude=(), header_map=None):
    """[v3.3 강화] JSON 트리 전체를 순회하며 title-like 키를 가진 dict를 행 후보로 채집.
    columns 키가 날짜가 아니라 컬럼식별자(c1/0/...)인 경우 header_map으로 날짜 복원."""
    if header_map is None:
        header_map = _build_header_map(data)

    title_keys = ("title", "krNm", "krName", "name", "accountNm", "acctNm",
                  "itemNm", "acctCd", "label")
    value_keys_for_cell = ("value", "amount", "val", "v", "data")
    # YYYY.MM / YYYY-MM / YYYY / YYYYMM(6) / YYYYMMDD(8) 모두 인식
    date_pattern = re.compile(r'^(19|20)\d{2}([.\-/]?\d{2}([.\-/]?\d{2})?)?$')

    def looks_like_date(s):
        s = str(s).strip()
        return bool(date_pattern.match(s))

    def normalize_date(s):
        """다양한 날짜 표기를 'YYYY.MM'으로 통일. 실패 시 원본 반환."""
        ks = str(s).strip()
        # 구분자 없는 숫자열 (202312, 20231231, 2023)
        digits = re.sub(r'[^\d]', '', ks)
        if re.fullmatch(r'(19|20)\d{2}\d{2}\d{2}', digits):      # YYYYMMDD
            return f"{digits[:4]}.{digits[4:6]}"
        if re.fullmatch(r'(19|20)\d{2}\d{2}', digits):           # YYYYMM
            return f"{digits[:4]}.{digits[4:6]}"
        if re.fullmatch(r'(19|20)\d{2}', digits) and digits == ks:  # YYYY
            return digits
        # 이미 구분자 있는 형태 (2023.12, 2023-12-31)
        m = re.match(r'^((19|20)\d{2})[.\-/](\d{1,2})', ks)
        if m:
            return f"{m.group(1)}.{int(m.group(3)):02d}"
        return ks

    def resolve_date(colkey):
        """컬럼키 → 실제 날짜 라벨. header_map 우선, 그다음 날짜 정규화."""
        ks = str(colkey)
        if ks in header_map:          # 헤더 매핑 우선 (식별자 → 날짜)
            return normalize_date(header_map[ks])
        if looks_like_date(ks):       # 컬럼키 자체가 날짜 (202312 등)
            return normalize_date(ks)
        return ks                     # 못 찾으면 원본 유지

    def get_title(row):
        for tk in title_keys:
            v = row.get(tk)
            if isinstance(v, str) and v.strip():
                return v
        return ""

    def extract_values_from_row(row):
        results = []
        for cont_k in ("columns", "values", "items", "data", "history", "cols"):
            cont = row.get(cont_k)
            if isinstance(cont, dict):
                for k, v in cont.items():
                    num = None
                    if isinstance(v, dict):
                        for vk in value_keys_for_cell:
                            if vk in v:
                                num = _to_num(v[vk])
                                break
                    else:
                        num = _to_num(v)
                    if num is not None:
                        results.append({"date": resolve_date(k), "value": num})
                if results:
                    return results
            elif isinstance(cont, list):
                # [{date,value}] 또는 [{value}] 순번 리스트
                for idx, item in enumerate(cont):
                    if not isinstance(item, dict):
                        # 순수 스칼라 리스트 → 순번으로 헤더 매핑
                        num = _to_num(item)
                        if num is not None:
                            results.append({"date": resolve_date(idx), "value": num})
                        continue
                    date_val = None
                    for dk in ("date", "term", "period", "yyyymm", "yyyymmdd", "key", "title"):
                        if dk in item:
                            date_val = resolve_date(item[dk])
                            break
                    if date_val is None:
                        date_val = resolve_date(idx)
                    num = None
                    for vk in value_keys_for_cell:
                        if vk in item:
                            num = _to_num(item[vk])
                            if num is not None:
                                break
                    if num is not None:
                        results.append({"date": date_val, "value": num})
                if results:
                    return results
        # 평탄 {연도:값}
        for k, v in row.items():
            if k in title_keys or isinstance(v, (dict, list)):
                continue
            if looks_like_date(k):
                num = _to_num(v)
                if num is not None:
                    results.append({"date": normalize_date(k), "value": num})
        return results

    candidates = []

    def walk(o):
        if isinstance(o, list):
            if o and all(isinstance(x, dict) for x in o):
                candidates.append(o)
            for it in o:
                walk(it)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)

    walk(data)

    for rowlist in candidates:
        for row in rowlist:
            title = get_title(row).replace(" ", "")
            if not title:
                continue
            if title_must_include and title_must_include not in title:
                continue
            if any(x in title for x in title_must_exclude):
                continue
            vals = extract_values_from_row(row)
            if vals:
                return vals
    return []


def _diagnose_finance_json(data, max_titles=40):
    """[v3.2] 추출 실패 시 디버그용 — financeInfo 내부 키/리스트 길이/타이틀 후보 요약.
    [v3.3] columns 내부 구조와 헤더(trTitleList) 매핑까지 노출."""
    summary = {"top_keys": [], "list_locations": [], "title_candidates": [],
               "sample_columns": None, "header_map": None}
    if not isinstance(data, dict):
        return summary
    summary["top_keys"] = list(data.keys())

    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                np = f"{path}.{k}" if path else k
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    summary["list_locations"].append({"path": np, "length": len(v)})
                    sample = v[0]
                    sample_keys = list(sample.keys())[:10]
                    title = None
                    for tk in ("title", "krNm", "krName", "name", "accountNm",
                               "acctNm", "itemNm", "label"):
                        if tk in sample and isinstance(sample[tk], str):
                            title = sample[tk]
                            break
                    for item in v[:max_titles]:
                        if not isinstance(item, dict):
                            continue
                        for tk in ("title", "krNm", "krName", "name", "accountNm",
                                   "acctNm", "itemNm", "label"):
                            if tk in item and isinstance(item[tk], str):
                                summary["title_candidates"].append(
                                    {"path": np, "key": tk, "title": item[tk]}
                                )
                                break
                    summary["list_locations"][-1]["sample_keys"] = sample_keys
                    summary["list_locations"][-1]["sample_title"] = title
                    # [v3.3] 데이터 행으로 보이는 리스트의 columns 구조 1개 노출
                    if "columns" in sample and summary["sample_columns"] is None:
                        col = sample["columns"]
                        if isinstance(col, dict):
                            preview = {}
                            for ck, cv in list(col.items())[:6]:
                                preview[ck] = cv
                            summary["sample_columns"] = {"path": np, "preview": preview}
                    # [v3.3] 헤더 리스트(날짜 매핑) 추출: key→title
                    if "Title" in np or "title" in np.lower():
                        hm = {}
                        for item in v:
                            if isinstance(item, dict):
                                key = item.get("key") or item.get("id") or item.get("code")
                                ttl = item.get("title") or item.get("name")
                                if key is not None and ttl is not None:
                                    hm[str(key)] = str(ttl)
                        if hm:
                            summary["header_map"] = {"path": np, "map": hm}
                walk(v, np)
        elif isinstance(o, list):
            for i, it in enumerate(o):
                walk(it, f"{path}[{i}]")

    walk(data)
    return summary


# =====================================================================
# [v3.5] DART 오픈API — 현금흐름표(CFO) 정식 수집
#  네이버 모바일 API에 현금흐름표가 없어 DART로 대체. 키는 사이드바 입력.
# =====================================================================
@st.cache_data(ttl=86400)
def dart_load_corpcode_map(api_key):
    """종목코드(6자리) → DART 고유번호(8자리) 매핑. corpCode.xml(zip) 1회 다운로드 후 캐시."""
    try:
        url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={api_key}"
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return {}
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml_name = zf.namelist()[0]
        root = ET.fromstring(zf.read(xml_name).decode("utf-8"))
        mapping = {}
        for item in root.iter("list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_code = (item.findtext("corp_code") or "").strip()
            if stock_code and corp_code and stock_code != " ":
                mapping[stock_code] = corp_code
        return mapping
    except Exception:
        return {}


def _dart_amount(row, key):
    """DART 금액 필드 파싱 (원 단위). 빈값/'-' 처리."""
    v = row.get(key, "")
    if v in ("", "-", None):
        return None
    return _to_num(v)


def dart_get_cashflow(api_key, corp_code, years=3):
    """[② §3.5/§3.7] DART fnlttSinglAcntAll에서 영업활동현금흐름(CFO) 연간 시계열.
    최신 사업연도부터 역순으로 조회, 각 응답의 당기/전기/전전기 금액을 활용."""
    out = {"annual": [], "source": None, "raw_sample": None, "error": None}
    if not api_key or not corp_code:
        out["error"] = "API 키 또는 corp_code 없음"
        return out

    this_year = datetime.now(KST).year
    cfo_by_year = {}  # {연도(int): 금액(원)}
    raw_sample = None

    # 최근 사업연도 후보 (확정실적 지연 감안해 작년부터)
    for by in range(this_year - 1, this_year - 1 - years - 1, -1):
        if len([y for y in cfo_by_year if y is not None]) >= years:
            break
        data = None
        for fs_div in ("CFS", "OFS"):   # 연결 우선, 없으면 별도
            url = (f"https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
                   f"?crtfc_key={api_key}&corp_code={corp_code}"
                   f"&bsns_year={by}&reprt_code=11011&fs_div={fs_div}")
            try:
                resp = requests.get(url, timeout=15)
                j = resp.json()
            except Exception:
                continue
            if j.get("status") == "013":   # 데이터 없음
                continue
            if j.get("list"):
                data = j["list"]
                out["source"] = f"DART {fs_div} (사업보고서 11011)"
                break
        if not data:
            continue
        if raw_sample is None:
            # CF 항목 일부를 디버그 샘플로 보존
            raw_sample = [r for r in data if r.get("sj_div") == "CF"][:8]

        # 영업활동현금흐름 행 탐색
        for row in data:
            if row.get("sj_div") != "CF":
                continue
            nm = (row.get("account_nm") or "").replace(" ", "")
            if "영업활동" in nm and not any(x in nm for x in ("투자활동", "재무활동")):
                # 당기/전기/전전기 = by / by-1 / by-2
                amt_t = _dart_amount(row, "thstrm_amount")
                amt_f = _dart_amount(row, "frmtrm_amount")
                amt_b = _dart_amount(row, "bfefrmtrm_amount")
                if amt_t is not None:
                    cfo_by_year[by] = amt_t
                if amt_f is not None:
                    cfo_by_year.setdefault(by - 1, amt_f)
                if amt_b is not None:
                    cfo_by_year.setdefault(by - 2, amt_b)
                break
        if cfo_by_year:
            break   # 한 해 응답에 당기/전기/전전기가 다 들어있어 보통 1회로 충분

    out["raw_sample"] = raw_sample
    # 정렬된 시계열로 변환 (억원 단위로 환산: DART는 원 단위)
    for y in sorted(cfo_by_year.keys())[-years:]:
        val_won = cfo_by_year[y]
        out["annual"].append({"date": f"{y}.12", "value": val_won / 1e8})  # 억원
    if not out["annual"] and out["error"] is None:
        out["error"] = "CFO 행을 찾지 못함 (CF 항목 확인 필요)"
    return out


def _dart_fetch_report(api_key, endpoint, corp_code, prefer_years=2):
    """[v3.6] DART 정기보고서 주요정보 공통 호출. 최근 사업연도부터 시도, list 반환."""
    this_year = datetime.now(KST).year
    for by in range(this_year - 1, this_year - 1 - prefer_years - 1, -1):
        url = (f"https://opendart.fss.or.kr/api/{endpoint}.json"
               f"?crtfc_key={api_key}&corp_code={corp_code}"
               f"&bsns_year={by}&reprt_code=11011")
        try:
            j = requests.get(url, timeout=15).json()
        except Exception:
            continue
        if j.get("status") == "013":
            continue
        if j.get("list"):
            return j["list"], by
    return [], None


def dart_get_shareholders(api_key, corp_code):
    """[① §5.1] 최대주주현황 — 오너 일가 지분율·특수관계인 합계."""
    out = {"top": None, "rows": [], "total_rate": None, "year": None, "error": None, "raw": None}
    if not api_key or not corp_code:
        out["error"] = "키/코드 없음"
        return out
    rows, by = _dart_fetch_report(api_key, "hyslrSttus", corp_code)
    out["year"] = by
    out["raw"] = rows[:10]
    if not rows:
        out["error"] = "최대주주 데이터 없음"
        return out
    parsed = []
    for r in rows:
        nm = r.get("nm")
        rate = _to_num(r.get("trmend_posesn_stock_qota_rt")) or _to_num(r.get("bsis_posesn_stock_qota_rt"))
        relate = r.get("relate")
        if nm and rate is not None:
            parsed.append({"이름": nm, "관계": relate or "-", "지분율": rate})
    # '계' 행이 특수관계인 합계 (관계가 '계'/'소계'/None인 큰 값)
    total = None
    for r in parsed:
        if r["이름"] and any(x in str(r["이름"]) for x in ("계", "합계")):
            total = r["지분율"]
    if total is None and parsed:
        # 합계행이 없으면 개별 지분율 합산 근사
        total = sum(p["지분율"] for p in parsed if not any(x in str(p["이름"]) for x in ("계", "합계")))
    # 최대주주(최상위 개별)
    indiv = [p for p in parsed if not any(x in str(p["이름"]) for x in ("계", "합계"))]
    if indiv:
        out["top"] = max(indiv, key=lambda x: x["지분율"])
    out["rows"] = parsed[:8]
    out["total_rate"] = total
    return out


def dart_get_dividend(api_key, corp_code):
    """[② §5.2] 배당에 관한 사항 — 주당배당금·배당성향·배당수익률 (당기/전기/전전기)."""
    out = {"items": {}, "year": None, "error": None, "raw": None}
    if not api_key or not corp_code:
        out["error"] = "키/코드 없음"
        return out
    rows, by = _dart_fetch_report(api_key, "alotMatter", corp_code)
    out["year"] = by
    out["raw"] = rows[:20]
    if not rows:
        out["error"] = "배당 데이터 없음"
        return out
    # se(항목명)별로 당기/전기/전전기 값 매핑
    wanted = {
        "주당 현금배당금": "DPS", "주당현금배당금": "DPS",
        "현금배당성향": "배당성향", "배당성향": "배당성향",
        "현금배당수익률": "배당수익률", "배당수익률": "배당수익률",
    }
    for r in rows:
        se = (r.get("se") or "").replace(" ", "")
        for k, label in wanted.items():
            if k.replace(" ", "") in se:
                vals = {
                    "당기": _to_num(r.get("thstrm")),
                    "전기": _to_num(r.get("frmtrm")),
                    "전전기": _to_num(r.get("lwfr")),
                }
                # 보통주 기준 행 우선 (우선주 행 중복 시 첫 유효값 유지)
                if label not in out["items"] or all(v is None for v in out["items"][label].values()):
                    out["items"][label] = vals
                break
    if not out["items"]:
        out["error"] = "배당 항목 파싱 실패 (raw 확인)"
    return out


def dart_get_treasury(api_key, corp_code):
    """[③ §5.2] 자기주식 취득·처분·보유 현황 (보유 수량 중심)."""
    out = {"hold": None, "rows": [], "year": None, "error": None, "raw": None}
    if not api_key or not corp_code:
        out["error"] = "키/코드 없음"
        return out
    rows, by = _dart_fetch_report(api_key, "tesstkAcqsDspsSttus", corp_code)
    out["year"] = by
    out["raw"] = rows[:15]
    if not rows:
        out["error"] = "자기주식 데이터 없음 (보유 없음일 수 있음)"
        return out
    # 보유 잔량 행 탐색 (se/acqs_mth 등에 '보유' 또는 기말잔고)
    for r in rows:
        label = " ".join(str(r.get(k, "")) for k in ("acqs_mth1", "acqs_mth2", "acqs_mth3", "se"))
        qty = _to_num(r.get("trmend_qy")) or _to_num(r.get("hold_qy"))
        if qty is not None:
            out["rows"].append({"구분": label.strip() or "-", "수량": qty})
    # 합계/보유 행
    for r in out["rows"]:
        if any(x in r["구분"] for x in ("보유", "총계", "합계", "계")):
            out["hold"] = r["수량"]
    if out["hold"] is None and out["rows"]:
        out["hold"] = out["rows"][-1]["수량"]
    return out



def get_recent_disclosures(ticker, limit=10):
    """[④ §7.1] 최근 공시 제목·날짜 (네이버 종목 공시 탭). 상세는 DART에서 검증."""
    try:
        url = f"https://finance.naver.com/item/news_notice.naver?code={ticker}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=10)
        items = []
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            for tr in soup.select("table tr"):
                a = tr.select_one("a")
                tds = tr.select("td")
                if a and tds:
                    title = re.sub(r'\s+', ' ', a.text.strip())
                    date = tds[-1].text.strip()
                    if title and not title.startswith("연관기사"):
                        items.append({"title": title, "date": date})
                if len(items) >= limit:
                    break
        return items
    except Exception:
        return []


def get_beta(ticker):
    """[③ §4.A] KOSPI(KS11) 대비 약 5년 월간수익률 베타 (FinanceDataReader)."""
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=365 * 5 + 30)
        s = fdr.DataReader(ticker, start, end)['Close'].resample('M').last()
        m = fdr.DataReader('KS11', start, end)['Close'].resample('M').last()
        df = pd.concat([s, m], axis=1, keys=['s', 'm']).dropna()
        rs = df['s'].pct_change().dropna()
        rm = df['m'].pct_change().dropna()
        common = rs.index.intersection(rm.index)
        rs, rm = rs.loc[common], rm.loc[common]
        if len(rs) < 24:
            return None
        var = ((rm - rm.mean()) ** 2).mean()
        if var == 0:
            return None
        cov = ((rs - rs.mean()) * (rm - rm.mean())).mean()
        return float(cov / var)
    except Exception:
        return None


@st.cache_data(ttl=3600)
def load_listing_df():
    """[⑤] 업종/시총순위 산출용 KRX 상장 목록 (컬럼 가용 시에만 사용)."""
    try:
        return fdr.StockListing('KRX')
    except Exception:
        return pd.DataFrame()


def get_sector_and_rank(ticker, listing_df):
    """[⑤ §3.1/§3.2/§4.G] 업종(Sector)·시장·시총순위. fdr 컬럼 가용성에 따라 best-effort."""
    out = {"sector": None, "industry": None, "market": None, "marcap_rank": None}
    try:
        if listing_df is None or listing_df.empty:
            return out
        df = listing_df
        code_col = 'Code' if 'Code' in df.columns else df.columns[0]
        row = df[df[code_col] == ticker]
        if row.empty:
            return out
        r0 = row.iloc[0]
        for c in ['Sector', 'sector', '업종']:
            if c in df.columns:
                out["sector"] = r0.get(c)
                break
        for c in ['Industry', 'industry']:
            if c in df.columns:
                out["industry"] = r0.get(c)
                break
        for c in ['Market', 'market']:
            if c in df.columns:
                out["market"] = r0.get(c)
                break
        if 'Marcap' in df.columns and out["market"] and 'Market' in df.columns:
            sub = df[df['Market'] == out["market"]].sort_values('Marcap', ascending=False).reset_index(drop=True)
            pos = sub.index[sub[code_col] == ticker].tolist()
            if pos:
                out["marcap_rank"] = int(pos[0]) + 1
    except Exception:
        pass
    return out


# --- 데이터 수집 함수들 ---
@st.cache_data(ttl=3600)
def load_stock_data():
    try:
        df = fdr.StockListing('KRX')
        if not df.empty:
            df['Search_Key'] = df['Name'] + " (" + df['Code'] + ")"
            search_map = dict(zip(df['Search_Key'], df['Code']))
            ticker_to_name = dict(zip(df['Code'], df['Name']))
            search_list = list(search_map.keys())
            return search_list, search_map, ticker_to_name
    except:
        pass
    return [], {}, {}

def get_naver_stock_details(ticker):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        
        data = {
            'name': ticker, 'overview': "정보 없음", 
            'now_price': '0', 'diff_rate': '0.00', 'diff_amount': '0', 'direction': 'flat',
            'market_cap': '-', 'shares': 0, 'foreign_rate': '-', 
            'per': '-', 'eps': '-', 'pbr': '-', 'bps': '-', 'dvr': '-',
            'high_52': '-', 'low_52': '-'
        }
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            name_tag = soup.select_one(".wrap_company h2 a")
            if name_tag: data['name'] = name_tag.text.strip()

            overview_div = soup.select_one("#summary_info")
            if overview_div:
                data['overview'] = "\n ".join([p.text.strip() for p in overview_div.select("p") if p.text.strip()])

            try:
                now_tag = soup.select_one(".no_today .blind")
                if now_tag: data['now_price'] = now_tag.text.strip()
                
                exday_tag = soup.select_one(".no_exday")
                if exday_tag:
                    spans = exday_tag.select("span.blind")
                    if len(spans) >= 2:
                        data['diff_amount'] = spans[0].text.strip()
                        data['diff_rate'] = spans[1].text.strip()
                    
                    if exday_tag.select_one(".ico.up"): data['direction'] = 'up'
                    elif exday_tag.select_one(".ico.down"): data['direction'] = 'down'
                    elif exday_tag.select_one(".ico.upper"): data['direction'] = 'upper'
                    elif exday_tag.select_one(".ico.lower"): data['direction'] = 'lower'
            except: pass

            try:
                mc_element = soup.select_one("#_market_sum")
                if mc_element:
                    data['market_cap'] = mc_element.text.strip().replace('\t', '').replace('\n', '') + " 억원"
            except: pass

            try:
                first_table = soup.select_one("div.first table")
                if first_table:
                    for tr in first_table.select("tr"):
                        if "상장주식수" in tr.text:
                            em = tr.select_one("em")
                            if em:
                                shares_str = em.text.strip().replace(',', '')
                                data['shares'] = int(shares_str)
                            break
            except: pass

            try:
                per_el = soup.select_one("#_per")
                if per_el: data['per'] = per_el.text.strip()
                eps_el = soup.select_one("#_eps")
                if eps_el: data['eps'] = eps_el.text.strip()
                pbr_el = soup.select_one("#_pbr")
                if pbr_el: data['pbr'] = pbr_el.text.strip()
                dvr_el = soup.select_one("#_dvr")
                if dvr_el: data['dvr'] = dvr_el.text.strip()
            except: pass

            all_ths = soup.select("th")
            for th in all_ths:
                th_text = th.text.strip()
                if "외국인소진율" in th_text:
                    td = th.find_next_sibling("td")
                    if td:
                        em = td.select_one("em")
                        data['foreign_rate'] = em.text.strip() if em else td.text.strip()
                elif "52주최고" in th_text:
                    td = th.find_next_sibling("td")
                    if td:
                        ems = td.select("em")
                        if len(ems) >= 2:
                            data['high_52'] = ems[0].text.strip()
                            data['low_52'] = ems[1].text.strip()
                elif "BPS" in th_text and "PBR" not in th_text:
                    td = th.find_next_sibling("td")
                    if td:
                        em = td.select_one("em")
                        data['bps'] = em.text.strip() if em else td.text.strip()
            
            if data['bps'] == '-':
                try:
                    per_table = soup.select_one("table.per_table")
                    if per_table:
                        rows = per_table.select("tr")
                        for r in rows:
                            if "BPS" in r.text:
                                ems = r.select("em")
                                if len(ems) >= 2: data['bps'] = ems[1].text.strip()
                                elif len(ems) == 1: data['bps'] = ems[0].text.strip()
                except: pass

        return data
    except:
        return {'name': ticker, 'overview': "로딩 실패", 'shares': 0}

def get_investor_trend(ticker):
    try:
        url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False)
        trends = []
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            tables = soup.select("table.type2")
            if len(tables) >= 2:
                target_table = tables[1]
                rows = target_table.select("tr")
                for row in rows:
                    cols = row.select("td")
                    if len(cols) == 9:
                        date = cols[0].text.strip()
                        close = cols[1].text.strip()
                        rate = cols[3].text.strip().replace('\n', '').replace('\t', '')
                        inst_net = cols[5].text.strip()
                        frgn_net = cols[6].text.strip()
                        hold_rate = cols[8].text.strip()
                        trends.append({"날짜": date, "종가": close, "등락률": rate, "기관": inst_net, "외국인": frgn_net, "보유율": hold_rate})
                        if len(trends) >= 10: break
        return trends
    except:
        return []

def get_same_industry_comparison(ticker):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            compare_section = soup.select_one("div.section.trade_compare")
            if compare_section:
                table = compare_section.select_one("table")
                if table:
                    headers = ["구분"]
                    thead = table.select_one("thead")
                    for th in thead.select("th"):
                        if th.find("a"):
                            raw_header = th.text.strip()
                            clean_header = raw_header.split('*')[0].strip()
                            headers.append(clean_header)
                    
                    rows_data = []
                    tbody = table.select_one("tbody")
                    for tr in tbody.select("tr"):
                        row_val = []
                        th_item = tr.select_one("th")
                        row_title = ""
                        if th_item:
                            row_title = th_item.text.strip()
                            row_val.append(row_title)
                        
                        for td in tr.select("td"):
                            raw_text = td.text.strip()
                            clean_text = re.sub(r'[\n\t]+', ' ', raw_text)
                            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                            
                            if row_title in ["전일대비", "등락률"]:
                                val_text = re.sub(r'[^0-9.,%]', '', clean_text)
                                if "상향" in clean_text or "상승" in clean_text or "+" in clean_text:
                                    clean_text = f'<span style="color:#d20000">+{val_text}</span>'
                                elif "하향" in clean_text or "하락" in clean_text or "-" in clean_text:
                                    clean_text = f'<span style="color:#0051c7">-{val_text}</span>'
                                elif "보합" in clean_text:
                                    clean_text = val_text
                            
                            row_val.append(clean_text)
                        
                        if len(row_val) == len(headers):
                             rows_data.append(row_val)
                        elif len(row_val) > len(headers):
                             rows_data.append(row_val[:len(headers)])

                    return pd.DataFrame(rows_data, columns=headers)
        return pd.DataFrame()
    except:
        return pd.DataFrame()

def clean_float(text):
    if not text or text.strip() in ['-', 'N/A', '', '.']:
        return 0.0
    try:
        text = text.replace(',', '')
        match = re.search(r'-?\d+\.?\d*', text)
        if match:
            return float(match.group())
        return 0.0
    except:
        return 0.0

def get_financials_from_naver(ticker, current_price=0, shares=0):
    """[v3 개정] 연간/분기 실적 + 컨센서스 추정(E)을 함께 반환.
    기존 버전은 '(E)' 컬럼을 버렸으나, v3 §3.5/§7.2를 위해 estimate_data로 보존."""
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, verify=False)
        soup = BeautifulSoup(response.text, 'html.parser')
        finance_table = soup.select_one("div.section.cop_analysis > div.sub_section > table")
        if not finance_table: return [], [], []

        header_rows = finance_table.select("thead > tr")
        date_cols = [th.text.strip() for th in header_rows[1].select("th")]
        
        annual_idxs = []
        quarter_idxs = []
        estimate_idxs = []   # [v3] 추정(E) 컬럼 보존
        
        for i, col in enumerate(date_cols):
             if "(E)" in col:
                 estimate_idxs.append(i)
             elif i < 4:
                 annual_idxs.append(i)
             else:
                 quarter_idxs.append(i)
        
        annual_idxs = annual_idxs[-3:]
        quarter_idxs = quarter_idxs[-5:]

        annual_data = [{'date': date_cols[i].split('(')[0]} for i in annual_idxs]
        quarter_data = [{'date': date_cols[i].split('(')[0]} for i in quarter_idxs]
        estimate_data = [{'date': date_cols[i].split('(')[0].strip()} for i in estimate_idxs]

        rows = finance_table.select("tbody > tr")
        items_map_main = {
            "매출액": "revenue", "영업이익": "op_income", "당기순이익": "net_income",
            "영업이익률": "op_margin", "순이익률": "net_income_margin", "ROE": "roe",
            "부채비율": "debt_ratio", "당좌비율": "quick_ratio", "유보율": "reserve_ratio",
            "EPS": "eps", "BPS": "bps", "PER": "per", "PBR": "pbr",
            "주당배당금": "dps", "배당성향": "payout_ratio", "시가배당률": "dividend_yield",
        }

        def fill_data(target_list, indices):
            for i, idx in enumerate(indices):
                for row in rows:
                    th_text = row.th.text.strip().replace(" ", "")
                    key = None
                    for k_txt, k_key in items_map_main.items():
                        if k_txt in th_text:
                             if k_txt == "영업이익" and "률" in th_text: continue
                             if k_txt == "당기순이익" and "률" in th_text: continue
                             key = k_key
                             break

                    if key:
                        cells = row.select("td")
                        cell_offset = len(date_cols) - len(cells)
                        t_idx = idx - cell_offset
                        if 0 <= t_idx < len(cells):
                            target_list[i][key] = clean_float(cells[t_idx].text.strip())
                
                rev = target_list[i].get('revenue', 0)
                if rev and shares > 0:
                     sps = (rev * 100000000) / shares
                     target_list[i]['sps'] = sps
                     if current_price > 0: target_list[i]['psr'] = current_price / sps
        
        fill_data(annual_data, annual_idxs)
        fill_data(quarter_data, quarter_idxs)
        fill_data(estimate_data, estimate_idxs)
        
        return annual_data, quarter_data, estimate_data

    except:
        return [], [], []

def calculate_srim(bps, roe, rrr):
    if rrr <= 0: return 0
    excess_profit_rate = (roe - rrr) / 100
    fair_value = bps + (bps * excess_profit_rate / (rrr / 100))
    return fair_value


def calculate_srim_w(bps, roe, rrr, w):
    """[v3 §4.B] 초과이익 지속계수 w 반영 S-RIM.
    적정주가 = BPS + BPS×(ROE-r)×w/(1+r-w).  w=1 → 영구지속(기존 식과 동일)."""
    if rrr <= 0 or bps <= 0:
        return 0
    r = rrr / 100.0
    excess = (roe - rrr) / 100.0
    if w >= 1.0:
        return bps + bps * excess / r
    denom = (1 + r - w)
    if denom <= 0:
        return bps + bps * excess / r
    return bps + bps * excess * w / denom


def roe_volatility(roe_values):
    """[v3 §4.B/§3.5] ROE 평균·표준편차·변동계수(CV) + S-RIM 신뢰도 판정."""
    vals = [v for v in roe_values if v is not None]
    n = len(vals)
    if n == 0:
        return None
    mean = sum(vals) / n
    if n >= 2:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        std = var ** 0.5
    else:
        std = 0.0
    cv = (std / abs(mean) * 100) if mean else None
    if cv is None:
        grade = "판정불가"
    elif cv <= 30:
        grade = "안정 → S-RIM 정상 적용"
    elif cv <= 50:
        grade = "보통"
    else:
        grade = "과대 → S-RIM 참고용(신뢰구간 넓음)"
    return {"mean": mean, "std": std, "cv": cv, "grade": grade}

if 'search_key' not in st.session_state:
    st.session_state.search_key = 0 

def reset_search_state():
    st.session_state.search_key += 1 

# --- 메인 UI ---
def main():
    st.set_page_config(page_title="주식 적정주가 분석기", page_icon="📈")

    # [v3] 인쇄(PDF) 친화 + 넓은 표 깨짐 방지 전역 스타일
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
    .asof-box { background: rgba(3,199,90,0.08); border: 1px solid rgba(3,199,90,0.5);
        border-radius: 8px; padding: 10px 14px; margin: 6px 0 14px 0; font-size: 0.9rem; }
    .asof-box code { font-size: 0.8rem; color: #555; }
    @media (prefers-color-scheme: dark) { .asof-box code { color: #bbb; } }
    </style>
    """, unsafe_allow_html=True)
    
    if 'search_list' not in st.session_state:
        with st.spinner('종목 데이터 로딩 중...'):
            st.session_state.search_list, st.session_state.search_map, st.session_state.ticker_to_name = load_stock_data()
    
    search_list = st.session_state.search_list
    search_map = st.session_state.search_map
    ticker_to_name = st.session_state.ticker_to_name

    with st.sidebar:
        st.header("설정")
        required_return = st.number_input("요구수익률 (%)", 1.0, 20.0, 8.0, 0.5)
        st.divider()
        st.markdown("**DART 오픈API (CFO 수집용)**")

        # [v3.6] Secrets 자동 인식 → 있으면 자동 사용, 없으면 수동 입력
        secret_key = ""
        try:
            secret_key = st.secrets.get("DART_API_KEY", "")
        except Exception:
            secret_key = ""

        if secret_key:
            dart_api_key = secret_key
            st.success("DART 인증키 자동 적용됨 (Secrets)")
        else:
            dart_api_key = st.text_input(
                "DART 인증키", type="password",
                help="opendart.fss.or.kr 에서 무료 발급. Secrets 미설정 시에만 수동 입력."
            )
            st.caption("키 미입력 시 CFO·이익의 질은 생략됩니다. "
                       "자동입력을 원하면 앱 설정 → Secrets에 DART_API_KEY를 등록하세요.")

        # [v3.6] ECOS(한국은행) 키 — 국고채 10년물 폴백용 (선택). Secrets 우선.
        ecos_key = ""
        try:
            ecos_key = st.secrets.get("ECOS_API_KEY", "")
        except Exception:
            ecos_key = ""
        if not ecos_key:
            ecos_key = st.text_input(
                "ECOS 인증키 (선택)", type="password",
                help="ecos.bok.or.kr 무료 발급. 국고채 10년물이 자주 비면 등록하세요. 미입력해도 FDR·네이버로 시도합니다."
            )

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

            # [v3] 보조 데이터 (토큰 절감용)
            ts = get_data_timestamp()
            liq = get_liquidity_and_band(ticker, info.get('high_52'), info.get('low_52'), curr_price)
            macro = get_macro_indicators(ecos_key)

            # [v3 추가] ①컨센서스(네이버모바일) ②CFO ③베타 ④공시 ⑤업종·시총순위
            listing_df = load_listing_df()
            sector_info = get_sector_and_rank(ticker, listing_df)
            beta = get_beta(ticker)
            consensus = get_naver_mobile_consensus(ticker)
            # [v3.5] CFO는 DART 오픈API로 수집 (네이버 모바일엔 현금흐름표 없음)
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
                else:
                    cashflow["error"] = "종목코드→DART 고유번호 매핑 실패 (키 확인 또는 비상장)"
            disclosures = get_recent_disclosures(ticker)

            st.markdown(f"### {info['name']} ({ticker})")

            # ============================================================
            # [v3 신설] 데이터 기준 시각 — Claude가 시세·수급 재검색 생략 판단
            # ============================================================
            adtv_txt = f"{liq['adtv']/1e8:,.1f}억원" if liq.get('adtv') else "N/A"
            usdkrw_txt = f"{macro['usdkrw']:,.1f}" if macro.get('usdkrw') else "N/A"
            kr10y_txt = (f"{macro['kr10y']:.3f}% ({macro.get('kr10y_src','')})"
                         if macro.get('kr10y') else "검색요(미수집)")
            sector_txt = sector_info.get('sector') or sector_info.get('industry') or "N/A"
            rank_txt = f"{sector_info['market'] or ''} {sector_info['marcap_rank']}위" if sector_info.get('marcap_rank') else "N/A"
            beta_txt = f"{beta:.2f}" if beta is not None else "N/A"
            st.markdown(f"""
            <div class="asof-box">
            📌 <b>데이터 기준</b>: {ts['human']} · <b>{ts['session']}</b><br>
            🔗 <b>출처</b>: 네이버 증권(시세·수급·재무·동일업종·공시) / 네이버 모바일API(컨센서스·CFO) / FinanceDataReader(거래대금·환율·베타·업종)<br>
            🏷️ <b>업종</b>: {sector_txt} · <b>시총순위</b>: {rank_txt} · <b>베타(5Y月)</b>: {beta_txt}<br>
            🌐 <b>시장지표</b>: USD/KRW {usdkrw_txt} · 국고채10Y {kr10y_txt} · 20일 ADTV {adtv_txt}
            <br><code>DATA_AS_OF={ts['iso']} SESSION={ts['session']} SRC=NAVER+FDR</code>
            </div>
            """, unsafe_allow_html=True)
            
            diff_color = "black"
            diff_arrow = ""
            if info['direction'] in ['up', 'upper']:
                diff_color = "#d20000"
                diff_arrow = "▲"
            elif info['direction'] in ['down', 'lower']:
                diff_color = "#0051c7"
                diff_arrow = "▼"
            
            st.markdown(f"""
            <div style="display:flex; align-items:flex-end; gap:10px; margin-bottom:10px;">
                <span style="font-size: 2.5rem; font-weight: bold; color:{diff_color};">{info['now_price']}</span>
                <span style="font-size: 1.2rem; color:{diff_color}; margin-bottom: 8px;">
                    {diff_arrow} {info['diff_amount']} ({info['diff_rate']}%)
                </span>
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

            # ============================================================
            # [v3 신설 §8.3] 유동성·가격대 (포지션 사이징 유동성 제약용)
            # ============================================================
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
            st.caption("※ ADTV는 종가×거래량 근사치(원), 출처: FinanceDataReader. 52주 위치 0%=저점·100%=고점.")

            with st.expander("기업 개요 보기"):
                st.write(info['overview'])

            st.markdown(f"""
                <a href="https://m.stock.naver.com/item/main.nhn?code={ticker}#/chart" target="_blank" style="text-decoration:none;">
                    <div style="background-color:#03C75A; color:white; padding:12px; border-radius:8px; text-align:center; font-weight:bold; margin: 15px 0;">
                        📊 네이버 증권 차트 보러가기
                    </div>
                </a>
                """, unsafe_allow_html=True)
            
            t_stamp = int(time.time())
            tab_d, tab_w, tab_m = st.tabs(["일봉", "주봉", "월봉"])
            with tab_d: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/day/{ticker}.png?t={t_stamp}", use_container_width=True)
            with tab_w: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/week/{ticker}.png?t={t_stamp}", use_container_width=True)
            with tab_m: st.image(f"https://ssl.pstatic.net/imgfinance/chart/item/candle/month/{ticker}.png?t={t_stamp}", use_container_width=True)

            if investor_trends:
                st.markdown("### 🏢 외국인/기관 매매동향 (최근 10일)")
                total_inst = 0
                total_frgn = 0
                # [v3 §3.2] 순매수 '금액' 근사 (수량×종가) 누적 — 금액 요구 충족
                est_inst_value = 0.0
                est_frgn_value = 0.0
                for row in investor_trends:
                    close_v = _to_num(row.get('종가')) or 0
                    try:
                        iv = int(row['기관'].replace('+', '').replace(',', ''))
                        total_inst += iv
                        est_inst_value += iv * close_v
                    except: pass
                    try:
                        fv = int(row['외국인'].replace('+', '').replace(',', ''))
                        total_frgn += fv
                        est_frgn_value += fv * close_v
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
.text-red { color: #d20000; }
.text-blue { color: #0051c7; }
.text-black { color: inherit; }
@media (prefers-color-scheme: dark) { .text-black { color: #fff; } }
</style>
<div style="overflow-x:auto;">
<table class="trend-table">
<thead><tr><th>날짜</th><th>종가</th><th>등락률</th><th>기관</th><th>외국인</th><th>보유율</th></tr></thead>
<tbody>
"""
                trend_html += f"""<tr class="total-row"><td style="text-align:center;">10일 합계</td><td colspan="2" style="text-align:center;">-</td><td class="{t_inst_color}">{t_inst_prefix}{abs(total_inst):,}</td><td class="{t_frgn_color}">{t_frgn_prefix}{abs(total_frgn):,}</td><td>-</td></tr>"""

                for row in investor_trends:
                    inst_val_str = row['기관'].replace('+', '').replace(',', '')
                    try: inst_val = int(inst_val_str)
                    except: inst_val = 0
                    inst_color = "text-red" if inst_val > 0 else "text-blue" if inst_val < 0 else "text-black"
                    inst_prefix = "+" if inst_val > 0 else "-" if inst_val < 0 else ""
                    
                    frgn_val_str = row['외국인'].replace('+', '').replace(',', '')
                    try: frgn_val = int(frgn_val_str)
                    except: frgn_val = 0
                    frgn_color = "text-red" if frgn_val > 0 else "text-blue" if frgn_val < 0 else "text-black"
                    frgn_prefix = "+" if frgn_val > 0 else "-" if frgn_val < 0 else ""
                    
                    try: rate_val = float(row['등락률'].replace('%', ''))
                    except: rate_val = 0.0
                    rate_color = "text-red" if rate_val > 0 else "text-blue" if rate_val < 0 else "text-black"

                    trend_html += f'<tr><td style="text-align:center;">{row["날짜"]}</td><td style="text-align:right;">{row["종가"]}</td><td class="{rate_color}" style="text-align:right;">{row["등락률"]}</td><td class="{inst_color}" style="text-align:right;">{inst_prefix}{abs(inst_val):,}</td><td class="{frgn_color}" style="text-align:right;">{frgn_prefix}{abs(frgn_val):,}</td><td style="text-align:right;">{row["보유율"]}</td></tr>'
                
                trend_html += "</tbody></table></div>"
                st.markdown(trend_html, unsafe_allow_html=True)

                # [v3 §3.2] 금액 근사 + 4주체 한계 명시
                st.caption(
                    f"※ 10일 누적 순매수 금액(근사, 수량×종가): "
                    f"기관 ≈ {est_inst_value/1e8:,.0f}억원 · 외국인 ≈ {est_frgn_value/1e8:,.0f}억원. "
                    f"단위: 수량=주, 금액=원 근사. "
                    f"※ 네이버 frgn 페이지는 '기관' 합계만 제공 — 연기금 분리·개인은 미포함(필요 시 KRX 4주체 별도 수집)."
                )

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

            # [v3 §5.2] 배당 항목 표시 추가 (DPS·배당성향·시가배당률)
            items_display = [
                ("매출액(억)", 'revenue'), ("영업이익(억)", 'op_income'), ("영업이익률(%)", 'op_margin'),
                ("당기순이익(억)", 'net_income'), ("순이익률(%)", 'net_income_margin'),
                ("부채비율(%)", 'debt_ratio'), ("당좌비율(%)", 'quick_ratio'), ("유보율(%)", 'reserve_ratio'),
                ("EPS(원)", 'eps'), ("BPS(원)", 'bps'), ("SPS(원)", 'sps'),
                ("PER(배)", 'per'), ("PBR(배)", 'pbr'), ("PSR(배)", 'psr'),
                ("ROE(%)", 'roe'),
                ("주당배당금(원)", 'dps'), ("배당성향(%)", 'payout_ratio'), ("시가배당률(%)", 'dividend_yield'),
            ]

            def render_fin_table(title, data_list):
                disp = []
                cols = ['항목'] + [d['date'] for d in data_list]
                for label, key in items_display:
                    rowv = [label]
                    is_money = '원' in label or '억' in label
                    for d in data_list:
                        val = d.get(key, 0)
                        if val == 0 and key not in ['op_income', 'net_income']:
                            rowv.append("-")
                        else:
                            rowv.append(f"{val:,.0f}" if is_money else f"{val:,.2f}")
                    disp.append(rowv)
                df_ = pd.DataFrame(disp, columns=cols)
                html_ = df_.to_html(index=False, border=0, classes='scroll-table-content')
                st.markdown(f"### {title}")
                st.markdown(f'<div class="scroll-table">{html_}</div>', unsafe_allow_html=True)

            if annual_list:
                render_fin_table("📊 연간 재무제표 (최근 3년)", annual_list)
                st.caption("출처: 네이버 증권 기업실적분석 · 단위: 억원/원/%/배 · [Source: PDF] "
                           "※ 배당 항목(DPS·배당성향·시가배당률)은 DART 섹션이 정식 출처이며, "
                           "본 표의 배당 행은 네이버 기준 참고용(누락 가능).")

            if quarter_list:
                render_fin_table("📊 분기 재무제표 (최근 5분기)", quarter_list)
                st.caption("출처: 네이버 증권 기업실적분석 · 단위: 억원/원/%/배 · [Source: PDF]")

            # [v3 신설 §3.5/§7.2] 컨센서스 추정(E) — 기존엔 버려지던 데이터
            if estimate_list:
                render_fin_table("🔮 컨센서스 추정 (E)", estimate_list)
                st.caption("출처: 네이버 증권 추정 컨센서스(E) · forward 지표(§3.5 Y+1~, §7.2)에 사용 · [Source: PDF]")

            # ============================================================
            # [v3 추가 ① §4.H 확률앵커/§7.2] 컨센서스 목표주가 (네이버 모바일 API)
            # ============================================================
            st.markdown("### 🎯 컨센서스 목표주가 / 투자의견")
            c_avg = consensus.get("target_avg")
            c_hi = consensus.get("target_high")
            c_lo = consensus.get("target_low")
            c_op = consensus.get("opinion")
            c_score = consensus.get("opinion_score")
            c_date = consensus.get("create_date")
            if c_avg or c_hi or c_lo or c_op:
                avg_s = f"{c_avg:,.0f}원" if c_avg else "N/A"
                hi_s = f"{c_hi:,.0f}원" if c_hi else "N/A"
                lo_s = f"{c_lo:,.0f}원" if c_lo else "N/A"
                up_s = f"{(c_avg/curr_price-1)*100:+.1f}%" if (c_avg and curr_price > 0) else "N/A"
                rng_s = f"{(c_hi/c_lo-1)*100:.0f}%" if (c_hi and c_lo and c_lo > 0) else "N/A"
                # 투자의견: 라벨 (점수) 형태
                if c_op and c_score is not None:
                    op_disp = f"{c_op} ({c_score:.2f})"
                elif c_score is not None:
                    op_disp = f"{c_score:.2f}"
                else:
                    op_disp = c_op or "N/A"
                # 목표가 최고/최저는 모바일 API에서 평균만 제공되는 경우가 잦음
                hilo_disp = f"{hi_s}/{lo_s}" if (c_hi or c_lo) else "평균만 제공"
                st.markdown(f"""
                <div class="stock-info-container" style="grid-template-columns: repeat(4, 1fr);">
                    <div class="stock-info-box"><div class="stock-info-label">목표가 평균</div><div class="stock-info-value">{avg_s}</div></div>
                    <div class="stock-info-box"><div class="stock-info-label">현재가 대비</div><div class="stock-info-value">{up_s}</div></div>
                    <div class="stock-info-box"><div class="stock-info-label">목표가 최고/최저</div><div class="stock-info-value">{hilo_disp}</div></div>
                    <div class="stock-info-box"><div class="stock-info-label">투자의견(5점척도)</div><div class="stock-info-value">{op_disp}</div></div>
                </div>
                """, unsafe_allow_html=True)
                date_cap = f" · 컨센 기준일 {c_date}" if c_date else ""
                anchor_cap = f"분포폭 ≈ {rng_s} → §4.H 확률앵커" if (c_hi and c_lo) else "분포폭 미수집(평균만 제공) — §4.H 확률앵커는 컨센서스 평균과 자체 추정의 갭으로 대체"
                st.caption(
                    f"출처: 네이버 모바일 API(consensusInfo) · {anchor_cap} · §7.2 컨센서스 갭{date_cap} · "
                    f"투자의견 척도: 5=강력매수, 4=매수, 3=중립, 2=매도, 1=강력매도 (업계 통용) · [Source: PDF]"
                )
            else:
                st.info("컨센서스 목표가를 자동 추출하지 못했습니다. 아래 디버그에서 실제 JSON 키를 확인하세요.")
            # 키 변경 대비 — 원본 JSON 디버그 (인쇄 시 접힌 상태라 PDF엔 미노출)
            with st.expander("🔧 컨센서스 원본 JSON (디버그 / 키 검증용)"):
                st.write("자동 탐색된 후보 (path, value):")
                st.write(consensus.get("found"))
                if consensus.get("raw") is not None:
                    st.json(consensus["raw"], expanded=False)
                else:
                    st.write("응답 없음 — 엔드포인트/네트워크 확인 필요")

            # ============================================================
            # [v3.5 §3.5/§3.7] 영업활동현금흐름(CFO) & 이익의 질 — DART 오픈API
            # ============================================================
            st.markdown("### 💵 영업활동현금흐름(CFO) & 이익의 질")
            cfo_annual = cashflow.get("annual") or []
            if cfo_annual:
                cfo_rows = [{"기간": r["date"],
                             "CFO(억원)": (f"{r['value']:,.0f}" if r.get("value") is not None else "-")}
                            for r in cfo_annual]
                st.table(pd.DataFrame(cfo_rows))
                # CFO/NI (이익의 질) — DART CFO(억원)와 네이버 순이익(억원) 단위 일치
                try:
                    best = None
                    for d in reversed(annual_list or []):
                        yr = re.search(r'20\d{2}', str(d.get("date", "")))
                        ni = d.get("net_income", 0)
                        if yr and ni:
                            cfo_v = None
                            for r in cfo_annual:
                                if yr.group() in str(r["date"]):
                                    cfo_v = r["value"]
                                    break
                            if cfo_v is not None:
                                best = (d["date"], cfo_v, ni)
                                break
                    if best:
                        d_label, cfo_v, ni = best
                        ratio = cfo_v / ni
                        judge = "양호(≥0.8)" if ratio >= 0.8 else ("주의(<0.5)" if ratio < 0.5 else "보통")
                        st.markdown(f"**CFO/순이익 ≈ {ratio:.2f}** ({d_label} 기준) → {judge}")
                except Exception:
                    pass
                src = cashflow.get("source") or "DART"
                st.caption(f"출처: {src} · 단위 억원(원→억원 환산) · §3.7 이익의 질(CFO/NI), §3.5 영업CF · [Source: DART]")
            else:
                err = cashflow.get("error")
                if not dart_api_key:
                    st.info("CFO 수집 생략 — 사이드바에 DART 인증키를 입력하면 영업활동현금흐름을 표시합니다.")
                else:
                    st.warning(f"CFO를 가져오지 못했습니다: {err or '원인 미상'}")
            with st.expander("🔧 CFO 디버그 (DART)"):
                st.write({
                    "corp_code": cashflow.get("corp_code"),
                    "source": cashflow.get("source"),
                    "error": cashflow.get("error"),
                })
                if cashflow.get("raw_sample"):
                    st.markdown("**CF 항목 샘플(원문 계정명 확인용):**")
                    st.write([{"sj_div": r.get("sj_div"), "account_nm": r.get("account_nm"),
                               "thstrm_amount": r.get("thstrm_amount")} for r in cashflow["raw_sample"]])

            # ============================================================
            # [v3.6 §5.1/§5.2] 한국시장 특수요인 — DART 정식 데이터
            # ============================================================
            if dart_api_key:
                st.markdown("### 🇰🇷 지배구조·밸류업 (DART)")
                col_g, col_d = st.columns(2)

                # ① 지배구조 — 최대주주 지분율 (§5.1)
                with col_g:
                    st.markdown("**최대주주 현황 (§5.1)**")
                    if dart_gov.get("top"):
                        top = dart_gov["top"]
                        st.markdown(f"최대주주: **{top['이름']}** ({top['관계']}) {top['지분율']:.2f}%")
                    if dart_gov.get("total_rate") is not None:
                        st.markdown(f"특수관계인 합계: **{dart_gov['total_rate']:.2f}%**")
                    if dart_gov.get("rows"):
                        gov_df = pd.DataFrame(dart_gov["rows"])
                        gov_df["지분율"] = gov_df["지분율"].apply(lambda x: f"{x:.2f}%")
                        st.table(gov_df)
                    if dart_gov.get("error"):
                        st.caption(f"※ {dart_gov['error']}")
                    yr = dart_gov.get("year")
                    st.caption(f"출처: DART 최대주주현황 ({yr} 사업보고서) · [Source: DART]")

                # ② 밸류업 — 배당 (§5.2)
                with col_d:
                    st.markdown("**배당 추이 (§5.2)**")
                    items = dart_div.get("items") or {}
                    if items:
                        order = ["당기", "전기", "전전기"]
                        div_rows = []
                        for label, vals in items.items():
                            row = {"항목": label}
                            for t in order:
                                v = vals.get(t)
                                row[t] = (f"{v:,.0f}" if (v is not None and label == "DPS")
                                          else (f"{v:.2f}" if v is not None else "-"))
                            div_rows.append(row)
                        st.table(pd.DataFrame(div_rows))
                    if dart_div.get("error"):
                        st.caption(f"※ {dart_div['error']}")
                    st.caption(f"출처: DART 배당에관한사항 ({dart_div.get('year')}) · DPS 원, 성향·수익률 % · [Source: DART]")

                # ③ 밸류업 — 자기주식 (§5.2)
                st.markdown("**자기주식 현황 (§5.2)**")
                if dart_tres.get("hold") is not None:
                    st.markdown(f"보유 자기주식: **{dart_tres['hold']:,.0f}주**")
                if dart_tres.get("rows"):
                    with st.expander("자기주식 세부 내역"):
                        st.table(pd.DataFrame(dart_tres["rows"]))
                if dart_tres.get("error"):
                    st.caption(f"※ {dart_tres['error']}")
                st.caption(f"출처: DART 자기주식 취득·처분현황 ({dart_tres.get('year')}) · [Source: DART]")

                # 파싱 실패 대비 디버그
                with st.expander("🔧 지배구조·배당·자사주 원본 (디버그)"):
                    st.markdown("**최대주주 raw:**"); st.write(dart_gov.get("raw"))
                    st.markdown("**배당 raw:**"); st.write(dart_div.get("raw"))
                    st.markdown("**자기주식 raw:**"); st.write(dart_tres.get("raw"))

            if not annual_list and not quarter_list:
                st.warning("재무 데이터를 불러올 수 없습니다.")

            if not industry_compare_df.empty:
                st.markdown("### 👯 동일업종 비교")
                html_compare = industry_compare_df.to_html(index=False, border=0, classes='scroll-table-content', escape=False)
                st.markdown(f'<div class="scroll-table">{html_compare}</div>', unsafe_allow_html=True)
                st.caption("출처: 네이버 증권 동일업종비교 · Peer 후보군(§4.E 선정기준은 분석 단계에서 적용) · [Source: PDF]")

            st.divider()
            st.markdown("### 💰 S-RIM 적정주가 분석")
            st.caption(f"요구수익률(Ke) {required_return:.1f}% 고정 적용 · [Source: PDF] "
                       f"※ Ke는 분석 단계에서 CAPM 재산정·정합성 점검(v3 §4.A) 대상")

            def show_srim_result(title, bps, roe_used, label_roe, roe_list=None):
                val = calculate_srim(bps, roe_used, required_return)
                excess_rate = roe_used - required_return
                
                st.markdown(f"#### {title}")
                if val > 0 and curr_price > 0:
                    diff_rate = (curr_price - val) / val * 100
                    diff_abs = abs(diff_rate)
                    if val > curr_price:
                        st.success(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 저평가** 상태입니다.")
                    else:
                        st.error(f"현재가({curr_price:,.0f}원)는 적정주가({val:,.0f}원) 대비 **{diff_abs:.1f}% 고평가** 상태입니다.")
                else:
                    st.warning("적정주가를 산출할 수 없습니다.")

                st.markdown("**🧮 산출 근거**")
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("*핵심 변수*")
                    input_df = pd.DataFrame({"구분": ["BPS", f"적용 ROE ({label_roe})"], "값": [f"{bps:,.0f} 원", f"{roe_used:.2f} %"]})
                    st.table(input_df)
                with c2:
                    st.markdown("*ROE 내역*")
                    if roe_list:
                        roe_df = pd.DataFrame(roe_list)
                        roe_df['ROE'] = roe_df['ROE'].apply(lambda x: f"{x:.2f} %")
                        st.table(roe_df)
                    else:
                        st.write(f"적용 ROE: {roe_used:.2f}%")

                # [v3 §4.B] ROE 변동성 진단 (S-RIM 신뢰도 판정 근거)
                if roe_list:
                    vol = roe_volatility([r['ROE'] for r in roe_list])
                    if vol:
                        cv_txt = f"{vol['cv']:.0f}%" if vol['cv'] is not None else "N/A"
                        st.markdown(
                            f"**📐 ROE 변동성**: 평균 {vol['mean']:.2f}% · 표준편차 {vol['std']:.2f}%p · "
                            f"변동계수(CV) {cv_txt} → **{vol['grade']}**"
                        )

                # [v3 §4.B] 지속계수 w 시나리오 (0.0 / 0.8 / 1.0)
                w_rows = []
                for w in [1.0, 0.8, 0.0]:
                    vw = calculate_srim_w(bps, roe_used, required_return, w)
                    gap = f"{(curr_price - vw)/vw*100:+.1f}%" if vw > 0 and curr_price > 0 else "-"
                    w_rows.append({"지속계수 w": f"{w:.1f}", "적정주가(원)": f"{vw:,.0f}", "현재가 괴리": gap})
                st.markdown("*지속계수(w) 시나리오 — w 채택근거는 §3.3 해자 등급과 연동*")
                st.table(pd.DataFrame(w_rows))

                with st.info("계산식"):
                    st.markdown(f"**① 초과이익률** = {roe_used:.2f}% (ROE) - {required_return}% (요구수익률) = **{excess_rate:.2f}%**")
                    st.markdown(f"**② 적정주가(w=1)** = {bps:,.0f} (BPS) + ( {bps:,.0f} × {excess_rate:.2f}% ÷ {required_return}% ) ≈ **{val:,.0f} 원**")

            if annual_list:
                bps_annual = annual_list[-1].get('bps', 0)
                roe_history_annual = []
                for d in annual_list:
                    if d.get('roe'): roe_history_annual.append({'연도': d['date'], 'ROE': d['roe']})
                
                roe_history_annual_3yr = roe_history_annual[-3:]
                avg_roe_annual = sum([r['ROE'] for r in roe_history_annual_3yr]) / len(roe_history_annual_3yr) if roe_history_annual_3yr else 0
                
                show_srim_result("1. 최근 3년 실적 평균 기준 (연간)", bps_annual, avg_roe_annual, "3년 평균", roe_history_annual_3yr)
            
            st.divider()

            if quarter_list:
                bps_quarter = quarter_list[-1].get('bps', 0)
                roe_history_quarter = []
                for d in quarter_list:
                    if d.get('roe'): roe_history_quarter.append({'분기': d['date'], 'ROE': d['roe']})
                
                roe_history_quarter_3q = roe_history_quarter[-3:]
                avg_roe_quarter = sum([r['ROE'] for r in roe_history_quarter_3q]) / len(roe_history_quarter_3q) if roe_history_quarter_3q else 0
                
                show_srim_result("2. 최근 3분기 실적 평균 기준 (분기)", bps_quarter, avg_roe_quarter, "3분기 평균", roe_history_quarter_3q)

            # ============================================================
            # [v3 추가 ④ §7.1] 최근 공시 (제목·날짜) — 상세는 DART에서 검증
            # ============================================================
            st.divider()
            st.markdown("### 📰 최근 공시 (네이버 종목 공시 탭)")
            if disclosures:
                disc_df = pd.DataFrame(disclosures)[["date", "title"]]
                disc_df.columns = ["날짜", "제목"]
                st.table(disc_df)
                st.caption("출처: 네이버 증권 공시 · §7.1 조건부 판단용(자사주·CB/BW·유증·정정 등) · 상세·원문은 DART 검증 · [Source: PDF]")
            else:
                st.caption("최근 공시 자동 수집 결과 없음 — DART에서 직접 확인 권장")

        except Exception as e:
            st.error(f"오류 발생: {e}")

if __name__ == "__main__":
    main()
