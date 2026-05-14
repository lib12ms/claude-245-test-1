"""
KORMARC 자동 생성기 - Flask 백엔드 (Render 배포용)

[데이터 수집 순서]
  1. 알라딘 API (표제, 저자, 역자, subInfo.originalTitle)
  2. 알라딘 상품 페이지 크롤링
     - SearchWord 링크 → 원제/원저자 영문명 (퍼센트 인코딩 디코드)
     - "원제 :" 텍스트 패턴 → 원제
     - "한글명 (영문명)" 패턴 → 원저자 영문명
     - meta-author 태그 → 한자명
     - 동아시아 키워드 → 동아시아 여부
  2b. 알라딘 외국도서 Title 검색(원제) → author 필드의 한글·한자 병기로 한자명 보완

[생성 필드]
  245 00  표제와 책임표시사항 (총서명 제거, 마케팅 부제목 제거)
  246 19  원서명 (번역서만)
  500 __  원저자명 주기 (번역서만)
  700 1_  개인명 부출(성·이름 구분 표기)
  700 0_  개인명 부출(한글 단명·닉네임 등 성 구분 없음, 주로 역·그림 등)
  710 0_  기관명 부출기입
  900 10  원저자 한글명 부출 (동아시아 도치 없음, 서양 도치, 한자only 음독)
"""

from __future__ import annotations

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import html
import json
import re
import os
import time
from urllib.parse import unquote

try:
    import hanja
    HANJA_AVAILABLE = True
except ImportError:
    HANJA_AVAILABLE = False

try:
    import pykakasi
    import jaconv

    _PYKAKASI_JACONV = True
except ImportError:
    _PYKAKASI_JACONV = False

app = Flask(__name__)
CORS(app)

ALADIN_API_KEY = os.environ.get("ALADIN_API_KEY", "ttbboyeong09010919001")
ALADIN_API_URL = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ALADIN_SEARCH_URL = "http://www.aladin.co.kr/ttb/api/ItemSearch.aspx"

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
    r".*아쿠타가와\s*상.*",
    r".*아쿠타가와상.*",
    r".*수상작\s*$",
    r".*수상\s*$",
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
    """부제목에서 마케팅/수상 키워드 제거. 수상작·특정 문학상 한 줄은 부표제로 두지 않음."""
    if not subtitle:
        return subtitle
    st = subtitle.strip()
    if re.search(r"아쿠타가와", st):
        return ""
    if re.search(
        r"제\s*\d+\s*회.*(나오키|본야도|서점\s*대상|일본\s*서점|아쿠타가와)",
        st,
        re.I | re.S,
    ):
        return ""
    if re.search(r"(수상\s*작|수상작)\s*$", st, re.I):
        return ""
    for pattern in SUBTITLE_NOISE_PATTERNS:
        if re.search(pattern, st, re.I | re.S):
            return ""
    for sep in [" · ", " - ", " | ", " / "]:
        if sep in st:
            parts = st.split(sep)
            cleaned = [p for p in parts if not any(re.search(pat, p.strip(), re.I | re.S) for pat in SUBTITLE_NOISE_PATTERNS)]
            return sep.join(cleaned).strip() if cleaned else ""
    return st


def strip_award_suffix_from_title(title: str) -> str:
    """표제 문자열에만 붙은 ' : 제○회 … 수상' 등은 부표제($b)가 아니므로 표제에서 제거."""
    t = title.strip()
    for sep in (" - ", " – ", " : ", "：", ":"):
        if sep not in t:
            continue
        a, b = t.split(sep, 1)
        bs = b.strip()
        if not bs:
            continue
        if clean_subtitle(bs) == "":
            return a.strip()
    return t


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


