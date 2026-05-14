"""
KORMARC 자동 생성기 - Flask 백엔드 (Render 배포용)

[데이터 수집 순서]
  1. 알라딘 API (표제, 저자, 역자, subInfo.originalTitle)
  2. 알라딘 상품 페이지 크롤링
     - SearchWord 링크 → 원제/원저자 영문명
     - "원제 :" 텍스트 패턴 → 원제
     - "한글명 (영문명)" 패턴 → 원저자 영문명
     - meta-author 태그 → 한자명
     - 동아시아 키워드 → 동아시아 여부
  3. 국립중앙도서관 API → 원제/원저자 영문명

[생성 필드]
  245 00  표제와 책임표시사항 (총서명 제거, 마케팅 부제목 제거)
  246 19  원서명 (번역서만)
  500 __  원저자명 주기 (번역서만)
  700 1_  개인명 부출기입
  710 0_  기관명 부출기입
  900 10  원저자 한글명 부출 (동아시아 도치 없음, 서양 도치, 한자only 음독)
"""

from __future__ import annotations

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import os

try:
    import hanja
    HANJA_AVAILABLE = True
except ImportError:
    HANJA_AVAILABLE = False

app = Flask(__name__)
CORS(app)

ALADIN_API_KEY = os.environ.get("ALADIN_API_KEY", "ttbboyeong09010919001")
ALADIN_API_URL = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
NL_API_KEY     = os.environ.get("NL_API_KEY", "")
NL_ISBN_URL    = "https://www.nl.go.kr/seoji/SearchApi.do"
NL_COLL_URL    = "https://www.nl.go.kr/NL/search/openApi/search.do"

ALADIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────
ORG_KEYWORDS = (
    "협회", "학회", "위원회", "연구소", "연구원", "연구회", "센터",
    "재단", "법인", "기관", "청", "공단", "공사",
    "협의회", "연합회", "연맹", "조합",
    "대학교", "대학", "학교", "출판사", "출판부",
    "association", "institute", "council", "committee",
    "foundation", "university", "society", "organization",
    "corp", "inc", "ltd",
)

ROLE_LABEL = {
    "옮긴이": "옮긴이", "역자": "옮긴이", "번역": "옮긴이",
    "그린이": "그린이", "그림": "그린이", "일러스트": "그린이",
    "사진": "사진", "감수": "감수", "편저": "편저", "편역": "편역",
    "엮은이": "엮은이", "편집": "엮은이", "해설": "해설",
}

PRIMARY_ROLES = {"지은이", "저자", "글", "글쓴이", ""}
TRANS_ROLES   = {"옮긴이", "역자", "번역", "편역"}

EAST_ASIA_KEYWORDS = (
    "일본 출생",
    "일본 출신",
    "일본인",
    "중국 출생",
    "중국 출신",
    "중국인",
    "대만",
    "홍콩",
    "도쿄",
    "오사카",
    "교토",
    "이와테",
    "홋카이도",
    "오키나와",
    "나고야",
    "후쿠오카",
    "삿포로",
    "고베",
    "요코하마",
    "베이징",
    "상하이",
    "광저우",
    "타이베이",
    "東京",
    "大阪",
    "京都",
    "北京",
    "上海",
)

SUBTITLE_NOISE_PATTERNS = [
    r"\d{4}\s*일본\s*서점대상.*",
    r"\d{4}\s*서점대상.*",
    r"일본\s*서점대상.*",
    r"\d{4}\s*본야도이상.*",
    r".*수상작$",
    r".*수상$",
    r".*대상\s*\d+위.*",
    r".*베스트셀러.*",
]

