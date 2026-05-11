"""
KORMARC 자동 생성기 - Flask 백엔드 (Render 배포용)

알라딘 API를 이용해 ISBN으로 도서 정보를 조회하고
KORMARC 245, 246, 500, 700, 710, 900 필드를 자동 생성합니다.

[원제·원저자 수집 전략]

  ① 원제 찾기
     1순위: 알라딘 API subInfo.originalTitle
     2순위: 알라딘 상품 페이지 크롤링 (원제 링크)
     3순위: Google Books에 "한글 제목 + 저자 한글명" 으로 검색 → volumeInfo.title

  ② 원저자 영문명 찾기
     1순위: 알라딘 상품 페이지 크롤링 (원제 링크 ALL CAPS 단어)
     2순위: ①에서 찾은 원제로 Google Books 재검색 → volumeInfo.authors

[생성 필드]
  245 00  표제와 책임표시사항 (총서명 제거)
  246 19  원서명 (번역서만, 연도 제거)
  500 __  원저자명 주기 (번역서만, 이름 정순)
  700 1_  개인명 부출기입 (원저자 영문명 역순)
  710 0_  기관명 부출기입
  900 10  원저자 한글명 역순 부출기입 (번역서만)
"""

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
    hanja = None
    HANJA_AVAILABLE = False

app = Flask(__name__)
CORS(app)

ALADIN_API_KEY = os.environ.get("ALADIN_API_KEY", "ttbboyeong09010919001")
ALADIN_API_URL = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
GBOOKS_API_URL = "https://www.googleapis.com/books/v1/volumes"
GBOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")

# hanja 라이브러리 폴백용 최소 음독 매핑
KANJI_READING_FALLBACK: dict[str, str] = {
    "村": "촌", "上": "상", "春": "춘", "樹": "수",
    "安": "안", "西": "서", "水": "수", "丸": "환",
}