def _build_katakana_hangul_maps() -> tuple[dict[str, str], dict[str, str]]:
    """
    가타카나(외래어 표기에 가까운 한글) 1·2글자 매핑.
    900 필드에 일본 원표기를 한글로 풀어 쓸 때 사용.
    """
    yo: dict[str, str] = {
        "キャ": "캬",
        "キュ": "큐",
        "キョ": "쿄",
        "シャ": "샤",
        "シュ": "슈",
        "ショ": "쇼",
        "チャ": "차",
        "チュ": "추",
        "チョ": "초",
        "ニャ": "냐",
        "ニュ": "뉴",
        "ニョ": "뇨",
        "ヒャ": "햐",
        "ヒュ": "휴",
        "ヒョ": "효",
        "ミャ": "먀",
        "ミュ": "뮤",
        "ミョ": "묘",
        "リャ": "랴",
        "リュ": "류",
        "リョ": "료",
        "ギャ": "갸",
        "ギュ": "규",
        "ギョ": "교",
        "ジャ": "쟈",
        "ジュ": "주",
        "ジョ": "조",
        "ヂャ": "쟈",
        "ヂュ": "주",
        "ヂョ": "조",
        "ビャ": "뱌",
        "ビュ": "뷰",
        "ビョ": "뵤",
        "ピャ": "퍄",
        "ピュ": "표",
        "ピョ": "표",
        "デャ": "댜",
        "デュ": "듀",
        "デョ": "됴",
    }
    one: dict[str, str] = {}
    one.update(zip("アイウエオ", "아이우에오"))
    one.update(zip("カキクケコ", "카키크케코"))
    one.update(zip("ガギグゲゴ", "가기구게고"))
    one.update(zip("サシスセソ", "사시스세소"))
    one.update(zip("ザジズゼゾ", "자지즈제조"))
    one.update(zip("タチツテト", "타치츠테토"))
    one.update(zip("ダヂヅデド", "다지즈데도"))
    one.update(zip("ナニヌネノ", "나니누네노"))
    one.update(zip("ハヒフヘホ", "하히후헤호"))
    one.update(zip("バビブベボ", "바비부베보"))
    one.update(zip("パピプペポ", "파피푸페포"))
    one.update(zip("マミムメモ", "마미무메모"))
    one.update(zip("ヤユヨ", "야유요"))
    one.update(zip("ラリルレロ", "라리루레로"))
    one["ワ"] = "와"
    one["ヲ"] = "오"
    one["ン"] = "응"
    one["ヴ"] = "브"
    one["ヵ"] = "카"
    one["ヶ"] = "케"
    return yo, one


_KATA_YOON2, _KATA1 = _build_katakana_hangul_maps()


def _katakana_to_hangul(kata: str) -> str | None:
    """전각 가타카나 문자열 → 한글(외래어식). 공백은 생략."""
    if not kata:
        return None
    out: list[str] = []
    i = 0
    while i < len(kata):
        c = kata[i]
        if c in " \t\n　":
            i += 1
            continue
        if c in "ッっ":
            i += 1
            continue
        if c == "ー":
            i += 1
            continue
        if i + 1 < len(kata):
            pair = kata[i : i + 2]
            if pair in _KATA_YOON2:
                out.append(_KATA_YOON2[pair])
                i += 2
                continue
        if c in _KATA1:
            out.append(_KATA1[c])
            i += 1
            continue
        if c in "ァィゥェォャュョヮ":
            i += 1
            continue
        i += 1
    s = "".join(out)
    return s if s else None


_JP_SCRIPT_SPLIT = re.compile(
    r"([\u4e00-\u9fff\u3400-\u4dbf]+)|([\u3040-\u309f\u30a0-\u30ff\u30fc\s　·・]+)"
)


def _one_cjk_char_hangul_reading(ch: str) -> str:
    """한 글자 한자 → 한글 음(한국 한자음)."""
    if not ("\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"):
        return ""
    if HANJA_AVAILABLE:
        try:
            t = hanja.translate(ch, "substitution")
            if t and re.fullmatch(r"[\uac00-\ud7a3]+", t):
                return t
        except Exception:
            pass
    return HANJA_READING.get(ch, "")