# 한자 음독 딕셔너리
HANJA_READING: dict[str, str] = {
    "村":"촌","上":"상","春":"춘","樹":"수","安":"안","西":"서","水":"수","丸":"환",
    "阿":"아","部":"부","曉":"효","子":"자","山":"산","川":"천","田":"전","中":"중",
    "大":"대","小":"소","木":"목","本":"본","森":"삼","林":"림","原":"원","野":"야",
    "井":"정","石":"석","金":"금","藤":"등","松":"송","竹":"죽","梅":"매","花":"화",
    "鳥":"조","魚":"어","馬":"마","龍":"룡","鳳":"봉","虎":"호","鶴":"학","一":"일",
    "二":"이","三":"삼","四":"사","五":"오","六":"육","七":"칠","八":"팔","九":"구",
    "十":"십","百":"백","千":"천","萬":"만","東":"동","南":"남","北":"북","左":"좌",
    "右":"우","前":"전","後":"후","内":"내","外":"외","天":"천","地":"지","人":"인",
    "火":"화","月":"월","日":"일","年":"년","生":"생","愛":"애","心":"심","道":"도",
    "太":"태","正":"정","新":"신","古":"고","長":"장","短":"단","高":"고","低":"저",
    "明":"명","暗":"암","光":"광","影":"영","白":"백","黑":"흑","赤":"적","靑":"청",
    "黄":"황","紫":"자","緑":"록","美":"미","善":"선","眞":"진","幸":"행","福":"복",
    "壽":"수","喜":"희","怒":"노","哀":"애","樂":"락","平":"평","和":"화","友":"우",
    "王":"왕","臣":"신","民":"민","文":"문","武":"무","詩":"시","歌":"가","書":"서",
    "音":"음","海":"해","空":"공","星":"성","雨":"우","雪":"설","風":"풍","雲":"운",
    "草":"초","葉":"엽","根":"근","枝":"지","幹":"간",
}


# ─────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────
def is_org(name: str) -> bool:
    return any(kw in name.lower() for kw in ORG_KEYWORDS)


def to_title_case(word: str) -> str:
    return "-".join(part.capitalize() for part in word.split("-"))


def remove_series(title: str) -> str:
    """괄호로 묶인 총서명 제거: "젊은 베르테르의 슬픔 (먼슬리 클래식)" → "젊은 베르테르의 슬픔" """
    return re.sub(r"\s*\([^)]+\)\s*$", "", title).strip()


def remove_year(text: str) -> str:
    """연도 괄호 제거: "Title (1774년)" → "Title" """
    return re.sub(r"\s*\(\d{4}년?\)\s*$", "", text).strip()


def clean_subtitle(subtitle: str) -> str:
    """부제목에서 마케팅/수상 키워드 제거"""
    if not subtitle:
        return subtitle
    for pattern in SUBTITLE_NOISE_PATTERNS:
        if re.search(pattern, subtitle.strip()):
            return ""
    for sep in [" · ", " - ", " | ", " / "]:
        if sep in subtitle:
            parts = subtitle.split(sep)
            cleaned = [p for p in parts if not any(re.search(pat, p.strip()) for pat in SUBTITLE_NOISE_PATTERNS)]
            return sep.join(cleaned).strip() if cleaned else ""
    return subtitle.strip()


