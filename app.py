from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import os
import urllib.request
import urllib.error
import urllib.parse
import json

app = Flask(__name__)
CORS(app)

ALADIN_API_KEY = os.environ.get("ALADIN_API_KEY", "ttbboyeong09010919001")
ALADIN_API_URL = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"

# ─────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────
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

# 발음 변환 매핑
EN_KO_MAP = {
    "chatgpt": "챗지피티", "gpt": "지피티", "ai": "에이아이",
    "api": "에이피아이", "ml": "엠엘", "nlp": "엔엘피",
    "llm": "엘엘엠", "excel": "엑셀", "youtube": "유튜브",
}

DECIMAL_MAP = {
    "2.0": "이점영", "3.0": "삼점영", "4.0": "사점영",
}

SINO = {"0":"영","1":"일","2":"이","3":"삼","4":"사","5":"오","6":"육","7":"칠","8":"팔","9":"구"}


# ─────────────────────────────────────────────
# 기본 헬퍼
# ─────────────────────────────────────────────
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


# ─────────────────────────────────────────────
# 표제 분리 로직 개선 (괄호·전각문자 처리)
# ─────────────────────────────────────────────
DELIMS = [" : ", ": ", ":", " - ", " — ", "–", "—", " · ", "·", " | ", "|"]