def _cjk_run_hangul_reading(run: str) -> str:
    """연속 한자 열 → 글자별 한글 음 연결."""
    return "".join(_one_cjk_char_hangul_reading(c) for c in run)


def _kana_run_to_hangul(run: str) -> str | None:
    """히라가나·가타카나 열 → 한글(외래어식)."""
    kseg = re.sub(r"[\s　·・]+", "", (run or "").strip())
    if not kseg or not re.search(r"[\u3040-\u30ff]", kseg):
        return None
    if not _PYKAKASI_JACONV:
        return None
    try:
        kks = pykakasi.kakasi()
        kks.setMode("J", "H")
        kks.setMode("K", "H")
        kks.setMode("H", "H")
        hira = kks.getConverter().do(kseg)
    except Exception:
        return None
    if not hira:
        return None
    kata = jaconv.hira2kata(hira)
    return _katakana_to_hangul(kata)


def _jp_script_reading_hangul(script: str) -> str | None:
    """
    일본 원저자 표기(한자·가나 혼용)를 900용 한글로 풀어 씀.
    - 한자 덩어리: 글자마다 한국 한자음(hanja)
    - 가나 덩어리: 가타카나→한글(외래어식, ミチ→미치 등)
    """
    s = (script or "").strip()
    if not s:
        return None
    if not re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", s):
        return _kana_run_to_hangul(s)
    pieces: list[str] = []
    for m in _JP_SCRIPT_SPLIT.finditer(s):
        cjk, kana = m.group(1), m.group(2)
        if cjk:
            h = _cjk_run_hangul_reading(cjk)
            if not h:
                return None
            pieces.append(h)
        elif kana:
            kh = _kana_run_to_hangul(kana)
            if kh:
                pieces.append(kh)
    out = "".join(pieces)
    return out if out else None


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
    """
    알라딘 author 문자열 파싱.
    '저1, 저2, …, 저N (지은이), 역자 (옮긴이)' 처럼 앞 이름에는 (역할)이 없고
    마지막 저자에만 (지은이)가 붙는 합저 표기가 많음. 예전 정규식 [^,(]+? 는
    쉼표를 못 넘겨 저N·역자 두 명만 잡히는 버그가 있었음.
    """
    author_str = (author_str or "").strip()
    if not author_str:
        return []

    parts   = [p.strip() for p in author_str.split(",") if p.strip()]
    result: list[dict] = []
    pending: list[str] = []

    for part in parts:
        role: str | None = None
        name_part = part
        if part.endswith(")") and "(" in part:
            open_i = part.rfind("(")
            if open_i > 0:
                inner = part[open_i + 1 : -1].strip()
                cand = part[:open_i].strip()
                if cand and inner:
                    name_part, role = cand, inner
        if role is not None:
            for pn in pending:
                result.append({"name": pn, "role": role, "is_org": is_org(pn)})
            pending.clear()
            result.append({"name": name_part, "role": role, "is_org": is_org(name_part)})
        else:
            pending.append(part)

    for pn in pending:
        result.append({"name": pn.strip(), "role": "", "is_org": is_org(pn)})

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


def _is_probable_orig_script(s: str) -> bool:
    """괄호 안 문자열이 일본·중국 원표기(한자·가나 등)로 보이면 True. 한글만·짧은 잡텍스트 제외."""
    t = (s or "").strip()
    if len(t) < 2:
        return False
    if re.fullmatch(r"[\uac00-\ud7a3\s·・]+", t):
        return False
    return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]", t))


