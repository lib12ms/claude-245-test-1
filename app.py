from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import os
import urllib.request
import urllib.error

app = Flask(__name__)
CORS(app)

ALADIN_API_KEY = os.environ.get("ALADIN_API_KEY", "ttbboyeong09010919001")
ALADIN_API_URL = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"

ORG_KEYWORDS = [
    "협회", "학회", "위원회", "연구소", "연구원", "연구회", "센터",
    "재단", "법인", "기관", "청", "공단", "공사", "협의회", "연합회",
    "연맹", "조합", "대학교", "대학", "학교", "출판사", "출판부",
    "association", "institute", "council", "committee", "foundation",
    "university", "society", "organization", "corp", "inc", "ltd",
]

ROLE_LABEL = {
    "옮긴이": "옮긴이", "역자": "옮긴이", "번역": "옮긴이",
    "그린이": "그린이", "그림": "그린이", "일러스트": "그린이",
    "사진": "사진", "감수": "감수", "편저": "편저", "편역": "편역",
    "엮은이": "엮은이", "편집": "엮은이", "해설": "해설",
}

PRIMARY_ROLES = {"지은이", "저자", "글", "글쓴이", ""}
PRIMARY_LABEL = {
    "지은이": "지은이", "저자": "지은이",
    "글": "지은이", "글쓴이": "지은이", "": "지은이",
}


def is_org(name):
    return any(kw in name.lower() for kw in ORG_KEYWORDS)


def is_korean(name):
    return bool(re.search(r"[\uac00-\ud7a3]", name))


def is_western(name):
    return bool(re.search(r"[A-Za-z]", name)) and not is_korean(name)


def invert_western(name):
    parts = name.strip().split()
    return parts[-1] + ", " + " ".join(parts[:-1]) if len(parts) >= 2 else name


def invert_korean(name):
    parts = name.strip().split()
    return parts[-1] + ", " + " ".join(parts[:-1]) if len(parts) >= 2 else name


