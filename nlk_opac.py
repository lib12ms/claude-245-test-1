"""
국립중앙도서관 소장자료 Open API — 표제/저자사항(책임표시) 및 원서명·원저자명 조회.

환경 변수:
  NLK_OPENAPI_KEY — https://www.nl.go.kr Open API 인증키 (소장자료 검색)

요청 예:
  https://www.nl.go.kr/NL/search/openApi/search.do?key=...&kwd=97889...&pageSize=10
  https://www.nl.go.kr/NL/search/marc_view.do?viewKey={id}&viewType=AH1
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

import requests

NLK_OPAC_SEARCH_URL = "https://www.nl.go.kr/NL/search/openApi/search.do"
NLK_MARC_VIEW_URL   = "https://www.nl.go.kr/NL/search/marc_view.do"
_NLK_HEADERS        = {"User-Agent": "KORMARC-generator/1.0"}

# MARC 서브필드 구분자 (▼ 또는 $)
_SF = r"[▼\$]"

# 국중 표제/저자사항에 쓰이는 역할어 (책임표시 절)
_NLK_RESP_ROLE = (
    r"(?:지은이|저자|옮긴이|역자|그린이|엮은이|편저|감수|해설|원작|글|글쓴이|"
    r"공저|편집|사진|일러스트|편역|해제|구성|각색|감수자|낭독|번역)"
)
_NLK_RESP_BLOCK_RE = re.compile(
    rf"({_NLK_RESP_ROLE}\s*:[^<>;/]+(?:\s*;\s*{_NLK_RESP_ROLE}\s*:[^<>;/]+)*)",
    re.I,
)

# 246 19 원서명 패턴
_MARC_246_RE = re.compile(
    rf"246\s+1\s*9\s+{_SF}a\s*([^▼$\n\r]+)",
    re.I,
)

# 500 원저자명 패턴
_MARC_500_AUTHOR_RE = re.compile(
    rf"500\s+[^\n]*?{_SF}a원저자명\s*:\s*([^▼$\n\r]+)",
    re.I,
)


def extract_responsibility_from_catalog_text(text: str) -> str | None:
    """
    '표제 / 지은이: … ; 옮긴이: …' 또는 '지은이: … ; …' 에서 책임표시만 추출.
    """
    if not text or not text.strip():
        return None
    t = re.sub(r"\s+", " ", text.strip())
    if "/" in t:
        t = t.split("/")[-1].strip()
    m = _NLK_RESP_BLOCK_RE.search(t)
    if not m:
        return None
    out = re.sub(r"\s*;\s*", " ; ", m.group(1).strip())
    return re.sub(r"\s+", " ", out).strip() or None


def _parse_nlk_opac_xml(xml_text: str) -> str | None:
    """Open API XML 본문에서 책임표시 문자열 탐색."""
    if not xml_text or not xml_text.strip():
        return None

    candidates: list[str] = []
    try:
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            if elem.text and _NLK_RESP_BLOCK_RE.search(elem.text):
                candidates.append(elem.text.strip())
    except ET.ParseError:
        pass

    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml_text))
    m = _NLK_RESP_BLOCK_RE.search(plain)
    if m:
        candidates.append(m.group(1))

    for c in candidates:
        got = extract_responsibility_from_catalog_text(c)
        if got:
            return got
    return extract_responsibility_from_catalog_text(plain)


def _get_nlk_record_id(isbn13: str, key: str) -> str | None:
    """ISBN으로 국중 검색 API 호출 → 레코드 id 반환."""
    params = {
        "key":      key,
        "kwd":      isbn13,
        "pageSize": 5,
        "pageNo":   1,
    }
    try:
        resp = requests.get(
            NLK_OPAC_SEARCH_URL,
            params=params,
            timeout=12,
            headers=_NLK_HEADERS,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    if "INVALID KEY" in resp.text or "<error>" in resp.text[:500].lower():
        return None

    try:
        root = ET.fromstring(resp.text)
        id_elem = root.find(".//id")
        return id_elem.text.strip() if id_elem is not None and id_elem.text else None
    except ET.ParseError:
        return None


def _fetch_nlk_marc_text(record_id: str) -> str | None:
    """MARC 뷰 페이지 텍스트 반환."""
    try:
        resp = requests.get(
            NLK_MARC_VIEW_URL,
            params={"viewKey": record_id, "viewType": "AH1"},
            timeout=12,
            headers=_NLK_HEADERS,
        )
        resp.raise_for_status()
        return resp.text
    except requests.RequestException:
        return None


def _parse_marc_orig_info(marc_text: str) -> dict:
    """MARC 텍스트에서 246 19(원서명)·500 원저자명 추출."""
    result = {"orig_title": None, "orig_author": None}
    if not marc_text:
        return result

    # HTML 태그 제거 후 평탄화
    plain = re.sub(r"<[^>]+>", " ", marc_text)
    plain = re.sub(r"&[a-zA-Z]+;", " ", plain)

    m246 = _MARC_246_RE.search(plain)
    if m246:
        t = m246.group(1).strip().rstrip(".,;")
        if t:
            result["orig_title"] = t

    m500 = _MARC_500_AUTHOR_RE.search(plain)
    if m500:
        a = m500.group(1).strip().rstrip(".,;")
        if a:
            result["orig_author"] = a

    return result


def fetch_nlk_orig_info(isbn13: str) -> dict:
    """
    ISBN으로 국중 MARC 레코드를 조회해 원서명·원저자명 반환.
    반환: {"orig_title": str|None, "orig_author": str|None}
    키가 없거나 조회 실패 시 두 값 모두 None.
    """
    empty = {"orig_title": None, "orig_author": None}
    key = (os.environ.get("NLK_OPENAPI_KEY") or os.environ.get("NLK_API_KEY") or "").strip()
    if not key:
        return empty

    isbn = re.sub(r"[^0-9Xx]", "", (isbn13 or ""))
    if len(isbn) not in (10, 13):
        return empty

    record_id = _get_nlk_record_id(isbn, key)
    if not record_id:
        return empty

    marc_text = _fetch_nlk_marc_text(record_id)
    if not marc_text:
        return empty

    result = _parse_marc_orig_info(marc_text)
    print(f"[NLK] isbn={isbn} id={record_id} → {result}", flush=True)
    return result


def fetch_nlk_responsibility_statement(isbn13: str) -> str | None:
    """
    ISBN으로 국중 소장자료를 조회해 '지은이: … ; 옮긴이: …' 형태 문자열 반환.
    키가 없거나 조회 실패 시 None.
    """
    key = (os.environ.get("NLK_OPENAPI_KEY") or os.environ.get("NLK_API_KEY") or "").strip()
    if not key:
        return None
    isbn = re.sub(r"[^0-9Xx]", "", (isbn13 or ""))
    if len(isbn) not in (10, 13):
        return None

    params = {
        "key":      key,
        "kwd":      isbn,
        "pageSize": 10,
        "pageNo":   1,
    }
    try:
        resp = requests.get(
            NLK_OPAC_SEARCH_URL,
            params=params,
            timeout=12,
            headers=_NLK_HEADERS,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    if "INVALID KEY" in resp.text or "<error>" in resp.text[:500].lower():
        return None
    return _parse_nlk_opac_xml(resp.text)
