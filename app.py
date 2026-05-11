"""
KORMARC 자동 생성기 - Flask 백엔드 (Render 배포용)

알라딘 API를 이용해 ISBN으로 도서 정보를 조회하고
KORMARC 245, 246, 500, 700, 710 필드를 자동 생성합니다.

[데이터 소스 우선순위]
  1순위: 알라딘 API + 상품 페이지 크롤링
  2순위: Google Books API (알라딘에 정보 없을 때 폴백)

[생성 필드]
  245 00  표제와 책임표시사항
  246 19  원서명 (번역서만)
  500 __  원저자명 주기 (번역서만)
  700 1_  개인명 부출기입
  710 0_  기관명 부출기입

[245 $c 책임표시사항 구성 규칙]
  /$d 첫번째저자
  ,$e 두번째저자 (공동저자, 반복)
  ;$e 역자·그린이 등 역할어 다른 저자

[246 구성 규칙]
  역자가 있을 때만 생성
  알라딘 원제 → Google Books title 순으로 폴백
  246 19 $a 원서명.

[500 구성 규칙]
  역자가 있을 때만 생성
  알라딘 상품 페이지 크롤링 → Google Books authors 순으로 폴백
  500 __ $a 원저자명: Antoine De Saint-Exupery.

[700 / 710 구성 규칙]
  700 1_ $a 개인명, ← 개인 부출기입
  710 0_ $a 기관명. ← 기관·단체·협의회 등
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import os

app = Flask(__name__)
CORS(app)

ALADIN_API_KEY = os.environ.get("ALADIN_API_KEY", "ttbboyeong09010919001")
ALADIN_API_URL = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
GBOOKS_API_URL = "https://www.googleapis.com/books/v1/volumes"
GBOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")  # 없어도 하루 1000건 무료

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


# ─────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────
def is_org(name: str) -> bool:
    return any(kw in name.lower() for kw in ORG_KEYWORDS)


def to_title_case(word: str) -> str:
    """SAINT-EXUPERY → Saint-Exupery"""
    return "-".join(part.capitalize() for part in word.split("-"))


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
# 알라딘 상품 페이지 크롤링
# ─────────────────────────────────────────────
def scrape_aladin_page(item_id: str) -> dict:
    """
    알라딘 상품 페이지의 '원제' 링크에서 원서명·원저자 영문명을 추출합니다.

    링크 형태:
      href="...SearchTarget=Foreign&SearchWord=Le+Petit+Prince+ANTOINE+DE+SAINT-EXUPERY"

    - ALL CAPS 단어 → 원저자명
    - 그 외 단어    → 원서명
    """
    result = {"orig_title": None, "orig_author_en": None}
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
    orig_link = soup.find("a", href=re.compile(r"SearchTarget=Foreign&SearchWord="))

    if orig_link:
        href  = orig_link.get("href", "")
        match = re.search(r"SearchWord=([^&\"]+)", href)
        if match:
            raw   = match.group(1).replace("+", " ").strip()
            parts = raw.split()

            # ALL CAPS(하이픈 허용) 단어 → 저자명
            author_parts = [p for p in parts if re.sub(r"[-']", "", p).isupper() and len(p) > 1]
            title_parts  = [p for p in parts if p not in author_parts]

            if title_parts:
                result["orig_title"] = " ".join(title_parts)
            if author_parts:
                result["orig_author_en"] = " ".join(to_title_case(p) for p in author_parts)

    return result


# ─────────────────────────────────────────────
# Google Books API 폴백
# ─────────────────────────────────────────────
def fetch_google_books(isbn13: str) -> dict:
    """
    Google Books API로 원서명·원저자 영문명을 조회합니다.
    알라딘에서 정보를 못 가져왔을 때만 호출됩니다.

    반환: {"orig_title": str|None, "orig_author_en": str|None}
    """
    result = {"orig_title": None, "orig_author_en": None}

    params: dict = {"q": f"isbn:{isbn13}"}
    if GBOOKS_API_KEY:
        params["key"] = GBOOKS_API_KEY

    try:
        resp = requests.get(GBOOKS_API_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return result

    items = data.get("items", [])
    if not items:
        return result

    info       = items[0].get("volumeInfo", {})
    g_title    = info.get("title", "").strip()
    g_subtitle = info.get("subtitle", "").strip()
    g_authors  = info.get("authors", [])

    if g_title:
        result["orig_title"] = f"{g_title} : {g_subtitle}" if g_subtitle else g_title

    if g_authors:
        result["orig_author_en"] = g_authors[0].strip()

    return result


# ─────────────────────────────────────────────
# MARC 필드 빌더
# ─────────────────────────────────────────────
def build_245(title: str, subtitle: str, authors: list[dict]) -> str:
    a_part    = title.strip()
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
        field += "."
    elif role_groups:
        all_names = [n for ns in role_groups.values() for n in ns]
        field += f" /$d {all_names[0]}"
        for name in all_names[1:]:
            field += f" ,$e {name}"
        field += "."
    else:
        field += "."

    return field


def build_246(orig_title: str | None) -> str | None:
    """246 19 — 원서명 (번역서 + 원제 있을 때만)"""
    if not orig_title:
        return None
    return f"246 19 $a {orig_title.strip()}."


def build_500(orig_author_en: str | None) -> str | None:
    """500 __ — 원저자명 주기 (번역서 + 영문명 있을 때만)"""
    if not orig_author_en:
        return None
    return f"500 __ $a 원저자명: {orig_author_en.strip()}."


def build_700(author: dict) -> str:
    name = author["name"].strip()
    # 영문 이름이면 성, 이름 역순 변환
    if re.search(r"[A-Za-z]", name) and not re.search(r"[\uac00-\ud7a3]", name):
        parts = name.split()
        if len(parts) >= 2:
            name = f"{parts[-1]}, {' '.join(parts[:-1])}"
    return f"$a {name},"


def build_710(author: dict) -> str:
    return f"$a {author['name'].strip()}."


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

    # ── 원제 · 원저자 영문명 수집 ───────────────────
    orig_title:     str | None = None
    orig_author_en: str | None = None

    if has_translator:
        # 1순위: 알라딘 상품 페이지 크롤링
        scraped        = scrape_aladin_page(item_id)
        orig_title     = scraped["orig_title"]
        orig_author_en = scraped["orig_author_en"]

        # 2순위: 없는 항목만 Google Books로 폴백
        if not orig_title or not orig_author_en:
            gbooks = fetch_google_books(isbn13)
            if not orig_title:
                orig_title = gbooks["orig_title"]
            if not orig_author_en:
                orig_author_en = gbooks["orig_author_en"]

    # ── MARC 필드 생성 ──────────────────────────────
    field_245 = build_245(title, subtitle, authors)
    field_246 = build_246(orig_title)      # 없으면 None
    field_500 = build_500(orig_author_en)  # 없으면 None

    persons    = [a for a in authors if not a["is_org"]]
    fields_700 = [f"700 1_ {build_700(a)}" for a in persons]

    orgs       = [a for a in authors if a["is_org"]]
    fields_710 = [f"710 0_ {build_710(a)}" for a in orgs]

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
            "f246": field_246,   # 번역서 + 원제 있을 때만, 없으면 null
            "f500": field_500,   # 번역서 + 원저자 영문명 있을 때만, 없으면 null
            "f700": fields_700,
            "f710": fields_710,
        }
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