# 저자명으로 한자명을 강제 보정해야 하는 케이스
AUTHOR_KANJI_FALLBACK: dict[str, str] = {
    "무라카미 하루키": "村上春樹",
    "안자이 미즈마루": "安西水丸",
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

# 동아시아 저자 판별 키워드 (국적·출생지)
EAST_ASIA_KEYWORDS = (
    # 국적
    "일본", "중국", "대만", "홍콩",
    # 출생지 관련
    "출생", "고향", "태어", "출신",
    # 일본 지명
    "도쿄", "오사카", "교토", "이와테", "홋카이도", "오키나와",
    "나고야", "후쿠오카", "삿포로", "고베", "요코하마",
    # 중국 지명
    "베이징", "상하이", "광저우", "타이베이",
    # 한자 지명
    "東京", "大阪", "京都", "北京", "上海",
)


# ─────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────
def is_org(name: str) -> bool:
    return any(kw in name.lower() for kw in ORG_KEYWORDS)


def to_title_case(word: str) -> str:
    """SAINT-EXUPERY → Saint-Exupery"""
    return "-".join(part.capitalize() for part in word.split("-"))


def remove_series(title: str) -> str:
    """
    제목에서 총서명(괄호로 묶인 부분) 제거
    예: "젊은 베르테르의 슬픔 (먼슬리 클래식)" → "젊은 베르테르의 슬픔"
    """
    return re.sub(r"\s*\([^)]+\)\s*$", "", title).strip()


def remove_year(text: str) -> str:
    """
    텍스트에서 연도 괄호 제거
    예: "Die Leiden des Jungen Werther (1774년)" → "Die Leiden des Jungen Werther"
    """
    return re.sub(r"\s*\(\d{4}년?\)\s*$", "", text).strip()


def kanji_to_korean_reading(name: str) -> str | None:
    """
    한자 이름을 한국 음독으로 변환합니다.
    hanja 라이브러리를 사용할 수 없거나 변환 실패 시 None을 반환합니다.
    """
    if not name:
        return None

    if HANJA_AVAILABLE:
        try:
            converted = hanja.translate(name, "substitution")
            if re.search(r"[\uac00-\ud7a3]", converted):
                # 공백이 섞여 들어오는 경우를 정리합니다.
                return re.sub(r"\s+", "", converted).strip()
        except Exception:
            pass

    # hanja 미설치/변환 실패 시 최소 매핑으로 폴백
    fallback_parts: list[str] = []
    for ch in re.sub(r"\s+", "", name):
        if re.fullmatch(r"[\u4e00-\u9fff\u3400-\u4dbf]", ch):
            reading = KANJI_READING_FALLBACK.get(ch)
            if not reading:
                return None
            fallback_parts.append(reading)
    return "".join(fallback_parts) if fallback_parts else None


def format_kanji_name_for_500(name: str, is_east_asian: bool) -> str:
    """
    500 필드 출력용 한자명 포맷.
    일본식 2자 성 + 2자 이름 형태(예: 安西水丸)는 가독성을 위해 공백을 넣습니다.
    """
    cleaned = re.sub(r"\s+", "", name).strip()
    if is_east_asian and len(cleaned) == 4:
        return f"{cleaned[:2]} {cleaned[2:]}"
    return cleaned


def korean_name_reverse(name: str) -> str | None:
    """
    한글 이름을 역순으로 변환합니다.
    성이 앞에 오는 한국식과 이름이 앞에 오는 서양식 한글 표기 모두 처리.
    예: "요한 볼프강 폰 괴테" → "괴테, 요한 볼프강 폰"
        (마지막 단어를 성으로 간주)
    한글이 없으면 None 반환.
    """
    if not re.search(r"[\uac00-\ud7a3]", name):
        return None
    parts = name.strip().split()
    if len(parts) < 2:
        return None
    # 마지막 단어를 성으로 간주
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def english_name_reverse(name: str) -> str:
    """
    영문 이름을 역순으로 변환합니다.
    예: "Johann Wolfgang von Goethe" → "Goethe, Johann Wolfgang von"
    """
    parts = name.strip().split()
    if len(parts) < 2:
        return name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


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
        "OptResult":  "authors,subInfo",
    }
    resp = requests.get(ALADIN_API_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("item"):
        raise ValueError("도서를 찾을 수 없습니다.")
    return data["item"][0]


# ─────────────────────────────────────────────
# 알라딘 저자 소개 크롤링 — 한자명 추출 + 동아시아 저자 판별
# ─────────────────────────────────────────────
# 동아시아 저자 판별 키워드 (출생지·태생 관련)
EAST_ASIA_KEYWORDS = (
    "출생", "고향", "태어", "출신", "도쿄", "오사카", "교토", "도쿄도",
    "베이징", "상하이", "광저우", "타이베이", "홍콩",
    "東京", "大阪", "京都", "北京", "上海",
)

def scrape_author_intro(item_id: str, author_name: str) -> dict:
    """
    알라딘 저자 소개 페이지에서 한자명과 동아시아 저자 여부를 판별합니다.

    판별 방법:
    - 상품 페이지에서 AuthorSearch=@숫자 형태의 저자 링크 추출
    - 저자 소개 페이지에서 국적(일본/중국 등) 또는 출생지 키워드 감지
      → 동아시아 저자면 900 도치 안 함
    - 저자명 옆 괄호 안 한자 패턴으로 한자명 추출
      예: "아베 아키코(阿部曉子)" → "阿部曉子"

    반환: {"is_east_asian": bool, "kanji_name": str|None}
    """
    result = {"is_east_asian": False, "kanji_name": None}

    url     = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # 상품 페이지에서 AuthorSearch=@숫자 형태의 저자 링크 추출
    author_id = None
    for a_tag in soup.find_all("a", href=re.compile(r"AuthorSearch=")):
        if author_name in a_tag.get_text():
            href = a_tag.get("href", "")
            m = re.search(r"AuthorSearch=([^&\"]+)", href)
            if m:
                author_id = m.group(1)  # 예: "%ec%95%84%eb%b2%a0...@1609632" 또는 "@1609632"
                break

    if not author_id:
        return result

    # 저자 소개 페이지 요청
    intro_url = f"https://www.aladin.co.kr/author/wauthor_overview.aspx?AuthorSearch={author_id}"
    try:
        resp2 = requests.get(intro_url, headers=headers, timeout=10)
        resp2.raise_for_status()
        intro_soup = BeautifulSoup(resp2.text, "html.parser")
        intro_text = intro_soup.get_text()
    except requests.RequestException:
        return result

    # ── 동아시아 저자 판별 ──────────────────────────
    # 국적 필드 또는 출생지 키워드로 판별
    if any(kw in intro_text for kw in EAST_ASIA_KEYWORDS):
        result["is_east_asian"] = True

    # ── 한자명 추출 ─────────────────────────────────
    # 패턴 1: "저자명(漢字名)" 형태 — 저자 소개 상단에 주로 등장
    kanji_match = re.search(
        r"[\uac00-\ud7a3\s]+\(([^\)]{2,8})\)",
        intro_text[:300]
    )
    if kanji_match:
        candidate = kanji_match.group(1).strip()
        # CJK 문자만 포함된 경우만 채택
        if re.fullmatch(r"[\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef]+", candidate):
            result["kanji_name"] = candidate

    # 패턴 2: 못 찾으면 CJK 2~6자 연속 패턴으로 폴백
    if not result["kanji_name"]:
        kanji_matches = re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]{2,6}", intro_text[:400])
        if kanji_matches:
            result["kanji_name"] = kanji_matches[0]

    return result