def enrich_kanji_map_from_aladin_blob(blob: str, names: list[str], km: dict) -> None:
    """
    페이지/HTML에서 한글명 옆 원저 표기 보완.
    - 합저·저자소개: '한글명 (靑崎有吾) (지은이)', '한글명 (一穂ミチ) (지은이)', '한글명 (織守 きょうや) (지은이)' 등
      (첫 괄호에 가나·공백 포함, 둘째 괄호에 역할)
    - 짧은 형식: '한글명（漢字）' 또는 '한글명 (漢字)'
    """
    if not blob:
        return
    script_in_paren = r"([\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\u30fc\u3400-\u4dbf·・\s]{2,56})"
    role_after = r"\s*[（(]\s*(?:지은이|저자|글(?:쓴이)?|원작)\s*[）)]"

    for nm in names:
        key = (nm or "").strip()
        if not key or km.get(key):
            continue
        # 형식 A: 한글명 (원문) (지은이|…)
        m = re.search(
            re.escape(key)
            + r"\s*[（(]\s*"
            + script_in_paren
            + r"\s*[）)]"
            + role_after,
            blob,
            re.MULTILINE,
        )
        if m:
            script = re.sub(r"\s+", " ", m.group(1)).strip()
            if _is_probable_orig_script(script):
                km[key] = script
                continue
        # 형식 B: 한글명 (원문) 만 — meta·목차 등
        m2 = re.search(
            re.escape(key) + r"\s*[（(]\s*" + script_in_paren + r"\s*[）)]",
            blob,
            re.MULTILINE,
        )
        if m2:
            script = re.sub(r"\s+", " ", m2.group(1)).strip()
            if _is_probable_orig_script(script):
                km[key] = script


def _aladin_author_overview_url_from_author_href(href: str) -> str | None:
    """상품 페이지의 저자 검색 링크에서 프로필(원문 표기) URL 생성."""
    h = (href or "").strip()
    if "AuthorSearch=" not in h:
        return None
    if "PublisherSearch" in h:
        return None
    if "wsearchresult.aspx" not in h.lower() and "wauthor_overview.aspx" not in h.lower():
        return None
    m = re.search(r"AuthorSearch=([^&\"']+)", h)
    if not m:
        return None
    return "https://www.aladin.co.kr/author/wauthor_overview.aspx?AuthorSearch=" + m.group(1)


def _parse_author_overview_name_cell(raw_html: str) -> str | None:
    """알라딘 저자 프로필(wauthor_overview) HTML에서 '이름:' 셀 텍스트."""
    t = html.unescape(raw_html)
    m = re.search(r"이름\s*:\s*</td>\s*<td[^>]*>\s*([^<]+)</td>", t)
    return m.group(1).strip() if m else None


def _ko_script_from_author_overview_cell(cell: str) -> tuple[str, str | None]:
    """'이치호 미치 (一穂ミチ)' → (이치호 미치, 一穂ミチ). 괄호 없으면 (cell, None)."""
    cell = re.sub(r"\s+", " ", (cell or "").strip())
    if "(" not in cell or ")" not in cell:
        return cell, None
    m = re.match(r"^(.+?)\s*\(\s*([^)]+)\s*\)\s*$", cell)
    if not m:
        return cell, None
    ko, script = m.group(1).strip(), m.group(2).strip()
    if not re.search(r"[\uac00-\ud7a3]", ko):
        return cell, None
    if _is_probable_orig_script(script):
        return ko, script
    return ko, None


def enrich_kanji_map_from_author_overview_pages(
    names: list[str],
    profile_urls: dict[str, str],
    km: dict,
) -> None:
    """
    상품 페이지에 링크된 저자별 프로필(wauthor_overview)을 열어
    '이름: 한글 (원문표기)'에서 한자·가나 원표기를 채움. (합저는 상품 HTML 본문에 없고 프로필에만 있는 경우가 많음)
    """
    for nm in names:
        key = (nm or "").strip()
        if not key or km.get(key):
            continue
        url = profile_urls.get(key)
        if not url:
            continue
        try:
            resp = requests.get(url, headers=ALADIN_HEADERS, timeout=12)
            resp.raise_for_status()
        except requests.RequestException:
            continue
        cell = _parse_author_overview_name_cell(resp.text)
        if not cell:
            continue
        ko, script = _ko_script_from_author_overview_cell(cell)
        if script and ko.strip() == key:
            km[key] = script
        time.sleep(0.12)