def compat_normalize(s):
    if not s:
        return ""
    s = s.replace("：", ":").replace("－", "-").replace("‧", "·").replace("／", "/")
    s = re.sub(r"[\u2000-\u200f\u202a-\u202e]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

TRAIL_PAREN_PAT = re.compile(
    r"\s*(?:[\(\[](개정|증보|개역|전정|합본|개정판|증보판|신판|보급판|초판|제?\d+\s*판|기념판)[)\]])\s*$",
    re.IGNORECASE
)

def strip_trailing_paren_notes(s):
    return TRAIL_PAREN_PAT.sub("", s).strip(" .,/;:-—·|")

def clean_piece(s):
    if not s:
        return ""
    s = compat_normalize(s)
    s = strip_trailing_paren_notes(s)
    s = s.strip(" .,/;:-—·|")
    return s

def find_top_level_split(text, delims=DELIMS):
    pairs = {
        "(": ")", "[": "]", "{": "}", "〈": "〉", "《": "》",
        "「": "」", "『": "』", "\u201c": "\u201d", "\u2018": "\u2019", "«": "»"
    }
    opens = set(pairs.keys())
    stack, i, L = [], 0, len(text)
    while i < L:
        ch = text[i]
        if ch in opens:
            stack.append(ch)
            i += 1
            continue
        if stack and pairs.get(stack[-1]) == ch:
            stack.pop()
            i += 1
            continue
        if not stack:
            for d in delims:
                if text.startswith(d, i):
                    return i, d
        i += 1
    return None

def split_title_subtitle(raw_title, raw_sub=""):
    """개선된 표제/부표제 분리"""
    title = compat_normalize(raw_title)
    subtitle = clean_piece(raw_sub)

    # API가 부표제를 직접 주는 경우 우선 사용
    if subtitle:
        # 제목 끝에 부표제가 붙어있으면 제거
        for pat in [f" : {subtitle}", f": {subtitle}", f" - {subtitle}"]:
            if title.endswith(pat):
                title = title[:-len(pat)]
                break
        return clean_piece(title), subtitle

    # 괄호를 고려한 구분자 기반 분리
    t = compat_normalize(title)
    hit = find_top_level_split(t, DELIMS)
    if not hit:
        return clean_piece(t), ""
    idx, delim = hit
    left = t[:idx]
    right = t[idx + len(delim):]
    return clean_piece(left), clean_piece(right)


# ─────────────────────────────────────────────
# $n 권차 자동 분리
# ─────────────────────────────────────────────
PART_LABEL_RX = re.compile(
    r"(?:제?\s*\d+\s*(?:권|부|편|책)|[IVXLCDM]+|[상중하]|[전후])$",
    re.IGNORECASE
)

def split_part_number(title, subtitle, item):
    """제목/부제에서 권차($n) 추출"""
    a = title.strip()
    n = ""

    # 괄호형 권차: '자바의 정석 (제2권)'
    m_paren = re.search(r"\s*[\(\[]\s*([^()\[\]]+)\s*[\)\]]\s*$", a)
    if m_paren and PART_LABEL_RX.search(m_paren.group(1).strip()):
        n_token = m_paren.group(1).strip()
        a = a[:m_paren.start()].rstrip(" .,/;:-—·|")
        m_num = re.search(r"\d+", n_token)
        return a, subtitle, (m_num.group(0) if m_num else n_token)

    # 라벨형: '자바의 정석 제2권'
    m_label = re.search(r"\s*(제?\s*\d+\s*(?:권|부|편|책))\s*$", a, re.IGNORECASE)
    if m_label:
        a = a[:m_label.start()].rstrip(" .,/;:-—·|")
        m_num = re.search(r"\d+", m_label.group(1))
        return a, subtitle, (m_num.group(0) if m_num else m_label.group(1).strip())

    # 상/중/하, 전/후
    m_kor = re.search(r"\s*([상중하]|[전후])\s*$", a)
    if m_kor:
        a = a[:m_kor.start()].rstrip(" .,/;:-—·|")
        return a, subtitle, m_kor.group(1)

    # 로마숫자
    if not re.fullmatch(r"[IVXLCDM]+", a, re.IGNORECASE):
        m_roman = re.search(r"\s+([IVXLCDM]{2,})\s*$", a, re.IGNORECASE)
        if m_roman:
            a = a[:m_roman.start()].rstrip(" .,/;:-—·|")
            return a, subtitle, m_roman.group(1)

    return a, subtitle, n


# ─────────────────────────────────────────────
# 246 원제 필드
# ─────────────────────────────────────────────
YEAR_EDITION_PAT = re.compile(
    r"\s*\(\s*(?:\d{3,4}\s*년?|rev(?:ised)?\.?\s*ed\.?|(?:\d+(?:st|nd|rd|th)\s*ed\.?)|edition|ed\.?|제?\s*\d+\s*판|개정(?:증보)?판?|증보판|초판|신판|보급판)[^()\[\]]*\)\s*$",
    re.IGNORECASE
)

def build_246(item):
    """알라딘 originalTitle에서 246 19 $a 생성"""
    sub_info = item.get("subInfo") or {}
    orig = (sub_info.get("originalTitle") or "").strip()
    if not orig:
        return ""
    orig = clean_piece(orig)
    orig = YEAR_EDITION_PAT.sub("", orig).strip()
    if orig:
        return "246 19 $a " + orig
    return ""


# ─────────────────────────────────────────────
# 940 한국어 발음 표기
# ─────────────────────────────────────────────
def read_number(num_str):
    """숫자를 한국어 발음으로 변환"""
    n = int(num_str)
    th = n // 1000
    hu = (n // 100) % 10
    te = (n // 10) % 10
    on = n % 10
    out = []
    if th:
        out.append(SINO[str(th)] + "천")
    if hu:
        out.append(SINO[str(hu)] + "백")
    if te:
        out.append("십" if te == 1 else SINO[str(te)] + "십")
    if on:
        out.append(SINO[str(on)])
    return "".join(out) if out else "영"

def read_digits(num_str):
    return "".join(SINO.get(ch, ch) for ch in num_str)

def replace_decimals(text):
    for k, v in DECIMAL_MAP.items():
        text = text.replace(k, v)
    return text

def replace_english(text):
    def sub(m):
        return EN_KO_MAP.get(m.group(0).lower(), m.group(0))
    pattern = r"\b(" + "|".join(map(re.escape, EN_KO_MAP.keys())) + r")\b"
    return re.sub(pattern, sub, text, flags=re.IGNORECASE)

def build_940(title_a):
    """제목에 숫자/영문이 있으면 한국어 발음 표기 940 필드 생성"""
    base = (title_a or "").strip()
    if not base:
        return []
    # 숫자/영문 없으면 생략
    if not re.search(r"[0-9A-Za-z]", base):
        return []

    variants = set()

    # 영문 치환 + 소수 치환
    v1 = replace_decimals(base)
    v1 = replace_english(v1)
    if v1 != base:
        variants.add(v1)

    # 숫자 읽기 변형
    nums = re.findall(r"\d{2,}", base)
    if nums:
        work = {replace_decimals(replace_english(base))}
        for num in nums:
            new_work = set()
            candidates = set()
            candidates.add(read_number(num))
            candidates.add(read_digits(num))
            if len(num) == 4 and 1000 <= int(num) <= 2999:
                candidates.add(read_number(num))
            for w in work:
                for c in candidates:
                    new_work.add(w.replace(num, c, 1))
            work = new_work
        variants |= work

    # 940 필드 조립
    result = []
    seen = set()
    for v in sorted(variants, key=len):
        v = v.strip()
        if not v or v == base or v in seen:
            continue
        if not re.search(r"[가-힣]", v):
            continue
        seen.add(v)
        result.append("940 \\\\ $a " + v)

    return result[:4]


# ─────────────────────────────────────────────
# VIAF 저자 국적 조회
# ─────────────────────────────────────────────
# VIAF에서 동아시아 관련 nationality 코드 (역순 변환 안 하는 국가들)
EAST_ASIAN_NATIONALITIES = {
    # 한국
    "ko", "kor",
    # 일본
    "ja", "jpn", "jp",
    # 중국
    "zh", "chi", "zho", "cn",
    # 대만
    "tw",
    # 베트남
    "vi", "vie", "vn",
}

# VIAF 소스 중 동아시아 국립도서관 코드
EAST_ASIAN_SOURCES = {
    "NLSK", "NLK",        # 한국국립중앙도서관
    "NDL",                # 일본국립국회도서관
    "NLC",                # 중국국가도서관
    "PLWABN",             # 폴란드 (제외용 아님, 동아시아만)
}

def get_viaf_nationality(name):
    """
    VIAF API로 저자 국적/언어 정보 조회.
    반환: 'east_asian' / 'non_east_asian' / None(조회 실패)
    동아시아(한국/일본/중국 등) 저자는 이름 도치 불필요.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Referer": "https://viaf.org/",
    }
    try:
        search_url = "https://viaf.org/viaf/search"
        params = {
            "query": f'local.personalNames all "{name}"',
            "maximumRecords": 3,
            "startRecord": 1,
            "httpAccept": "application/json",
        }
        resp = requests.get(search_url, params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        records = (
            data.get("searchRetrieveResponse", {})
                .get("records", {})
                .get("record", [])
        )
        if isinstance(records, dict):
            records = [records]
        if not records:
            return None

        record_data = records[0].get("recordData", {})
        viaf_cluster = record_data.get("VIAFCluster", record_data)

        # 방법 1: 동아시아 국립도서관 소스 확인 (NLK=한국, NDL=일본, NLC=중국)
        sources = viaf_cluster.get("sources", {}).get("s", [])
        if isinstance(sources, str):
            sources = [sources]
        for source in sources:
            source_id = source.get("@id", "") if isinstance(source, dict) else str(source)
            for es in EAST_ASIAN_SOURCES:
                if es in source_id:
                    return "east_asian"

        # 방법 2: nationalityOfAssociatedName 필드 확인
        nat_field = viaf_cluster.get("nationalityOfAssociatedName", {})
        if isinstance(nat_field, dict):
            nat_data = nat_field.get("data", [])
            if isinstance(nat_data, dict):
                nat_data = [nat_data]
            for item in nat_data:
                text = (item.get("text", "") or "").lower()
                if text in EAST_ASIAN_NATIONALITIES:
                    return "east_asian"

        # 방법 3: VIAF ID 상세 조회
        viaf_id = viaf_cluster.get("viafID") or viaf_cluster.get("@viafID")
        if viaf_id:
            detail_url = f"https://viaf.org/viaf/{viaf_id}/viaf.json"
            resp2 = requests.get(detail_url, headers=headers, timeout=10)
            if resp2.status_code == 200:
                detail = resp2.json()
                src_list = detail.get("sources", {}).get("s", [])
                if isinstance(src_list, str):
                    src_list = [src_list]
                for src in src_list:
                    src_id = src.get("@id", "") if isinstance(src, dict) else str(src)
                    for es in EAST_ASIAN_SOURCES:
                        if es in src_id:
                            return "east_asian"
                nat2 = detail.get("nationalityOfAssociatedName", {})
                if isinstance(nat2, dict):
                    nd = nat2.get("data", [])
                    if isinstance(nd, dict):
                        nd = [nd]
                    for item in nd:
                        text = (item.get("text", "") or "").lower()
                        if text in EAST_ASIAN_NATIONALITIES:
                            return "east_asian"

        return "non_east_asian"
    except Exception:
        return None


def is_east_asian_name_pattern(name):
    """
    VIAF 조회 실패 시 패턴 기반 동아시아 이름 판별 폴백.

    규칙:
    - 1어절 한국어 → 동아시아 (김영아, 한강, 위화)
    - 2어절 한국어:
        * 한 어절 6글자 이상 → 서양인 (미르탈리포바=6)
        * 한 어절 5글자, 총 9글자 이하 → 동아시아 (아쿠타가와 류노스케=9)
        * 한 어절 4글자 이하, 총 7글자 이하 → 동아시아 (무라카미 하루키=7)
    - 3어절 이상 → 서양인 표기 (리베카 가딘 레빙턴)
    """
    name = name.strip()
    parts = name.split()

    if not all(re.fullmatch(r"[가-힣]+", p) for p in parts):
        return False

    # 1어절 → 항상 동아시아
    if len(parts) == 1:
        return True

    max_len = max(len(p) for p in parts)
    total = sum(len(p) for p in parts)

    # 2어절
    if len(parts) == 2:
        if max_len >= 6:
            return False  # 서양인 (미르탈리포바=6)
        if max_len == 5 and total <= 9:
            return True   # 일본인 성씨 (아쿠타가와 류노스케=9)
        if max_len <= 4 and total <= 7:
            return True   # 동아시아 (무라카미 하루키=7)

    # 3어절 이상 → 서양인 표기
    return False


def is_east_asian_author_viaf(name):
    """
    VIAF 기반 동아시아 저자 여부 판별.
    VIAF 조회 실패 시 이름 패턴으로 폴백.
    """
    result = get_viaf_nationality(name)
    if result == "east_asian":
        return True
    if result == "non_east_asian":
        return False
    # VIAF 조회 실패 → 패턴 기반 폴백
    return is_east_asian_name_pattern(name)


def extract_original_names_from_aladin_page(link, names):
    if not link or not names:
        return {}, {}, {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    result = {}
    hanja_result = {}
    # 동아시아 국적 여부: {이름: True/False/None}
    nationality_result = {}

    target_names = [n for n in names if not is_org(n)]

    def detect_nationality_from_text(text, name):
        """
        저자 소개 텍스트에서 국적 판별.
        단순 도시명 대신 '출생', '출신', '태어' 등과 조합해서 정확하게 감지.
        """
        idx = text.find(name)
        if idx == -1:
            return None
        # 저자 이름 뒤 400자 범위 내에서만 검색
        snippet = text[max(0, idx - 30) : idx + 400]

        # 동아시아 국적 패턴 (도시명 + 출생/출신/태어 조합)
        east_asian_patterns = [
            # 일본
            r"일본\s*(?:출생|출신|태생|에서\s*(?:태어|출생))",
            r"(?:도쿄|교토|오사카|삿포로|나고야|요코하마|고베|후쿠오카|히로시마)\s*(?:출생|출신|에서\s*태어|에서\s*출생)",
            r"(?:도쿄|교토|오사카)\s*\d{4}",  # 도쿄 1949년 같은 패턴
            # 중국
            r"중국\s*(?:출생|출신|태생|에서\s*(?:태어|출생))",
            r"(?:베이징|상하이|광저우|청두|충칭)\s*(?:출생|출신|에서\s*태어)",
            # 한국
            r"한국\s*(?:출생|출신|태생)",
            r"(?:서울|부산|대구|인천|광주|대전)\s*(?:출생|출신|에서\s*태어)",
        ]

        # 서양 국적 패턴
        western_patterns = [
            r"(?:프랑스|독일|영국|미국|이탈리아|스페인|러시아|오스트리아|스위스|"
            r"노르웨이|스웨덴|덴마크|네덜란드|우즈베키스탄|카자흐스탄|알제리|"
            r"아르헨티나|브라질|콜롬비아|칠레|멕시코|쿠바|이란|이스라엘|인도|"
            r"파키스탄|터키|이집트|나이지리아|에티오피아)\s*(?:출생|출신|태생|에서\s*(?:태어|출생))",
            r"(?:뉴욕|런던|파리|베를린|모스크바|로마|마드리드|비엔나|암스테르담)\s*(?:출생|출신|에서\s*태어)",
        ]

        for pat in east_asian_patterns:
            if re.search(pat, snippet):
                return "east_asian"

        for pat in western_patterns:
            if re.search(pat, snippet):
                return "non_east_asian"

        return None

    try:
        req = urllib.request.Request(link, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        for name in target_names:
            # 영문 원저자명 패턴
            pattern = re.compile(
                rf"{re.escape(name)}\s*\(\s*([A-Za-z][A-Za-z .,'-]+)\s*\)",
                re.IGNORECASE
            )
            match = pattern.search(html)
            if match:
                result[name] = match.group(1).strip()

            # 한자 원저자명 패턴
            hanja_pattern = re.compile(
                rf"{re.escape(name)}\s*\(\s*([\u4e00-\u9fff\u3040-\u30ff][\u4e00-\u9fff\u3040-\u30ff\s·]+)\s*\)",
                re.IGNORECASE
            )
            hanja_match = hanja_pattern.search(html)
            if hanja_match:
                hanja_result[name] = hanja_match.group(1).strip()

            # 상품 페이지에서 국적 판별
            nat = detect_nationality_from_text(html, name)
            if nat:
                nationality_result[name] = nat

        # 저자 소개 페이지 추가 크롤링
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

                # 영문 원저자명
                pairs = re.findall(
                    r"([가-힣][가-힣\s.\-]{0,40})\s*\(\s*([A-Za-z][A-Za-z .,'-]{1,80})\s*\)",
                    author_html
                )
                for kor_name, orig_name in pairs:
                    kn = kor_name.strip()
                    on = orig_name.strip()
                    if kn in target_names and kn not in result:
                        result[kn] = on

                # 한자 원저자명
                hanja_pairs = re.findall(
                    r"([가-힣][가-힣\s.\-]{0,40})\s*\(\s*([\u4e00-\u9fff\u3040-\u30ff][\u4e00-\u9fff\u3040-\u30ff\s·]+)\s*\)",
                    author_html
                )
                for kor_name, hanja_name in hanja_pairs:
                    kn = kor_name.strip()
                    hn = hanja_name.strip()
                    if kn in target_names and kn not in hanja_result:
                        hanja_result[kn] = hn

                # 저자 소개 페이지에서 국적 판별
                for name in target_names:
                    if name not in nationality_result:
                        nat = detect_nationality_from_text(author_html, name)
                        if nat:
                            nationality_result[name] = nat

            except Exception:
                continue
    except Exception:
        pass

    return result, hanja_result, nationality_result


# ─────────────────────────────────────────────
# 저자 파싱
# ─────────────────────────────────────────────
def parse_authors(author_str, page_link=""):
    result = []
    found = set()

    # 케이스 1: 한국어이름 (영문원어명) (역할)
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
            "hanja_name": "",
            "nationality": None,
        })
        found.add(kor_name)

    # 케이스 2: 이름 (역할)
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
            "hanja_name": "",
            "nationality": None,
        })
        found.add(name)

    if not result:
        for name in author_str.split(","):
            name = name.strip()
            if name:
                result.append({
                    "name": name, "role": "", "is_org": is_org(name),
                    "original_name": "", "hanja_name": "", "nationality": None,
                })

    # 페이지 크롤링으로 영문·한자 원저자명 + 국적 정보 보강
    if page_link:
        all_persons = [
            a["name"] for a in result
            if not a["is_org"] and is_korean(a["name"])
        ]
        if all_persons:
            scraped, hanja_scraped, nat_scraped = extract_original_names_from_aladin_page(page_link, all_persons)
            for a in result:
                if a["name"] in scraped and not a["original_name"]:
                    a["original_name"] = scraped[a["name"]]
                if a["name"] in hanja_scraped:
                    a["hanja_name"] = hanja_scraped[a["name"]]
                if a["name"] in nat_scraped:
                    a["nationality"] = nat_scraped[a["name"]]

    return result


# ─────────────────────────────────────────────
# MARC 필드 생성
# ─────────────────────────────────────────────
def build_245(title, subtitle, part_number, authors):
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

    # $n 권차
    if part_number:
        field += " $n " + part_number

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


def build_500(authors):
    """
    500 \\ $a 원저자명 주기 생성.
    한자(일본어/중국어) 원저자명이 있는 저자들을 쉼표로 나열.
    예: 500 \\ $a 원저자명: 村上春樹, 安西 水丸
    """
    hanja_names = []
    for a in authors:
        hanja = a.get("hanja_name", "").strip()
        if hanja:
            hanja_names.append(hanja)

    if not hanja_names:
        return ""

    return "500 \\\\ $a 원저자명: " + ", ".join(hanja_names)



    """
    원어명 있음  → 원어명 역순:                     Nunez, Sigrid
    원어명 없음 + 한국어 2어절 이상:
      - VIAF로 동아시아인 확인 → 그대로:            무라카미 하루키
      - VIAF로 비동아시아인 확인 → 역순:            레빙턴, 리베카 가딘
    한국어 단일 이름 (2~5글자 공백없음) → 그대로:   김영아
    """
    name = author["name"].strip()
    original = author.get("original_name", "").strip()

    # 원어명 있으면 원어명 역순
    if original and is_western(original):
        return "$a " + invert_western(original)

    # 한국어 단일 이름 (공백 없는 2~5글자) → 동아시아인, 그대로
    if re.fullmatch(r"[가-힣]{2,5}", name):
        return "$a " + name

    # 한국어 2어절 이상 → VIAF로 동아시아인 여부 판별
    if is_korean(name) and len(name.split()) >= 2:
        east_asian = is_east_asian_author_viaf(name)
        if east_asian:
            return "$a " + name                 # 동아시아인 → 그대로
        else:
            return "$a " + invert_korean(name)  # 서양인 한국어 표기 → 역순

    return "$a " + name

def build_700(author):
    """
    700 1_ 개인명 부출기입 도치 방식:

    1. 영문 원어명 있음 → 원어명 역순:              Nunez, Sigrid
    2. 한자 원저자명 있음 → 동아시아 확정 → 그대로: 무라카미 하루키
    3. 알라딘 크롤링 국적 정보 있음 → 국적으로 판별
    4. 한국어 단일 이름 (1어절) → 항상 그대로:      김영아
    5. VIAF 조회 → 패턴 폴백
    """
    name = author["name"].strip()
    original = author.get("original_name", "").strip()
    hanja = author.get("hanja_name", "").strip()
    nationality = author.get("nationality")  # 'east_asian' / 'non_east_asian' / None

    # 1. 영문 원어명 → 원어명 역순
    if original and is_western(original):
        return "$a " + invert_western(original)

    # 2. 한자 원저자명 → 동아시아 확정 → 그대로
    if hanja:
        return "$a " + name

    # 3. 알라딘 크롤링 국적 정보 활용
    if nationality == "east_asian":
        return "$a " + name
    if nationality == "non_east_asian":
        if is_korean(name) and len(name.split()) >= 2:
            return "$a " + invert_korean(name)
        return "$a " + name

    # 4. 한국어 단일 이름 → 그대로
    if re.fullmatch(r"[가-힣]{2,5}", name):
        return "$a " + name

    # 5. 한국어 2어절 이상 → VIAF → 패턴 폴백
    if is_korean(name) and len(name.split()) >= 2:
        east_asian = is_east_asian_author_viaf(name)
        if east_asian:
            return "$a " + name
        else:
            return "$a " + invert_korean(name)

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


# ─────────────────────────────────────────────
# API 엔드포인트
# ─────────────────────────────────────────────
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

    # 개선된 표제/부표제 분리
    raw_title = item.get("title", "")
    raw_sub = (item.get("subInfo") or {}).get("subTitle") or ""
    title, subtitle = split_title_subtitle(raw_title, raw_sub)

    # $n 권차 분리
    title, subtitle, part_number = split_part_number(title, subtitle, item)

    # 저자 파싱
    author_str = item.get("author", "")
    page_link = item.get("link", "")
    authors = parse_authors(author_str, page_link)

    # 245 필드
    field_245 = build_245(title, subtitle, part_number, authors)

    # 246 원제 필드
    field_246 = build_246(item)

    # 500 원저자명 주기 (한자 원저자명)
    field_500 = build_500(authors)

    # 700 / 900 / 710 필드
    persons = [a for a in authors if not a["is_org"]]
    fields_700 = ["700 1_ " + build_700(a) for a in persons]

    fields_900 = []
    for a in persons:
        r = build_900(a)
        if r:
            fields_900.append(r)

    orgs = [a for a in authors if a["is_org"]]
    fields_710 = ["710 0_ " + build_710(a) for a in orgs]

    # 940 한국어 발음 표기
    fields_940 = build_940(title)

    return jsonify({
        "isbn13": isbn13,
        "title": title,
        "subtitle": subtitle,
        "part_number": part_number,
        "author_raw": author_str,
        "authors": authors,
        "publisher": item.get("publisher", ""),
        "pub_date": item.get("pubDate", ""),
        "cover": item.get("cover", ""),
        "marc": {
            "f245": "245 00 " + field_245,
            "f246": field_246,
            "f500": field_500,
            "f700": fields_700,
            "f710": fields_710,
            "f900": fields_900,
            "f940": fields_940,
        }
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