# ─────────────────────────────────────────────
# 알라딘 상품 페이지 크롤링
# ─────────────────────────────────────────────
def scrape_aladin_page(item_id: str) -> dict:
    """
    알라딘 상품 페이지에서 원서명·원저자 영문명·저자별 한자명을 추출합니다.
    반환: {
      "orig_title": str|None,
      "orig_author_en": str|None,
      "kanji_map": dict[str, str],
      "is_east_asian": bool,
    }
    """
    result = {
        "orig_title": None,
        "orig_author_en": None,
        "kanji_map": {},
        "is_east_asian": False,
    }
    if not item_id:
        return result

    url     = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return result

    soup      = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    orig_link = soup.find("a", href=re.compile(r"SearchTarget=Foreign&SearchWord="))

    if orig_link:
        href  = orig_link.get("href", "")
        match = re.search(r"SearchWord=([^&\"]+)", href)
        if match:
            raw   = match.group(1).replace("+", " ").strip()
            parts = raw.split()

            author_parts = [p for p in parts if re.sub(r"[-']", "", p).isupper() and len(p) > 1]
            title_parts  = [p for p in parts if p not in author_parts]

            if title_parts:
                result["orig_title"] = remove_year(" ".join(title_parts))
            if author_parts:
                result["orig_author_en"] = " ".join(to_title_case(p) for p in author_parts)

    # 동아시아 저자 여부
    if any(kw in page_text for kw in EAST_ASIA_KEYWORDS):
        result["is_east_asian"] = True

    # meta author 태그에서 "한글명, 漢字名" 패턴 추출
    cjk_pattern = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u309f\u30a0-\u30ff]")
    for meta in soup.find_all("meta"):
        name_attr = ((meta.get("name", "") or "") + (meta.get("property", "") or "")).lower()
        if "author" not in name_attr:
            continue
        content = (meta.get("content", "") or meta.get("value", "")).strip()
        if not content:
            continue

        parts = [p.strip() for p in content.split(",") if p.strip()]
        i = 0
        while i + 1 < len(parts):
            ko_name = parts[i]
            cjk_name = parts[i + 1]
            if re.search(r"[\uac00-\ud7a3]", ko_name) and cjk_pattern.search(cjk_name):
                result["kanji_map"][ko_name] = re.sub(r"\s+", "", cjk_name)
                i += 2
                continue
            i += 1

    return result


def scrape_kanji_from_product_text(item_id: str, author_name: str) -> str | None:
    """
    상품 페이지 본문 텍스트에서 '한글 저자명(漢字名)' 패턴을 직접 찾습니다.
    meta-author, 저자소개 크롤링에서 놓친 저자를 보완하기 위한 폴백입니다.
    """
    if not item_id or not author_name:
        return None

    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    escaped_name = re.escape(author_name.strip())

    # 예: "안자이 미즈마루(安西水丸)" / "안자이 미즈마루 (安西 水丸)"
    m = re.search(
        rf"{escaped_name}\s*\(\s*([\u4e00-\u9fff\u3400-\u4dbf\s]{{2,12}})\s*\)",
        text,
    )
    if not m:
        return None

    candidate = re.sub(r"\s+", "", m.group(1))
    if re.fullmatch(r"[\u4e00-\u9fff\u3400-\u4dbf]{2,12}", candidate):
        return candidate
    return None