def _merge_kanji_from_foreign_author_string(author_str: str, names: list[str], km: dict) -> None:
    """
    외국도서 검색 author 예:
    '무라카미 하루키, 안자이 미즈마루, 村上春樹 文  安西水丸 绘 (지은이)'
    앞쪽 한글 블록 순서와 뒤쪽 한자 덩어리(2자 이상) 순서를 맞춤.
    """
    s = (author_str or "").strip()
    if not s:
        return
    parts = [re.sub(r"\([^)]*\)\s*$", "", p).strip() for p in s.split(",")]
    ko_blocks: list[str] = []
    cjk_parts: list[str] = []
    hangul_only = re.compile(r"^[ 가-힣·・]+$")
    for p in parts:
        if not p:
            continue
        if hangul_only.match(p) and re.search(r"[가-힣]", p):
            ko_blocks.append(re.sub(r"\s+", " ", p.strip()))
        else:
            cjk_parts.append(p)
    blob = "".join(cjk_parts)
    clusters = re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]{2,}", blob)
    if not clusters or not ko_blocks:
        return
    for i, ko in enumerate(ko_blocks):
        if i >= len(clusters):
            break
        if ko not in km and clusters[i]:
            km[ko] = clusters[i]


def enrich_kanji_map_from_foreign_title_search(orig_title: str | None, names: list[str], km: dict) -> None:
    """동일 원제(가나·한자) 외국도서 ItemSearch 결과 author 필드로 한자명 보완."""
    if not orig_title or not names:
        return
    ot = orig_title.strip()
    if not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", ot):
        return
    if not any(n and not km.get(n) for n in names):
        return
    params = {
        "ttbkey":       ALADIN_API_KEY,
        "Query":        ot,
        "QueryType":    "Title",
        "SearchTarget": "Foreign",
        "MaxResults":   8,
        "start":        1,
        "output":       "js",
        "Version":      "20131101",
    }
    try:
        resp = requests.get(ALADIN_SEARCH_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return
    raw_items = data.get("item") or []
    items = [raw_items] if isinstance(raw_items, dict) else raw_items
    for it in items[:8]:
        auth = html.unescape((it.get("author") or "").strip())
        if not auth:
            continue
        _merge_kanji_from_foreign_author_string(auth, names, km)
        if not any(n and not km.get(n) for n in names):
            break


def scrape_aladin_product(item_id: str) -> dict:
    """
    알라딘 상품 페이지 크롤링.
    반환: {
        "orig_title": str|None,
        "orig_author_en": str|None,
        "kanji_map": dict,       # {"한글명": "漢字名"}
        "is_east_asian": bool,
        "text_blob": str,       # 한글（漢字） 보완용 원문
        "author_overview_urls": dict[str, str],  # 한글 표기 → 저자 프로필 URL
    }
    """
    result = {
        "orig_title":             None,
        "orig_author_en":         None,
        "kanji_map":              {},
        "is_east_asian":          False,
        "text_blob":              "",
        "author_overview_urls":   {},
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

    profile_urls: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        overview = _aladin_author_overview_url_from_author_href(href)
        if not overview:
            continue
        nm = a.get_text(" ", strip=True)
        if nm and re.search(r"[\uac00-\ud7a3]", nm):
            profile_urls[nm] = overview
    result["author_overview_urls"] = profile_urls

    # ── 원제/원저자 영문명 추출 ──────────────────────

    # 방법 1: SearchWord 링크에서 추출 (ALL CAPS → 저자명, 나머지 → 원제)
    orig_link = soup.find("a", href=re.compile(r"SearchTarget=Foreign&SearchWord="))
    if orig_link:
        href  = orig_link.get("href", "")
        m = re.search(r"SearchWord=([^&\"]+)", href)
        if m:
            raw   = unquote(m.group(1).replace("+", " ")).strip()
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
        # meta author="앤디 위어, Andy Weir" — 본문 get_text()에 안 나오는 영문 원저자명
        if not result["orig_author_en"]:
            for p in content.split(","):
                p = p.strip()
                ws = p.split()
                if len(ws) >= 2 and all(re.match(r"^[A-Za-z.\-\']+$", w) for w in ws):
                    result["orig_author_en"] = p
                    break
        parts = [p.strip() for p in content.split(",")]
        used_cjk_idx: set[int] = set()
        for i, ko_name in enumerate(parts):
            if not (re.search(r"[\uac00-\ud7a3]", ko_name) and not CJK_PATTERN.search(ko_name)):
                continue
            kn = ko_name.strip()
            for j in range(i + 1, min(i + 8, len(parts))):
                if j in used_cjk_idx:
                    continue
                cand = parts[j].strip()
                if (
                    CJK_PATTERN.search(cand)
                    and not re.search(r"[\uac00-\ud7a3]", cand)
                    and len(cand) <= 36
                ):
                    if kn not in result["kanji_map"]:
                        result["kanji_map"][kn] = cand
                    used_cjk_idx.add(j)
                    break

    # 방법 4: 저자소개 밖·HTML 속성 등 — 전체 페이지에서 '한글 (English Name)' (API 없이 크롤만으로 보완)
    if not result["orig_author_en"]:
        hit = extract_english_author_after_korean_paren(page_text)
        if not hit:
            hit = extract_english_author_after_korean_paren(resp.text)
        if hit:
            result["orig_author_en"] = hit

    # SearchWord 등으로 원제 끝에 영문 저자명이 붙은 경우 제거 (예: Project Hail Mary Andy Weir)
    if result.get("orig_title") and result.get("orig_author_en"):
        t = result["orig_title"].strip()
        en = result["orig_author_en"].strip()
        if en and t.endswith(en):
            t = t[: -len(en)].rstrip(" -–—")
            if t:
                result["orig_title"] = remove_year(t)

    # ── 동아시아 저자 판별 (전역 네비 '일본도서' 등 제외) ───────────────────
    bio_slice = _aladin_page_text_skip_global_nav(page_text)
    if any(kw in bio_slice for kw in EAST_ASIA_KEYWORDS):
        result["is_east_asian"] = True

    result["text_blob"] = page_text + "\n" + resp.text[:150000]

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

    enrich_kanji_map_from_aladin_blob(
        scraped.get("text_blob") or "",
        [a["name"] for a in non_trans],
        kanji_map,
    )
    enrich_kanji_map_from_foreign_title_search(
        orig_title,
        [a["name"] for a in non_trans],
        kanji_map,
    )
    enrich_kanji_map_from_author_overview_pages(
        [a["name"] for a in non_trans],
        scraped.get("author_overview_urls") or {},
        kanji_map,
    )

    orig_cjk_or_kana = bool(
        (orig_title or "").strip()
        and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", orig_title)
    )

    # 각 저자별 한자명
    author_info = []
    for a in non_trans:
        author_info.append({
            "name":          a["name"],
            "kanji_name":    kanji_map.get(a["name"]),
            "is_east_asian": is_east_asian or orig_cjk_or_kana,
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


def marc700_second_indicator(name: str, role: str) -> str:
    """
    700 두 번째 지시자: 성명 구분이 드러나는 부출 → 1, 한국식 단명(닉네임 등)→ 0.
    지은이·저자 등 1저는 항상 1.
    """
    r = (role or "").strip()
    if r in PRIMARY_ROLES:
        return "1"
    n = (name or "").strip()
    if not n:
        return "1"
    if re.search(r"[A-Za-z]", n) and not re.search(r"[\uac00-\ud7a3]", n):
        return "1"
    if re.search(r"[\u3040-\u30ff]", n):
        return "1"
    if re.search(r"\s", n):
        return "1"
    core = re.sub(r"[\s·ㆍ]", "", n)
    if not core or not re.match(r"^[가-힣]+$", core):
        return "1"
    if len(core) <= 2:
        return "0"
    return "1"


def build_710(author: dict) -> str:
    return f"$a {author['name'].strip()}"


def build_900(
    orig_author_ko: str | None,
    is_east_asian: bool = False,
    kanji_name: str | None = None,
    orig_author_en: str | None = None,
    translation_book: bool = False,
    orig_title_has_cjk_or_kana: bool = False,
) -> str | None:
    """
    246 19(원서명)이 있는 번역·원서 맥락(translation_book)이면 한국어 표기 900은 반드시 출력.
    서양 원저: 한글 성·이름 도치. 동아시아·원표제에 가나/한자가 있으면 한글 표기는 도치 없음.
    """
    if not orig_author_ko:
        return None
    ko = orig_author_ko.strip()

    if kanji_name:
        ktype = kanji_type(kanji_name)
        # 번역서 + 동아시아·원표제 CJK 맥락: 900은 원표기를 한글로 풀어 쓴 음(가타카나→한글) 우선.
        if translation_book and (is_east_asian or orig_title_has_cjk_or_kana):
            gloss = _jp_script_reading_hangul(kanji_name.strip())
            if gloss:
                return f"900 10 $a {gloss}"
            return f"900 10 $a {ko}"
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
        if is_east_asian or orig_title_has_cjk_or_kana:
            return f"900 10 $a {ko}"
        reversed_name = korean_name_reverse(ko)
        if reversed_name:
            return f"900 10 $a {reversed_name}"
        return f"900 10 $a {ko}"

    if is_east_asian or orig_title_has_cjk_or_kana:
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
    import sys
    import traceback

    try:
        return _isbn_lookup()
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        err = {"error": f"서버 오류: {str(e)}", "exception_type": type(e).__name__}
        if os.environ.get("API_DEBUG", "").lower() in ("1", "true", "yes"):
            err["traceback"] = traceback.format_exc()[-8000:]
        try:
            body = json.dumps(err, ensure_ascii=False)
        except Exception:
            body = '{"error":"서버 오류(상세 직렬화 실패)"}'
        return Response(body, status=500, mimetype="application/json; charset=utf-8")


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
            if clean_subtitle(api_sub) == "":
                subtitle = ""
            else:
                subtitle = api_sub

    if not subtitle:
        for sep in (" - ", " – ", " : ", "：", ":"):
            if sep in title:
                t, s = title.split(sep, 1)
                t, s = t.strip(), s.strip()
                if clean_subtitle(s) == "":
                    title = t
                else:
                    title, subtitle = t, s
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

    if subtitle and clean_subtitle(subtitle) == "":
        subtitle = ""
    title = strip_award_suffix_from_title(title)

    # MARC 필드 생성
    field_245 = build_245(title, subtitle, authors)
    field_246 = build_246(orig_title)
    translation_book = field_246 is not None
    orig_title_has_cjk_or_kana = bool(
        (orig_title or "").strip()
        and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", orig_title)
    )

    # 500: 원저자명(한자·영문). 한자는 저자별, 로마자는 책 전체 한 줄만(복수 원저에 동일 영문 중복 방지).
    # 합저·일본 원서: 알라딘 HTML meta author 는 첫 저자+한자만 주는 경우가 많아, 나머지는 한글 표기로 500에 나란히 둠.
    jp_ctx_for_500 = orig_title_has_cjk_or_kana or (
        bool(author_info) and any(ai.get("is_east_asian") for ai in author_info)
    )
    n_missing_kanji = sum(1 for x in author_info if not x.get("kanji_name"))
    allow_roman_500 = bool(orig_author_en) and n_missing_kanji == 1
    roman_once = False
    orig_names_500 = []
    for ai in author_info:
        kanji = ai.get("kanji_name")
        if kanji:
            orig_names_500.append(kanji)
        elif translation_book and jp_ctx_for_500 and ai.get("name"):
            orig_names_500.append(ai["name"].strip())
        elif allow_roman_500 and orig_author_en and not roman_once:
            orig_names_500.append(orig_author_en.strip())
            roman_once = True
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
        # 한글·한자·가나가 섞인 문자열은 원제 등 오탐(예: 容疑者Xの献身 — 글자 X 때문에)이므로
        # '로마자 원저자명'으로 보지 않음.
        latin = bool(re.search(r"[A-Za-z]", rom)) and not re.search(
            r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]",
            rom,
        )
        if latin and (not first_ai.get("kanji_name") or translation_book):
            if first_ai.get("is_east_asian") or orig_title_has_cjk_or_kana:
                fields_700.append(f"700 1_ $a {rom}")
            else:
                fields_700.append(f"700 1_ $a {english_name_reverse(rom)}")
            skip_first_person_700 = True
        elif (
            translation_book
            and not latin
            and not first_ai.get("kanji_name")
            and not first_ai.get("is_east_asian")
            and not orig_title_has_cjk_or_kana
        ):
            kr = korean_name_reverse(first_ai["name"].strip())
            if kr:
                fields_700.append(f"700 1_ $a {kr}")
                skip_first_person_700 = True
    elif (
        first_ai
        and translation_book
        and not first_ai.get("kanji_name")
        and not orig_author_en
        and not first_ai.get("is_east_asian")
        and not orig_title_has_cjk_or_kana
    ):
        kr = korean_name_reverse(first_ai["name"].strip())
        if kr:
            fields_700.append(f"700 1_ $a {kr}")
            skip_first_person_700 = True

    first_ko_name = (first_ai or {}).get("name", "").strip()
    for a in persons:
        if skip_first_person_700 and a["name"].strip() == first_ko_name:
            skip_first_person_700 = False
            continue
        ind = marc700_second_indicator(a["name"], a.get("role", ""))
        fields_700.append(f"700 {ind}_ {build_700(a)}")

    # 710
    orgs       = [a for a in authors if a["is_org"]]
    fields_710 = [f"710 0_ {build_710(a)}" for a in orgs]

    # 900 — 원저자명(500과 같은 기준)을 못 구하면 500·900 모두 생략
    fields_900 = []
    jp_book_for_roman = orig_title_has_cjk_or_kana or (
        bool(author_info) and any(ai.get("is_east_asian") for ai in author_info)
    )
    if field_500:
        for idx, ai in enumerate(author_info):
            en_for_900 = None
            if (
                orig_author_en
                and not ai.get("kanji_name")
                and not jp_book_for_roman
                and (len(author_info) <= 1 or idx == 0)
            ):
                en_for_900 = orig_author_en
            f900 = build_900(
                ai["name"],
                ai["is_east_asian"],
                ai["kanji_name"],
                orig_author_en=en_for_900,
                translation_book=translation_book,
                orig_title_has_cjk_or_kana=orig_title_has_cjk_or_kana,
            )
            if f900:
                fields_900.append(f900)

    marc: dict[str, object] = {
        "f245": f"245 00 {field_245}",
        "f246": field_246,
        "f700": fields_700,
        "f710": fields_710,
    }
    if field_500:
        marc["f500"] = field_500
    if fields_900:
        marc["f900"] = fields_900

    payload = {
        "isbn13":     isbn13,
        "title":      title,
        "subtitle":   subtitle,
        "author_raw": author_str,
        "authors":    authors,
        "publisher":  item.get("publisher", ""),
        "pub_date":   item.get("pubDate", ""),
        "cover":      item.get("cover", ""),
        "marc":       marc,
    }
    try:
        body = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as ser_e:
        return jsonify({"error": f"응답 JSON 변환 실패: {ser_e}"}), 500
    return Response(body, mimetype="application/json; charset=utf-8")


@app.route("/")
def index():
    """루트 URL 직접 열 때 404 방지 — API 안내."""
    return jsonify({
        "service": "KORMARC API",
        "health": "/health",
        "isbn":   "/api/isbn?isbn=9788936434267",
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
