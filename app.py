"""
KORMARC 자동 생성기 - Flask 백엔드 (Render 배포용)
알라딘 API를 이용해 ISBN으로 도서 정보를 조회하고
KORMARC 245, 700, 710 필드를 자동 생성합니다.

[245 $c 책임표시사항 구성 규칙]
  /$c 첫번째저자
  ,$e 두번째저자 (공동저자, 반복)
  ;$e 역자·그린이 등 역할어 다른 저자

[700 / 710 구성 규칙]
  700 1_  $a 개인명,     ← 개인 부출기입 ($e 역할어 표시 없음)
  710 0_  $a 기관명.     ← 기관·단체·협의회 등
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import os

app = Flask(__name__)

# Streamlit 프론트엔드에서 오는 요청 허용
CORS(app)

ALADIN_API_KEY = os.environ.get("ALADIN_API_KEY", "ttbboyeong09010919001")
ALADIN_API_URL = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"

# ─────────────────────────────────────────────
# 기관·단체 판별 키워드
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

# 역할어 → 표시 문자열
ROLE_LABEL = {
    "옮긴이": "옮긴이",
    "역자":   "옮긴이",
    "번역":   "옮긴이",
    "그린이": "그린이",
    "그림":   "그린이",
    "일러스트": "그린이",
    "사진":   "사진",
    "감수":   "감수",
    "편저":   "편저",
    "편역":   "편역",
    "엮은이": "엮은이",
    "편집":   "엮은이",
    "해설":   "해설",
}

PRIMARY_ROLES = {"지은이", "저자", "글", "글쓴이", ""}


def is_org(name: str) -> bool:
    nl = name.lower()
    return any(kw in nl for kw in ORG_KEYWORDS)


def parse_authors(author_str: str) -> list[dict]:
    result = []
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


def build_245(title: str, subtitle: str, authors: list[dict]) -> str:
    a_part = title.strip()
    b_part = subtitle.strip() if subtitle else ""

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
        all_names = [name for names in role_groups.values() for name in names]
        field += f" /$d {all_names[0]}"
        for name in all_names[1:]:
            field += f" ,$e {name}"
        field += "."
    else:
        field += "."

    return field


def build_700(author: dict) -> str:
    name = author["name"].strip()
    if re.search(r"[A-Za-z]", name) and not re.search(r"[\uac00-\ud7a3]", name):
        parts = name.split()
        if len(parts) >= 2:
            name = f"{parts[-1]}, {' '.join(parts[:-1])}"
    return f"$a {name},"


def build_710(author: dict) -> str:
    return f"$a {author['name'].strip()}."


def to_isbn13(isbn: str) -> str:
    if len(isbn) == 13:
        return isbn
    base = "978" + isbn[:9]
    check = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(base))
    return base + str((10 - check % 10) % 10)


def fetch_aladin(isbn: str) -> dict:
    params = {
        "ttbkey": ALADIN_API_KEY,
        "itemIdType": "ISBN13",
        "ItemId": isbn,
        "output": "js",
        "Version": "20131101",
        "OptResult": "authors,subInfo",
    }
    resp = requests.get(ALADIN_API_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("item"):
        raise ValueError("도서를 찾을 수 없습니다.")
    return data["item"][0]


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

    # 표제 / 부제목 분리
    title = item.get("title", "")
    subtitle = ""

    # 1순위: 알라딘 subInfo.subTitle
    sub_info = item.get("subInfo", {})
    if isinstance(sub_info, dict):
        api_sub = sub_info.get("subTitle", "").strip()
        if api_sub and title.endswith(api_sub):
            title = title[: -len(api_sub)].rstrip(" -:").strip()
            subtitle = api_sub
        elif api_sub:
            subtitle = api_sub

    # 2순위: 구분자로 분리
    if not subtitle:
        for sep in (" - ", " : "):
            if sep in title:
                t, s = title.split(sep, 1)
                title, subtitle = t.strip(), s.strip()
                break

    author_str = item.get("author", "")
    authors = parse_authors(author_str)

    field_245 = build_245(title, subtitle, authors)

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
            "f700": fields_700,
            "f710": fields_710,
        }
    })


# Render 헬스체크용
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