def extract_original_names_from_aladin_page(link, names):
    """
    알라딘 상품 페이지와 저자 소개 페이지를 크롤링해서 원어명 추출.
    예: '시그리드 누네즈 (Sigrid Nunez)' 패턴 감지
    """
    if not link or not names:
        return {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    result = {}
    target_names = [n for n in names if not is_org(n)]

    # 1차: 상품 페이지에서 직접 추출
    try:
        req = urllib.request.Request(link, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        for name in target_names:
            pattern = re.compile(
                rf"{re.escape(name)}\s*\(\s*([A-Za-z][A-Za-z .,'-]+)\s*\)",
                re.IGNORECASE
            )
            match = pattern.search(html)
            if match:
                result[name] = match.group(1).strip()

        # 2차: 저자 소개 페이지에서 추가 추출
        if len(result) < len(target_names):
            author_search_values = re.findall(
                r"AuthorSearch=([^\"'&\s]+)", html, flags=re.IGNORECASE
            )
            author_search_values = list(dict.fromkeys(author_search_values))

            for value in author_search_values:
                author_url = f"https://www.aladin.co.kr/author/wauthor_overview.aspx?AuthorSearch={value}"
                try:
                    req2 = urllib.request.Request(author_url, headers=headers)
                    with urllib.request.urlopen(req2, timeout=12) as resp2:
                        author_html = resp2.read().decode("utf-8", errors="ignore")

                    pairs = re.findall(
                        r"([가-힣][가-힣\s.\-]{0,40})\s*\(\s*([A-Za-z][A-Za-z .,'-]{1,80})\s*\)",
                        author_html
                    )
                    for kor_name, orig_name in pairs:
                        kn = kor_name.strip()
                        on = orig_name.strip()
                        if kn in target_names and kn not in result:
                            result[kn] = on
                except Exception:
                    continue

    except Exception:
        pass

    return result


def parse_authors(author_str, page_link=""):
    """
    알라딘 author 문자열 파싱 + 페이지 크롤링으로 원어명 보강.
    """
    result = []
    found = set()

    # 케이스 1: 한국어이름 (영문원어명) (역할) — API가 원어명을 직접 제공하는 경우
    p1 = re.findall(
        r"([^,]+?)\s*\(([A-Za-z][^)]*)\)\s*\(([^)]+)\)",
        author_str
    )
    for kor_name, original, role in p1:
        kor_name = kor_name.strip()
        if not is_korean(kor_name):
            continue
        result.append({
            "name": kor_name,
            "role": role.strip(),
            "is_org": is_org(kor_name),
            "original_name": original.strip(),
        })
        found.add(kor_name)

    # 케이스 2: 이름 (역할) — 일반 패턴
    p2 = re.findall(r"([^,(]+?)\s*\(([^)]+)\)", author_str)
    for name, info in p2:
        name = name.strip()
        if not name or is_western(name) or name in found:
            continue
        result.append({
            "name": name,
            "role": info.strip(),
            "is_org": is_org(name),
            "original_name": "",
        })
        found.add(name)

    # 파싱 실패 시 기본 처리
    if not result:
        for name in author_str.split(","):
            name = name.strip()
            if name:
                result.append({
                    "name": name, "role": "", "is_org": is_org(name), "original_name": "",
                })

    # 원어명 없는 저자들에 대해 페이지 크롤링으로 보강
    if page_link:
        need_original = [
            a["name"] for a in result
            if not a["original_name"] and not a["is_org"] and is_korean(a["name"])
        ]
        if need_original:
            scraped = extract_original_names_from_aladin_page(page_link, need_original)
            for a in result:
                if a["name"] in scraped:
                    a["original_name"] = scraped[a["name"]]

    return result


def build_245(title, subtitle, authors):
    a_part = title.strip()
    b_part = subtitle.strip() if subtitle else ""

    persons = [a for a in authors if not a["is_org"]]
    primary = [a for a in persons if a["role"] in PRIMARY_ROLES]
    secondary = [a for a in persons if a["role"] not in PRIMARY_ROLES]

    role_groups = {}
    for a in secondary:
        label = ROLE_LABEL.get(a["role"], a["role"])
        role_groups.setdefault(label, []).append(a)

    field = "$a " + a_part
    if b_part:
        field += " $b : " + b_part

    if primary:
        p_label = PRIMARY_LABEL.get(primary[0]["role"], "지은이")
        field += " /$d " + p_label + ": " + primary[0]["name"]
        for a in primary[1:]:
            pl = PRIMARY_LABEL.get(a["role"], "지은이")
            field += " ,$e " + pl + ": " + a["name"]
        for label, members in role_groups.items():
            for a in members:
                field += " ;$e " + label + ": " + a["name"]
    elif role_groups:
        all_members = []
        for members in role_groups.values():
            all_members.extend(members)
        first_lbl = ROLE_LABEL.get(all_members[0]["role"], all_members[0]["role"])
        field += " /$d " + first_lbl + ": " + all_members[0]["name"]
        for a in all_members[1:]:
            lbl = ROLE_LABEL.get(a["role"], a["role"])
            field += " ,$e " + lbl + ": " + a["name"]

    return field


def build_700(author):
    """
    원어명 있음  → 원어명 역순:            Nunez, Sigrid
    원어명 없음 + 한국어 2어절 이상 → 한국어 역순: 레빙턴, 리베카 가딘
    한국어 단일 이름               → 그대로:      김영아
    """
    name = author["name"].strip()
    original = author.get("original_name", "").strip()

    if original and is_western(original):
        return "$a " + invert_western(original)
    elif is_korean(name) and len(name.split()) >= 2:
        return "$a " + invert_korean(name)
    else:
        return "$a " + name


def build_900(author):
    """원어명 있는 저자만 → 한국어 역순"""
    name = author["name"].strip()
    original = author.get("original_name", "").strip()
    if original and is_western(original) and is_korean(name):
        return "900 10 $a " + invert_korean(name)
    return ""


def build_710(author):
    return "710 0_ $a " + author["name"].strip() + "."


def to_isbn13(isbn):
    if len(isbn) == 13:
        return isbn
    base = "978" + isbn[:9]
    check = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(base))
    return base + str((10 - check % 10) % 10)


def fetch_aladin(isbn):
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


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


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
    except Exception as e:
        return jsonify({"error": "알라딘 API 오류: " + str(e)}), 502

    # 표제 / 부제목 분리
    title = item.get("title", "")
    subtitle = ""

    sub_info = item.get("subInfo", {})
    if isinstance(sub_info, dict):
        api_sub = sub_info.get("subTitle", "").strip()
        if api_sub and title.endswith(api_sub):
            title = title[:-len(api_sub)].rstrip(" -:").strip()
            subtitle = api_sub
        elif api_sub:
            subtitle = api_sub

    if not subtitle:
        for sep in [" - ", " : "]:
            if sep in title:
                t, s = title.split(sep, 1)
                title, subtitle = t.strip(), s.strip()
                break

    author_str = item.get("author", "")
    page_link = item.get("link", "")  # 알라딘 상품 페이지 URL

    # 원어명 크롤링 포함 저자 파싱
    authors = parse_authors(author_str, page_link)

    field_245 = build_245(title, subtitle, authors)

    persons = [a for a in authors if not a["is_org"]]
    fields_700 = ["700 1_ " + build_700(a) for a in persons]

    fields_900 = []
    for a in persons:
        r = build_900(a)
        if r:
            fields_900.append(r)

    orgs = [a for a in authors if a["is_org"]]
    fields_710 = ["710 0_ " + build_710(a) for a in orgs]

    return jsonify({
        "isbn13": isbn13,
        "title": title,
        "subtitle": subtitle,
        "author_raw": author_str,
        "authors": authors,
        "publisher": item.get("publisher", ""),
        "pub_date": item.get("pubDate", ""),
        "cover": item.get("cover", ""),
        "marc": {
            "f245": "245 00 " + field_245,
            "f700": fields_700,
            "f710": fields_710,
            "f900": fields_900,
        }
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