def korean_name_reverse(name: str) -> str | None:
    """서양식 한글 표기 역순: "요한 볼프강 폰 괴테" → "괴테, 요한 볼프강 폰" """
    if not re.search(r"[\uac00-\ud7a3]", name):
        return None
    parts = name.strip().split()
    if len(parts) < 2:
        return None
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def english_name_reverse(name: str) -> str:
    """영문 이름 역순: "Johann Wolfgang von Goethe" → "Goethe, Johann Wolfgang von" """
    parts = name.strip().split()
    if len(parts) < 2:
        return name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def kanji_type(name: str) -> str:
    """한자명 문자 구성 판별"""
    has_kanji    = bool(re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", name))
    has_hiragana = bool(re.search(r"[\u3040-\u309f]", name))
    has_katakana = bool(re.search(r"[\u30a0-\u30ff]", name))
    has_kana     = has_hiragana or has_katakana
    if has_kanji and has_kana:
        return "kanji_kana"
    elif has_kanji:
        return "kanji_only"
    elif has_kana:
        return "kana_only"
    else:
        return "other"


def kanji_to_korean_reading(name: str) -> str | None:
    """한자 이름을 한국 음독으로 변환: 村上春樹 → 촌상춘수"""
    if HANJA_AVAILABLE:
        try:
            result = hanja.translate(name, "substitution")
            if re.search(r"[\uac00-\ud7a3]", result):
                return result
        except Exception:
            pass
    # 내장 딕셔너리 폴백
    result = []
    for ch in name:
        if ch in HANJA_READING:
            result.append(HANJA_READING[ch])
        elif re.match(r"[\u4e00-\u9fff\u3400-\u4dbf]", ch):
            return None
    return "".join(result) if result else None


def aladin_item_description_blob(item: dict) -> str:
    """알라딘 ItemLookUp 본문 — 상품 페이지 크롤링 실패 시 원저자 영문 힌트용."""
    parts: list[str] = []
    for key in ("fullDescription2", "fullDescription", "description"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    sub = item.get("subInfo")
    if isinstance(sub, dict):
        for val in sub.values():
            if isinstance(val, str) and len(val) > 20:
                parts.append(val)
    return "\n".join(parts)


def extract_english_author_after_korean_paren(text: str, limit: int = 16000) -> str | None:
    """'한글명 (English Name)' 패턴에서 영문명 추출 (상세설명·저자소개 등)."""
    if not text:
        return None
    chunk = text[:limit]
    for m in re.finditer(
        r"[\uac00-\ud7a3][\uac00-\ud7a3\s]*\(([A-Z][a-z]+(?:\s+[A-Za-z.\-\']+){1,})\)",
        chunk,
    ):
        candidate = re.sub(r"\s+", " ", m.group(1)).strip()
        words = candidate.split()
        if len(words) >= 2 and all(re.match(r"^[A-Za-z.\-\']+$", w) for w in words):
            return candidate
    return None


def parse_authors(author_str: str) -> list[dict]:
    result  = []
    pattern = re.findall(r"([^,(]+?)\s*\(([^)]+)\)", author_str)
    if pattern:
        for name, role in pattern:
            name = name.strip()
            result.append({"name": name, "role": role.strip(), "is_org": is_org(name)})
    else:
        for name in author_str.split(","):
            name = name.strip()
            if name:
                result.append({"name": name, "role": "", "is_org": is_org(name)})
    return result


def to_isbn13(isbn: str) -> str:
    if len(isbn) == 13:
        return isbn
    base  = "978" + isbn[:9]
    check = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(base))
    return base + str((10 - check % 10) % 10)


# ─────────────────────────────────────────────
# 알라딘 API
# ─────────────────────────────────────────────
def fetch_aladin(isbn: str) -> dict:
    params = {
        "ttbkey":     ALADIN_API_KEY,
        "itemIdType": "ISBN13",
        "ItemId":     isbn,
        "output":     "js",
        "Version":    "20131101",
        "OptResult":  "authors,subInfo,seriesInfo",
    }
    resp = requests.get(ALADIN_API_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("item"):
        raise ValueError("도서를 찾을 수 없습니다.")
    return data["item"][0]


# ─────────────────────────────────────────────
# 알라딘 상품 페이지 크롤링
# ─────────────────────────────────────────────
def _aladin_page_text_skip_global_nav(page_text: str) -> str:
    """전역 메뉴(일본도서·중국 도서 등)와 본문 분리 — 동아시아 키워드 오탐 방지."""
    for marker in (
        "저자 및 역자소개",
        "저자소개",
        "저자 프로필",
        "상품정보 요약",
        "책소개",
    ):
        i = page_text.find(marker)
        if i != -1:
            return page_text[i : i + 100000]
    return page_text


def scrape_aladin_product(item_id: str) -> dict:
    """
    알라딘 상품 페이지 크롤링.
    반환: {
        "orig_title": str|None,
        "orig_author_en": str|None,
        "kanji_map": dict,       # {"한글명": "漢字名"}
        "is_east_asian": bool
    }
    """
    result = {
        "orig_title":     None,
        "orig_author_en": None,
        "kanji_map":      {},
        "is_east_asian":  False,
    }

    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}"
    try:
        resp = requests.get(url, headers=ALADIN_HEADERS, timeout=10, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            # 리다이렉트 시 원래 URL로 강제 요청
            resp = requests.get(url, headers=ALADIN_HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return result

    soup      = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text()

    # ── 원제/원저자 영문명 추출 ──────────────────────

    # 방법 1: SearchWord 링크에서 추출 (ALL CAPS → 저자명, 나머지 → 원제)
    orig_link = soup.find("a", href=re.compile(r"SearchTarget=Foreign&SearchWord="))
    if orig_link:
        href  = orig_link.get("href", "")
        m = re.search(r"SearchWord=([^&\"]+)", href)
        if m:
            raw   = m.group(1).replace("+", " ").strip()
            parts = raw.split()
            author_parts = [p for p in parts if re.sub(r"[-']", "", p).isupper() and len(p) > 1]
            title_parts  = [p for p in parts if p not in author_parts]
            if title_parts:
                result["orig_title"] = remove_year(" ".join(title_parts))
            if author_parts:
                result["orig_author_en"] = " ".join(to_title_case(p) for p in author_parts)

    # 방법 2: "원제 :" 텍스트 패턴으로 원제 추출
    if not result["orig_title"]:
        m = re.search(r"원제\s*[:：]\s*([^\n\r<(]+)", page_text)
        if m:
            candidate = remove_year(m.group(1).strip())
            if candidate and not re.search(r"[\uac00-\ud7a3]", candidate):
                result["orig_title"] = candidate

    # 방법 3: 저자 및 역자소개 섹션에서 "한글명 (영문명)" 패턴으로 원저자 영문명 추출
    # 알라딘 저자소개는 class="Ere_author_intro" 또는 id="authorIntroContent" 등에 있음
    if not result["orig_author_en"]:
        # 저자소개 섹션 찾기
        intro_section = (
            soup.find("div", class_=re.compile(r"author", re.I)) or
            soup.find("div", id=re.compile(r"author", re.I)) or
            soup.find("div", class_=re.compile(r"Ere_prod_mcontents", re.I))
        )
        search_text = intro_section.get_text() if intro_section else page_text[:3000]

        for m in re.finditer(
            r"[\uac00-\ud7a3][\uac00-\ud7a3\s]+\(([A-Z][a-z]+(?:\s+[A-Za-z]+){1,})\)",
            search_text
        ):
            candidate = re.sub(r"\s+", " ", m.group(1)).strip()
            words = candidate.split()
            if len(words) >= 2 and all(re.match(r"[A-Za-z\-\.\']", w) for w in words):
                result["orig_author_en"] = candidate
                break

    # 방법 3: 저자 및 역자소개 섹션에서 "한글명 (영문명)" 패턴으로 원저자 영문명 추출
    # 예: "앤디 위어 (Andy Weir)" 또는 "요한 볼프강 폰 괴테 (Johann Wolfgang von Goethe)"
    if not result["orig_author_en"]:
        # 저자소개 섹션 찾기 (알라딘 HTML 구조)
        intro_div = (
            soup.find("div", class_=re.compile(r"Ere_prod_mcontents", re.I)) or
            soup.find("div", id=re.compile(r"authorIntro", re.I)) or
            soup.find("div", class_=re.compile(r"author_info", re.I))
        )
        search_text = intro_div.get_text() if intro_div else page_text

        for m in re.finditer(
            r"[\uac00-\ud7a3][\uac00-\ud7a3\s]+\(([A-Z][a-z]+(?:\s+[A-Za-z]+){1,})\)",
            search_text
        ):
            candidate = re.sub(r"\s+", " ", m.group(1)).strip()
            words = candidate.split()
            if len(words) >= 2 and all(re.match(r"[A-Za-z\-\.\']", w) for w in words):
                result["orig_author_en"] = candidate
                break
    CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u309f\u30a0-\u30ff]")
    for meta in soup.find_all("meta"):
        content   = meta.get("content", "") or meta.get("value", "")
        name_attr = (meta.get("name", "") + meta.get("property", "")).lower()
        if not content or "author" not in name_attr:
            continue
        parts = [p.strip() for p in content.split(",")]
        i = 0
        while i < len(parts):
            ko_name = parts[i]
            if re.search(r"[\uac00-\ud7a3]", ko_name) and not CJK_PATTERN.search(ko_name):
                if i + 1 < len(parts):
                    next_part = parts[i + 1]
                    if CJK_PATTERN.search(next_part) and not re.search(r"[\uac00-\ud7a3]", next_part):
                        result["kanji_map"][ko_name] = next_part
                        i += 2
                        continue
            i += 1

    # 방법 4: 저자소개 밖·HTML 속성 등 — 전체 페이지에서 '한글 (English Name)' (API 없이 크롤만으로 보완)
    if not result["orig_author_en"]:
        hit = extract_english_author_after_korean_paren(page_text)
        if not hit:
            hit = extract_english_author_after_korean_paren(resp.text)
        if hit:
            result["orig_author_en"] = hit

    # ── 동아시아 저자 판별 (전역 네비 '일본도서' 등 제외) ───────────────────
    bio_slice = _aladin_page_text_skip_global_nav(page_text)
    if any(kw in bio_slice for kw in EAST_ASIA_KEYWORDS):
        result["is_east_asian"] = True

    return result


# ─────────────────────────────────────────────
# 국립중앙도서관 API 폴백
# ─────────────────────────────────────────────
def scrape_nl_detail(control_no: str) -> dict:
    """국중 소장자료 페이지에서 원표제/원저자명 추출"""
    result = {"orig_title": None, "orig_author_en": None}
    if not control_no:
        return result
    url = f"https://www.nl.go.kr/NL/contents/search.do?pageNum=1&pageSize=10&srchTarget=total&kwd={control_no}"
    try:
        resp = requests.get(url, headers=ALADIN_HEADERS, timeout=10)
        resp.raise_for_status()
        text = resp.text
    except requests.RequestException:
        return result

    m = re.search(r"원저자명\s*[:：]\s*(.+?)(?=\s*원표제|\s*내용|\s*일본어|\s*중국어|\s*영어|\s*ISBN|$)", text)
    if m:
        candidate = re.sub(r"\s+", " ", m.group(1)).strip()
        if candidate and not re.search(r"[\uac00-\ud7a3]", candidate):
            result["orig_author_en"] = candidate

    m = re.search(r"원표제\s*[:：]\s*(.+?)(?=\s*내용|\s*일본어|\s*중국어|\s*영어|\s*ISBN|\s*원저자|$)", text)
    if m:
        candidate = re.sub(r"\s+", " ", m.group(1)).strip()
        if candidate and not re.search(r"[\uac00-\ud7a3]", candidate):
            result["orig_title"] = remove_year(candidate)

    return result


def fetch_nl_library(isbn13: str) -> dict:
    """국립중앙도서관 API로 원서명·원저자 영문명 조회"""
    result = {"orig_title": None, "orig_author_en": None}
    if not NL_API_KEY:
        return result

    # seoji API → TITLE 필드에서 원표제, AUTHOR 필드에서 원저자 영문명
    try:
        params = {
            "cert_key":     NL_API_KEY,
            "result_style": "json",
            "page_no":      1,
            "page_size":    5,
            "isbn":         isbn13,
        }
        resp = requests.get(NL_ISBN_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("docs", [])
        if docs:
            doc = docs[0]
            title_raw = doc.get("TITLE", "")
            if "=" in title_raw:
                candidate = title_raw.split("=", 1)[1].strip()
                candidate = re.sub(r"\s*\(\d{4}년?\)\s*$", "", candidate).strip()
                if candidate and not re.search(r"[\uac00-\ud7a3]", candidate):
                    result["orig_title"] = candidate
            author_raw = doc.get("AUTHOR", "")
            for part in re.split(r"[;,]", author_raw):
                part = part.strip()
                name = re.sub(r"\s*(지음|저|원저|지은이|글쓴이|씀|저자)\s*$", "", part).strip()
                if re.search(r"[A-Za-z]", name) and not re.search(r"[\uac00-\ud7a3]", name) and len(name) > 2:
                    result["orig_author_en"] = name
                    break
    except requests.RequestException:
        pass

    # 소장자료 API → control_no 추출 후 상세 페이지 크롤링
    try:
        params = {
            "key":      NL_API_KEY,
            "isbnOp":   "isbn",
            "isbnCode": isbn13,
            "pageSize": 3,
            "startNum": 1,
            "kwd":      isbn13,
        }
        resp = requests.get(NL_COLL_URL, params=params, timeout=10)
        resp.raise_for_status()
        from xml.etree import ElementTree as ET
        root = ET.fromstring(resp.text)
        for item in root.iter("item"):
            control_el = item.find("control_no")
            if control_el is not None and control_el.text:
                detail = scrape_nl_detail(control_el.text.strip())
                if not result["orig_title"] and detail["orig_title"]:
                    result["orig_title"] = detail["orig_title"]
                if not result["orig_author_en"] and detail["orig_author_en"]:
                    result["orig_author_en"] = detail["orig_author_en"]
                break
    except Exception:
        pass

    return result


# ─────────────────────────────────────────────
# 원제·원저자 수집 메인 로직
# ─────────────────────────────────────────────
def collect_orig_info(item: dict, item_id: str, isbn13: str, title: str, authors: list[dict]) -> dict:
    non_trans    = [a for a in authors if not a["is_org"] and a["role"] not in TRANS_ROLES]
    primary_ko   = [a for a in non_trans if a["role"] in PRIMARY_ROLES]
    primary_name = primary_ko[0]["name"] if primary_ko else (non_trans[0]["name"] if non_trans else "")

    orig_title:     str | None = None
    orig_author_en: str | None = None

    # 1순위: 알라딘 API subInfo.originalTitle
    sub_info = item.get("subInfo", {})
    if isinstance(sub_info, dict):
        api_orig = sub_info.get("originalTitle", "").strip()
        if api_orig:
            orig_title = remove_year(api_orig)

    # 2순위: 알라딘 상품 페이지 크롤링
    scraped       = scrape_aladin_product(item_id)
    kanji_map     = scraped["kanji_map"]
    is_east_asian = scraped["is_east_asian"]

    if not orig_title and scraped["orig_title"]:
        orig_title = scraped["orig_title"]
    if not orig_author_en and scraped["orig_author_en"]:
        orig_author_en = scraped["orig_author_en"]

    # 3순위: 국립중앙도서관 API
    if not orig_title or not orig_author_en:
        nl = fetch_nl_library(isbn13)
        if not orig_title and nl["orig_title"]:
            orig_title = nl["orig_title"]
        if not orig_author_en and nl["orig_author_en"]:
            orig_author_en = nl["orig_author_en"]

    # 각 저자별 한자명
    author_info = []
    for a in non_trans:
        author_info.append({
            "name":          a["name"],
            "kanji_name":    kanji_map.get(a["name"]),
            "is_east_asian": is_east_asian,
        })

    return {
        "orig_title":     orig_title,
        "orig_author_en": orig_author_en,
        "author_info":    author_info,
    }


# ─────────────────────────────────────────────
# MARC 필드 빌더
# ─────────────────────────────────────────────
def build_245(title: str, subtitle: str, authors: list[dict]) -> str:
    a_part    = remove_series(title)
    b_part    = clean_subtitle(subtitle)
    persons   = [a for a in authors if not a["is_org"]]
    primary   = [a for a in persons if a["role"] in PRIMARY_ROLES]
    secondary = [a for a in persons if a["role"] not in PRIMARY_ROLES]

    role_groups: dict[str, list[str]] = {}
    for a in secondary:
        label = ROLE_LABEL.get(a["role"], a["role"])
        role_groups.setdefault(label, []).append(a["name"])

    field = f"$a {a_part}"
    if b_part:
        field += f" $b : {b_part}"

    if primary:
        field += f" /$d {primary[0]['name']}"
        for a in primary[1:]:
            field += f" ,$e {a['name']}"
        for label, names in role_groups.items():
            for name in names:
                field += f" ;$e {name}"
    elif role_groups:
        all_names = [n for ns in role_groups.values() for n in ns]
        field += f" /$d {all_names[0]}"
        for name in all_names[1:]:
            field += f" ,$e {name}"

    return field


def build_246(orig_title: str | None) -> str | None:
    if not orig_title:
        return None
    return f"246 19 $a {remove_year(orig_title.strip())}"


def build_500(orig_author_en: str | None, kanji_name: str | None = None) -> str | None:
    """
    케이스 1: 한자+가나 혼합 (鈴木いづみ) → 500 __ $a 원저자명: 鈴木いづみ
    케이스 2: 한자만 (村上春樹)           → 500 __ $a 원저자명: 村上春樹
    케이스 3: 서양 저자                   → 500 __ $a 원저자명: Georges Bernanos
    """
    if kanji_name:
        ktype = kanji_type(kanji_name)
        if ktype in ("kanji_only", "kanji_kana"):
            return f"500 __ $a 원저자명: {kanji_name}"
    if orig_author_en:
        return f"500 __ $a 원저자명: {orig_author_en.strip()}"
    return None


def build_700(author: dict) -> str:
    name = author["name"].strip()
    if re.search(r"[A-Za-z]", name) and not re.search(r"[\uac00-\ud7a3]", name):
        name = english_name_reverse(name)
    return f"$a {name}"


def build_710(author: dict) -> str:
    return f"$a {author['name'].strip()}"


def build_900(
    orig_author_ko: str | None,
    is_east_asian: bool = False,
    kanji_name: str | None = None,
    orig_author_en: str | None = None,
    translation_book: bool = False,
) -> str | None:
    """
    246 19(원서명)이 있는 번역·원서 맥락(translation_book)이면 한국어 표기 900은 반드시 출력.
    서양 원저: 한글 성·이름 도치. 동아시아 한글 표기: 도치 없이 그대로(단 kan자만이면 음독).
    """
    if not orig_author_ko:
        return None
    ko = orig_author_ko.strip()

    if kanji_name:
        ktype = kanji_type(kanji_name)
        if ktype == "kanji_kana":
            if translation_book:
                return f"900 10 $a {ko}"
            return None
        if ktype == "kanji_only":
            reading = kanji_to_korean_reading(kanji_name)
            if reading:
                return f"900 10 $a {reading}"
            if translation_book:
                return f"900 10 $a {ko}"
            return None

    if orig_author_en:
        reversed_name = korean_name_reverse(ko)
        if reversed_name:
            return f"900 10 $a {reversed_name}"
        return f"900 10 $a {ko}"

    if is_east_asian:
        return f"900 10 $a {ko}"

    reversed_name = korean_name_reverse(ko)
    if reversed_name:
        return f"900 10 $a {reversed_name}"
    return f"900 10 $a {ko}" if translation_book else None


# ─────────────────────────────────────────────
# 라우트
# ─────────────────────────────────────────────
@app.route("/api/isbn", methods=["GET"])
def isbn_lookup():
    try:
        return _isbn_lookup()
    except Exception as e:
        import traceback, sys
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": f"서버 오류: {str(e)}"}), 500


def _isbn_lookup():
    isbn = request.args.get("isbn", "").replace("-", "").strip()
    if not isbn:
        return jsonify({"error": "ISBN을 입력해 주세요."}), 400
    if not re.fullmatch(r"\d{10}|\d{13}", isbn):
        return jsonify({"error": "올바른 ISBN-10 또는 ISBN-13 형식이 아닙니다."}), 400

    isbn13 = to_isbn13(isbn)

    try:
        item = fetch_aladin(isbn13)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except requests.RequestException as e:
        return jsonify({"error": f"알라딘 API 오류: {e}"}), 502

    # 표제 / 부제목 분리
    title    = item.get("title", "")
    subtitle = ""

    # 총서명 제거 (seriesInfo.seriesName 활용)
    series_info = item.get("seriesInfo", {})
    if isinstance(series_info, dict):
        series_name = series_info.get("seriesName", "").strip()
        if series_name:
            series_base = re.sub(r"\s*\d+$", "", series_name).strip()
            title = re.sub(r"\s*\(" + re.escape(series_base) + r"[^)]*\)", "", title).strip()

    sub_info = item.get("subInfo", {})
    if isinstance(sub_info, dict):
        api_sub = sub_info.get("subTitle", "").strip()
        if api_sub and title.endswith(api_sub):
            title    = title[: -len(api_sub)].rstrip(" -:").strip()
            subtitle = api_sub
        elif api_sub:
            subtitle = api_sub

    if not subtitle:
        for sep in (" - ", " : "):
            if sep in title:
                t, s            = title.split(sep, 1)
                title, subtitle = t.strip(), s.strip()
                break

    author_str     = item.get("author", "")
    authors        = parse_authors(author_str)
    item_id        = str(item.get("itemId", ""))
    has_translator = any(a["role"] in TRANS_ROLES for a in authors)

    # 원제·원저자 수집
    orig_title:     str | None = None
    orig_author_en: str | None = None
    author_info:    list       = []

    if has_translator:
        orig_info      = collect_orig_info(item, item_id, isbn13, title, authors)
        orig_title     = orig_info["orig_title"]
        orig_author_en = orig_info["orig_author_en"]
        author_info    = orig_info["author_info"]
        if not orig_author_en:
            blob = aladin_item_description_blob(item)
            orig_author_en = extract_english_author_after_korean_paren(blob)

    # MARC 필드 생성
    field_245 = build_245(title, subtitle, authors)
    field_246 = build_246(orig_title)
    translation_book = field_246 is not None

    # 500: 원저자명(한자·영문만, 수집 실패 시 생략).
    orig_names_500 = []
    for ai in author_info:
        kanji = ai["kanji_name"]
        if kanji:
            orig_names_500.append(kanji)
        elif orig_author_en:
            if translation_book:
                orig_names_500.append(orig_author_en.strip())
            elif not ai["is_east_asian"]:
                orig_names_500.append(orig_author_en.strip())
    if not orig_names_500 and orig_author_en:
        orig_names_500.append(orig_author_en.strip())
    field_500 = f"500 __ $a 원저자명: {', '.join(orig_names_500)}" if orig_names_500 else None

    # 700 — 246 있으면 원저 영문 부출은 성·이름 도치(한자 원저라도 영문 있으면 부출).
    persons    = [a for a in authors if not a["is_org"]]
    fields_700 = []
    first_ai   = author_info[0] if author_info else None
    skip_first_person_700 = False
    if first_ai and orig_author_en:
        rom = orig_author_en.strip()
        latin = bool(re.search(r"[A-Za-z]", rom)) and not re.search(r"[\uac00-\ud7a3]", rom)
        if latin and (not first_ai.get("kanji_name") or translation_book):
            fields_700.append(f"700 1_ $a {english_name_reverse(rom)}")
        elif translation_book and not latin and not first_ai.get("kanji_name"):
            kr = korean_name_reverse(first_ai["name"].strip())
            if kr:
                fields_700.append(f"700 1_ $a {kr}")
                skip_first_person_700 = True
    elif first_ai and translation_book and not first_ai.get("kanji_name") and not orig_author_en:
        kr = korean_name_reverse(first_ai["name"].strip())
        if kr:
            fields_700.append(f"700 1_ $a {kr}")
            skip_first_person_700 = True

    first_ko_name = (first_ai or {}).get("name", "").strip()
    for a in persons:
        if skip_first_person_700 and a["name"].strip() == first_ko_name:
            skip_first_person_700 = False
            continue
        fields_700.append(f"700 1_ {build_700(a)}")

    # 710
    orgs       = [a for a in authors if a["is_org"]]
    fields_710 = [f"710 0_ {build_710(a)}" for a in orgs]

    # 900
    fields_900 = []
    for ai in author_info:
        en_for_900 = orig_author_en if not ai.get("kanji_name") else None
        f900 = build_900(
            ai["name"],
            ai["is_east_asian"],
            ai["kanji_name"],
            orig_author_en=en_for_900,
            translation_book=translation_book,
        )
        if f900:
            fields_900.append(f900)

    return jsonify({
        "isbn13":     isbn13,
        "title":      title,
        "subtitle":   subtitle,
        "author_raw": author_str,
        "authors":    authors,
        "publisher":  item.get("publisher", ""),
        "pub_date":   item.get("pubDate", ""),
        "cover":      item.get("cover", ""),
        "marc": {
            "f245": f"245 00 {field_245}",
            "f246": field_246,
            "f500": field_500,
            "f700": fields_700,
            "f710": fields_710,
            "f900": fields_900,
        }
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