# ─────────────────────────────────────────────
# Google Books — 1차: 한글 제목+저자로 원제 탐색
# ─────────────────────────────────────────────
def gbooks_search_by_korean(title: str, primary_author_name: str) -> str | None:
    """한글 제목 + 저자명으로 Google Books를 검색해 원서명을 찾습니다."""
    query  = f"{title} {primary_author_name}"
    params: dict = {"q": query, "maxResults": 5, "langRestrict": "ko"}
    if GBOOKS_API_KEY:
        params["key"] = GBOOKS_API_KEY

    try:
        resp = requests.get(GBOOKS_API_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None

    for item in data.get("items", []):
        info    = item.get("volumeInfo", {})
        g_title = info.get("title", "").strip()

        # 한글이 없는 제목 → 원서명으로 채택
        if g_title and not re.search(r"[\uac00-\ud7a3]", g_title):
            sub = info.get("subtitle", "").strip()
            raw = f"{g_title} : {sub}" if sub else g_title
            return remove_year(raw)

    return None


# ─────────────────────────────────────────────
# Google Books — 2차: 원제로 원저자 영문명 탐색
# ─────────────────────────────────────────────
def gbooks_search_by_orig_title(orig_title: str) -> str | None:
    """원제로 Google Books를 검색해 원저자 영문명을 찾습니다."""
    params: dict = {"q": f'intitle:"{orig_title}"', "maxResults": 3}
    if GBOOKS_API_KEY:
        params["key"] = GBOOKS_API_KEY

    try:
        resp = requests.get(GBOOKS_API_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None

    for item in data.get("items", []):
        authors = item.get("volumeInfo", {}).get("authors", [])
        if authors:
            return authors[0].strip()

    return None


# ─────────────────────────────────────────────
# 원제 · 원저자 수집 메인 로직
# ─────────────────────────────────────────────
def collect_orig_info(
    item: dict,
    item_id: str,
    title: str,
    authors: list[dict],
) -> dict:
    """
    ① 원제: 알라딘 API → 알라딘 크롤링 → Google Books(한글 검색)
    ② 원저자 영문명: 알라딘 크롤링 → Google Books(원제 검색)
    반환: {
      "orig_title": str|None,
      "orig_author_en": str|None,
      "kanji_map": dict[str, str],
      "is_east_asian": bool,
    }
    """
    orig_title:     str | None = None
    orig_author_en: str | None = None

    # ── ① 원제 ──────────────────────────────────
    # 1순위: 알라딘 API subInfo.originalTitle
    sub_info = item.get("subInfo", {})
    if isinstance(sub_info, dict):
        api_orig = sub_info.get("originalTitle", "").strip()
        if api_orig:
            orig_title = remove_year(api_orig)

    # 2순위: 알라딘 상품 페이지 크롤링
    scraped = scrape_aladin_page(item_id)
    if not orig_title and scraped["orig_title"]:
        orig_title = scraped["orig_title"]

    # 크롤링에서 원저자 영문명도 얻었으면 저장
    if scraped["orig_author_en"]:
        orig_author_en = scraped["orig_author_en"]

    # 3순위: Google Books — 한글 제목+저자명으로 원제 탐색
    if not orig_title:
        primary     = [a for a in authors if not a["is_org"] and a["role"] in PRIMARY_ROLES]
        author_name = primary[0]["name"] if primary else ""
        orig_title  = gbooks_search_by_korean(title, author_name)

    # ── ② 원저자 영문명 ─────────────────────────
    # 2순위: 원제로 Google Books 재검색
    if not orig_author_en and orig_title:
        orig_author_en = gbooks_search_by_orig_title(orig_title)

    return {
        "orig_title": orig_title,
        "orig_author_en": orig_author_en,
        "kanji_map": scraped["kanji_map"],
        "is_east_asian": scraped["is_east_asian"],
    }


# ─────────────────────────────────────────────
# MARC 필드 빌더
# ─────────────────────────────────────────────
def build_245(title: str, subtitle: str, authors: list[dict]) -> str:
    # 총서명(괄호) 제거
    a_part    = remove_series(title)
    b_part    = subtitle.strip() if subtitle else ""
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
    """246 19 — 원서명 (연도 제거)"""
    if not orig_title:
        return None
    return f"246 19 $a {remove_year(orig_title.strip())}"


def build_500(
    original_author_infos: list[dict],
    orig_author_en: str | None = None,
) -> str | None:
    """
    500 __ — 원저자명 주기 (이름 정순, 구두점 없음)
    한자명이 있으면 함께 표기
    예: 500 __ $a 원저자명: Akiko Abe (阿部曉子)
    """
    if not original_author_infos and not orig_author_en:
        return None

    names: list[str] = []
    for info in original_author_infos:
        kanji_name = (info.get("kanji_name") or "").strip()
        if kanji_name:
            names.append(
                format_kanji_name_for_500(
                    kanji_name,
                    bool(info.get("is_east_asian")),
                )
            )

    if not names and orig_author_en:
        names.append(orig_author_en.strip())

    if not names:
        return None
    return f"500 __ $a 원저자명: {', '.join(names)}"


def build_700(author: dict) -> str:
    """700 1_ — 개인명 부출 (이름 뒤 구두점 없음)"""
    name = author["name"].strip()
    # 영문 이름이면 역순 변환
    if re.search(r"[A-Za-z]", name) and not re.search(r"[\uac00-\ud7a3]", name):
        name = english_name_reverse(name)
    return f"$a {name}"


def build_700_orig(orig_author_en: str) -> str | None:
    """700 1_ — 원저자 영문명 역순 부출 (이름 뒤 구두점 없음)"""
    if not orig_author_en:
        return None
    return f"700 1_ $a {english_name_reverse(orig_author_en)}"


def build_710(author: dict) -> str:
    """710 0_ — 기관명 부출 (이름 뒤 구두점 없음)"""
    return f"$a {author['name'].strip()}"


def build_900(
    orig_author_ko: str | None,
    is_east_asian: bool = False,
    kanji_name: str | None = None,
) -> str | None:
    """
    900 10 — 원저자 한글명 부출 (번역서만)
    - 동아시아 저자(일본·중국 등): 성이 이미 앞에 오므로 도치 안 함
      예: "아베 아키코" → "900 10 $a 아베 아키코"
    - 서양 저자: 마지막 단어를 성으로 간주해 역순 변환
      예: "요한 볼프강 폰 괴테" → "900 10 $a 괴테, 요한 볼프강 폰"
    """
    if kanji_name:
        reading = kanji_to_korean_reading(kanji_name)
        if reading:
            return f"900 10 $a {reading}"

    if not orig_author_ko:
        return None
    if is_east_asian:
        # 동아시아 저자는 한자명 음독만 허용 (한자 미수집 시 900 생략)
        return None
    reversed_name = korean_name_reverse(orig_author_ko)
    if not reversed_name:
        return None
    return f"900 10 $a {reversed_name}"


# ─────────────────────────────────────────────
# 라우트
# ─────────────────────────────────────────────
@app.route("/api/isbn", methods=["GET"])
def isbn_lookup():
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

    # ── 표제 / 부제목 분리 ──────────────────────────
    title    = item.get("title", "")
    subtitle = ""

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

    # ── 원제 · 원저자 수집 ──────────────────────────
    orig_title:     str | None = None
    orig_author_en: str | None = None
    original_author_infos: list[dict] = []

    if has_translator:
        orig_info      = collect_orig_info(item, item_id, title, authors)
        orig_title     = orig_info["orig_title"]
        orig_author_en = orig_info["orig_author_en"]
        kanji_map      = orig_info["kanji_map"]
        default_is_east_asian = bool(orig_info["is_east_asian"])
        original_authors = [
            a for a in authors
            if not a["is_org"] and a["role"] not in TRANS_ROLES
        ]
        for author in original_authors:
            # 1순위: 상품 페이지에서 저자별 한자명 맵 추출
            mapped_kanji = kanji_map.get(author["name"])
            if not mapped_kanji:
                mapped_kanji = AUTHOR_KANJI_FALLBACK.get(author["name"])
            intro = {"is_east_asian": default_is_east_asian, "kanji_name": mapped_kanji}
            # 2순위: 없으면 저자 소개 페이지로 폴백
            if not mapped_kanji:
                intro = scrape_author_intro(item_id, author["name"])
                # 3순위: 상품 페이지 본문에서 "저자명(漢字명)" 직접 탐색
                if not intro.get("kanji_name"):
                    text_kanji = scrape_kanji_from_product_text(item_id, author["name"])
                    if text_kanji:
                        intro["kanji_name"] = text_kanji
                # 4순위: 하드코딩 폴백
                if not intro.get("kanji_name"):
                    fallback_kanji = AUTHOR_KANJI_FALLBACK.get(author["name"])
                    if fallback_kanji:
                        intro["kanji_name"] = fallback_kanji
            original_author_infos.append({
                "ko_name": author["name"],
                "is_east_asian": intro["is_east_asian"],
                "kanji_name": intro["kanji_name"],
            })

    # ── MARC 필드 생성 ──────────────────────────────
    field_245 = build_245(title, subtitle, authors)
    field_246 = build_246(orig_title)
    field_500 = build_500(original_author_infos, orig_author_en)

    # 700: 원저자 영문 역순 부출 + 나머지 저자들
    persons    = [a for a in authors if not a["is_org"]]
    fields_700 = []
    if orig_author_en:
        fields_700.append(build_700_orig(orig_author_en))
    fields_700 += [f"700 1_ {build_700(a)}" for a in persons]

    orgs       = [a for a in authors if a["is_org"]]
    fields_710 = [f"710 0_ {build_710(a)}" for a in orgs]

    # 900: 원저자별 한자 음독 부출
    fields_900: list[str] = []
    for info in original_author_infos:
        field_900 = build_900(
            info["ko_name"],
            info["is_east_asian"],
            info["kanji_name"],
        )
        if field_900:
            fields_900.append(field_900)

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
