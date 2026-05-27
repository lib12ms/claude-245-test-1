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
  246 19  원서명 (번역서·알라딘만, 없으면 비움)
  500 __  원저자명 주기 (번역서), 감수 등 245 제외 역할 주기
  700 1_  개인명 부출(등록 성씨+이름 1~2음절·출생신고 이름 통계 등록)
  700 0_  개인명 부출(닉네임·필명·성씨 없음·이름 3음절 이상·미등록 이름 등, 실명 예외 목록 제외)
  710 0_  기관명 부출기입
  900 10  원저자 한글명 부출 (500 원문이 한자만일 때 한국 한자음; 가타카나·히라가나가 섞이면 생략; 500이 로마자인 서양 원저는 build_900 규칙)
"""

from __future__ import annotations

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import requests
from bs4 import BeautifulSoup
import html
import json
import re
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

from korean_surnames import KOREAN_SURNAMES, KOREAN_SURNAMES_BY_LENGTH
from korean_real_name_allowlist import KOREAN_REAL_NAME_ALLOWLIST as _KOREAN_REAL_NAME_ALLOWLIST
from korean_given_names import (
    korean_given_name_is_plausible,
    korean_given_names,
)

try:
    import hanja
    HANJA_AVAILABLE = True
except ImportError:
    HANJA_AVAILABLE = False

try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

try:
    import jaconv
    import pykakasi

    _PYKAKASI_JACONV = True
except ImportError:
    _PYKAKASI_JACONV = False

app = Flask(__name__)
CORS(app)


@app.errorhandler(Exception)
def _json_error_handler(exc: Exception) -> Response:
    """
    Streamlit 등 클라이언트가 항상 JSON으로 파싱할 수 있게 함.
    (Gunicorn 타임아웃 등으로 프록시가 HTML을 줄 때는 여기까지 오지 않을 수 있음.)
    """
    if isinstance(exc, HTTPException):
        code = int(exc.code or 500)
        desc = exc.description
        msg = desc if isinstance(desc, str) else (str(desc) if desc else str(exc))
        payload: dict[str, object] = {"error": (msg or getattr(exc, "name", None) or str(exc))}
    else:
        code = 500
        payload = {"error": f"서버 오류: {str(exc)}", "exception_type": type(exc).__name__}
        if os.environ.get("API_DEBUG", "").lower() in ("1", "true", "yes"):
            import sys
            import traceback

            traceback.print_exc(file=sys.stderr)
            payload["traceback"] = traceback.format_exc()[-8000:]
    try:
        body = json.dumps(payload, ensure_ascii=False)
    except Exception:
        body = '{"error":"서버 오류"}'
    return Response(body, status=code, mimetype="application/json; charset=utf-8")


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
    "저자": "지은이", "글": "지은이", "글쓴이": "지은이", "지은이": "지은이",
    "원작": "원작",
}

PRIMARY_ROLES = {"지은이", "저자", "글", "글쓴이", ""}

# 245 책임표시 제외 → 500 주기로 출력 (예: 500 __ $a 감수: …)
ROLES_EXCLUDED_FROM_245 = frozenset({"감수"})

TRANS_ROLES   = {"옮긴이", "역자", "번역", "편역"}

# 저자 문자열 괄호 안이 역할만인 경우(원문 표기 아님)
_KO_AUTHOR_PAREN_SKIP = frozenset({
    "지은이", "저자", "글", "글쓴이", "원작", "옮긴이", "역자", "번역", "편역",
    "그림", "그린이", "일러스트", "편저", "해설", "엮은이", "편집", "감수",
    "사진", "총서", "공저", "공동", "외", "외1", "외2",
})

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
    "草":"초","葉":"엽","根":"근","枝":"지",    "幹":"간",
    "暁":"효",
}

# 알라딘·일부 DB의 일본 인명 한자 오표기 보정 (曉→暁 등)
_JP_KANJI_CHAR_FIX: dict[str, str] = {
    "曉": "暁",
}


def _normalize_jp_kanji(s: str) -> str:
    t = (s or "").strip()
    for wrong, right in _JP_KANJI_CHAR_FIX.items():
        t = t.replace(wrong, right)
    return t


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


def _looks_like_roman_orig_display(s: str) -> bool:
    """500 토큰이 로마자 원저 표기인지(한글·한자·가나 없음)."""
    t = (s or "").strip()
    if not t:
        return False
    if re.search(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]", t):
        return False
    return bool(re.search(r"[A-Za-z]", t))


def _orig_500_token_has_hangul(s: str) -> bool:
    """500 목록에 넣으면 안 되는 한글 표기(서양 번역권)."""
    return bool(re.search(r"[\uac00-\ud7a3]", (s or "").strip()))


def _is_hangul_only_orig_name_for_500(s: str) -> bool:
    """한글 음역 표기만(한자·가나·로마자 없음) — 500 원저자명에 넣지 않음."""
    t = (s or "").strip()
    if not t or not re.search(r"[\uac00-\ud7a3]", t):
        return False
    if re.search(r"[\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbfA-Za-z]", t):
        return False
    return True


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
    yo: dict[str, str] = {
        "キャ": "캬", "キュ": "큐", "キョ": "쿄", "シャ": "샤", "シュ": "슈", "ショ": "쇼",
        "チャ": "차", "チュ": "추", "チョ": "초", "ニャ": "냐", "ニュ": "뉴", "ニョ": "뇨",
        "ヒャ": "햐", "ヒュ": "휴", "ヒョ": "효", "ミャ": "먀", "ミュ": "뮤", "ミョ": "묘",
        "リャ": "랴", "リュ": "류", "リョ": "료", "ギャ": "갸", "ギュ": "규", "ギョ": "교",
        "ジャ": "쟈", "ジュ": "주", "ジョ": "조", "ヂャ": "쟈", "ヂュ": "주", "ヂョ": "조",
        "ビャ": "뱌", "ビュ": "뷰", "ビョ": "뵤", "ピャ": "퍄", "ピュ": "표", "ピョ": "표",
        "デャ": "댜", "デュ": "듀", "デョ": "됴",
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


def _jp_name_reading_hangul(script: str) -> str | None:
    """
    500의 일본 원문 표기(한자·가나) → 일본어 음을 가타카나 경유 한글로(900용, 245 한글 음역과 구분).
    """
    if not _PYKAKASI_JACONV:
        return None
    s = (script or "").strip()
    if not s:
        return None
    s = s.translate({0x9751: 0x9752})  # 靑 → 青
    try:
        kks = pykakasi.kakasi()
        kks.setMode("J", "H")
        kks.setMode("K", "H")
        kks.setMode("H", "H")
        hira = kks.getConverter().do(s)
    except Exception:
        return None
    if not hira or re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", hira):
        return None
    kata = jaconv.hira2kata(hira)
    return _katakana_to_hangul(kata)


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


def _extract_paren_english_names_from_blob(text: str, limit: int = 220000) -> list[str]:
    """
    상세설명 등에서 '한글… (English…)' 괄호 안 영문명을 순서대로 수집.
    합집·다수 원저자(영문) 보강용. 이니셜 포함 단일 토큰도 허용.
    """
    if not text:
        return []
    chunk = text[:limit]
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(
        r"[\uac00-\ud7a3][^()\n]{0,52}\(\s*([^)\n]{2,160})\s*\)",
        chunk,
    ):
        inner = re.sub(r"\s+", " ", m.group(1)).strip()
        if not inner or re.search(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff]", inner):
            continue
        if not re.search(r"[A-Za-z]", inner):
            continue
        for piece in re.split(r",\s*", inner):
            p = piece.strip()
            if len(p) < 2:
                continue
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


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
                km[key] = _normalize_jp_kanji(script)
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
                km[key] = _normalize_jp_kanji(script)


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


def _english_from_author_overview_cell(cell: str) -> tuple[str, str | None]:
    """
    '네일로 홉킨슨 (Nalo Hopkinson)', 'N. K. 제미신 (N. K. Jemisin)' 등
    괄호 안이 로마자만일 때 (앞 표기, 영문) 반환. 한자·가나 병기면 (outer, None).
    """
    cell = re.sub(r"\s+", " ", (cell or "").strip())
    if "(" not in cell or ")" not in cell:
        return cell, None
    m = re.match(r"^(.+?)\s*\(\s*([^)]+)\s*\)\s*$", cell)
    if not m:
        return cell, None
    outer, inner = m.group(1).strip(), m.group(2).strip()
    if not re.search(r"[A-Za-z]", inner):
        return outer, None
    if re.search(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]", inner):
        return outer, None
    return outer, inner


def _is_paren_latin_roman_name(s: str) -> bool:
    """괄호 안이 로마자 표기의 사람 이름으로 보일 때(한글·한자·가나 없음)."""
    t = re.sub(r"\s+", " ", (s or "").strip())
    if len(t) < 2:
        return False
    if re.search(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]", t):
        return False
    if t in _KO_AUTHOR_PAREN_SKIP:
        return False
    return bool(re.search(r"[A-Za-z]", t))


def _norm_author_display(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _author_overview_url_for_name(name: str, profile_urls: dict[str, str]) -> str | None:
    """상품 페이지 링크 텍스트와 parse_authors 이름이 공백만 다를 때도 매칭."""
    key = _norm_author_display(name)
    if not key:
        return None
    if key in profile_urls:
        return profile_urls[key]
    for k, u in profile_urls.items():
        if _norm_author_display(k) == key:
            return u
    return None


def _fetch_author_overview_name_cell(url: str) -> str | None:
    """저자 프로필 HTML에서 '이름:' 셀만 가져옴(재시도 1회)."""
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=ALADIN_HEADERS, timeout=8)
            resp.raise_for_status()
            return _parse_author_overview_name_cell(resp.text)
        except requests.RequestException:
            if attempt == 0:
                time.sleep(0.12)
    return None


def enrich_kanji_map_from_author_overview_pages(
    names: list[str],
    profile_urls: dict[str, str],
    km: dict,
    english_map: dict | None = None,
) -> None:
    """
    상품 페이지에 링크된 저자별 프로필(wauthor_overview)을 열어
    '이름: 한글 (원문표기)'에서 한자·가나 원표기를 채움. (합저는 상품 HTML 본문에 없고 프로필에만 있는 경우가 많음)
    english_map 이 있으면 같은 페이지의 '한글 (English)' 로마자 병기도 함께 채움(한 HTTP로 처리).
    합저 대비: 순차 요청 대신 소수 스레드로 병렬 요청(총 대기 시간·Render 타임아웃 완화).
    """
    tasks: list[tuple[str, str, bool, bool]] = []
    for nm in names:
        key = (nm or "").strip()
        if not key:
            continue
        need_k = not km.get(key)
        need_e = english_map is not None and not english_map.get(key)
        if not need_k and not need_e:
            continue
        url = _author_overview_url_for_name(key, profile_urls)
        if not url:
            continue
        tasks.append((key, url, need_k, need_e))

    if not tasks:
        return

    def _one(task: tuple[str, str, bool, bool]) -> tuple[str, str | None, bool, bool]:
        key, url, need_k, need_e = task
        return key, _fetch_author_overview_name_cell(url), need_k, need_e

    workers = min(
        max(1, len(tasks)),
        int(os.environ.get("ALADIN_OVERVIEW_WORKERS", "6")),
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, t) for t in tasks]
        for fut in as_completed(futures):
            key, cell, need_k, need_e = fut.result()
            if not cell:
                continue
            ko, script = _ko_script_from_author_overview_cell(cell)
            if need_k and script and _norm_author_display(ko) == _norm_author_display(key):
                km[key] = _normalize_jp_kanji(script)
            outer_en, en_inner = _english_from_author_overview_cell(cell)
            if (
                english_map is not None
                and need_e
                and en_inner
                and _norm_author_display(outer_en) == _norm_author_display(key)
            ):
                english_map[key] = en_inner


def enrich_kanji_map_from_book_author_keyword(names: list[str], km: dict) -> None:
    """
    국내도서 키워드 검색으로 저자 문자열 속 '한글명 (원문표기)'을 찾아 한자명 보완.
    (합저 중 프로필 URL이 없거나 요청 실패한 인물 보강)
    """
    for nm in names:
        key = (nm or "").strip()
        if not key or km.get(key):
            continue
        params = {
            "ttbkey":       ALADIN_API_KEY,
            "Query":        key,
            "QueryType":    "Keyword",
            "SearchTarget": "Book",
            "MaxResults":   12,
            "start":        1,
            "output":       "js",
            "Version":      "20131101",
        }
        try:
            resp = requests.get(ALADIN_SEARCH_URL, params=params, timeout=11)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError, KeyError):
            time.sleep(0.06)
            continue
        raw = data.get("item") or []
        items = [raw] if isinstance(raw, dict) else raw
        esc = re.escape(key)
        pat = re.compile(esc + r"\s*[（(]\s*([^)]+)\s*[）)]")
        for it in items[:12]:
            auth = html.unescape((it.get("author") or "").strip())
            if not auth or key not in auth:
                continue
            for m in pat.finditer(auth):
                inner = re.sub(r"\s+", " ", m.group(1)).strip()
                if inner in _KO_AUTHOR_PAREN_SKIP or not _is_probable_orig_script(inner):
                    continue
                km[key] = inner
                break
            if km.get(key):
                break
        time.sleep(0.08)


def enrich_english_map_from_book_author_keyword(names: list[str], em: dict) -> None:
    """
    국내도서 키워드 검색으로 '한글명 (English Name)'을 찾아 english_map 보완.
    (저자 프로필에 괄호 영문이 없는 합저 인물 보강)
    """
    for nm in names:
        key = (nm or "").strip()
        if not key or em.get(key):
            continue
        params = {
            "ttbkey":       ALADIN_API_KEY,
            "Query":        key,
            "QueryType":    "Keyword",
            "SearchTarget": "Book",
            "MaxResults":   12,
            "start":        1,
            "output":       "js",
            "Version":      "20131101",
        }
        try:
            resp = requests.get(ALADIN_SEARCH_URL, params=params, timeout=11)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError, KeyError):
            time.sleep(0.06)
            continue
        raw = data.get("item") or []
        items = [raw] if isinstance(raw, dict) else raw
        esc = re.escape(key)
        pat = re.compile(esc + r"\s*[（(]\s*([^)]+)\s*[）)]")

        def scan_blob(blob: str) -> bool:
            b = html.unescape(blob or "")
            if key not in b:
                return False
            for m in pat.finditer(b):
                inner = re.sub(r"\s+", " ", m.group(1)).strip()
                if not _is_paren_latin_roman_name(inner):
                    continue
                em[key] = inner
                return True
            return False

        for it in items[:12]:
            for fld in ("author", "title", "description"):
                if scan_blob((it.get(fld) or "").strip()):
                    break
            else:
                continue
            break
        time.sleep(0.08)


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
            km[ko] = _normalize_jp_kanji(clusters[i])


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
            return


_INTRO_ROLE_ALIASES: dict[str, str] = {
    "지은이": "지은이", "저자": "지은이", "글": "지은이", "글쓴이": "지은이",
    "옮긴이": "옮긴이", "역자": "옮긴이", "번역": "옮긴이", "편역": "옮긴이",
    "그림": "그린이", "그린이": "그린이", "원작": "원작", "편저": "편저", "감수": "감수",
}

_INTRO_ENTRY_RE = re.compile(
    r"([가-힣][가-힣\s·ㆍ・]{0,42}[가-힣])\s*"
    r"(?:[（(][^)）]{1,52}[）)])?\s*"
    r"[（(]\s*(지은이|저자|글|글쓴이|옮긴이|역자|번역|편역|그림|그린이|원작|편저|감수)\s*[）)]",
)


def _best_author_intro_container(soup: BeautifulSoup):
    """'저자 및 역자소개' 본문이 가장 많이 들어 있는 div."""
    best = None
    best_hits = 0
    for div in soup.find_all("div"):
        txt = div.get_text(" ", strip=True)
        if "소개" not in txt:
            continue
        if "저자" not in txt and "역자" not in txt and "지은이" not in txt:
            continue
        hits = (
            txt.count("(지은이)")
            + txt.count("(옮긴이)")
            + txt.count("(역자)")
            + txt.count("（지은이）")
            + txt.count("（옮긴이）")
        )
        if hits >= 2 and hits > best_hits:
            best_hits = hits
            best = div
    return best


def parse_intro_author_persons(soup: BeautifulSoup) -> list[dict]:
    """
    저자 및 역자소개 블록에서 '이름 (원문)? (역할)' 행을 순서대로 파싱.
    parse_authors와 동일한 dict 형태: {"name","role","is_org"}
    """
    container = _best_author_intro_container(soup)
    if not container:
        return []
    raw = container.get_text("\n")
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if len(line) < 4:
            continue
        m = _INTRO_ENTRY_RE.search(line)
        if not m:
            continue
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        if len(name) < 2:
            continue
        role_raw = m.group(2).strip()
        role = _INTRO_ROLE_ALIASES.get(role_raw, role_raw)
        dedup = (name, role)
        if dedup in seen:
            continue
        seen.add(dedup)
        out.append({"name": name, "role": role, "is_org": is_org(name)})
    return out


_INTRO_KO_EN_ROLE_RE = re.compile(
    r"(.{2,80}?) \(([A-Za-z][A-Za-z0-9 ,.'\-]{1,120})\) \((지은이|저자|글|엮은이|그린이|원작|편저|감수)\)"
)


def parse_intro_author_english_pairs(soup: BeautifulSoup, page_text: str) -> list[dict]:
    """
    저자소개 등에서 '표기 (English) (지은이|엮은이|…)' 를 찾음 (줄바꿈·<br> 등으로 끊겨도 동작하도록 공백 정규화 후 검색).
    500 원저자명을 영문만 쓰기 위한 한글 표기 → 영문 매핑.
    """
    chunks: list[str] = []
    container = _best_author_intro_container(soup)
    if container:
        chunks.append(container.get_text("\n"))
    bio = _aladin_page_text_skip_global_nav(page_text)
    if bio and len(bio) > 80:
        chunks.append(bio)
    src = "\n".join(chunks)
    one = re.sub(r"\s+", " ", src.replace("\r", " ").replace("\n", " "))
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in _INTRO_KO_EN_ROLE_RE.finditer(one):
        ko = re.sub(r"\s+", " ", m.group(1).strip())
        en = re.sub(r"\s+", " ", m.group(2).strip())
        role_raw = m.group(3).strip()
        role = _INTRO_ROLE_ALIASES.get(role_raw, role_raw)
        if len(ko) < 2 or len(en) < 2:
            continue
        if not re.fullmatch(r"[A-Za-z0-9 ,.'\-\s]+", en):
            continue
        if not (re.search(r"[\uac00-\ud7a3]", ko) or re.search(r"[A-Z]\.", ko)):
            continue
        key = (ko.lower(), en.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"ko": ko, "en": en, "role": role})
    return out


def _norm_author_lookup_key(n: str) -> str:
    return re.sub(r"\s+", " ", (n or "").strip()).lower()


def try_orig_names_500_from_intro_english_pairs(
    translation_book: bool,
    jp_ctx_for_500: bool,
    author_info: list[dict],
    pairs: list[dict],
) -> list[str] | None:
    """author_info 순서대로 영문 원저자명을 모두 찾으면 그 리스트만, 아니면 None."""
    if not translation_book or jp_ctx_for_500 or not author_info or not pairs:
        return None
    by_ko: dict[str, str] = {}
    for p in pairs:
        if p.get("role") in TRANS_ROLES:
            continue
        ko = _norm_author_lookup_key(p.get("ko", ""))
        en = (p.get("en") or "").strip()
        if ko and en:
            by_ko.setdefault(ko, en)
    out: list[str] = []
    for ai in author_info:
        nk = _norm_author_lookup_key(ai.get("name", ""))
        en = by_ko.get(nk)
        if not en:
            return None
        out.append(en)
    return out


def try_orig_names_500_from_profile_english(
    translation_book: bool,
    jp_ctx_for_500: bool,
    author_info: list[dict],
) -> list[str] | None:
    """
    저자 프로필 등으로 채운 english_name 으로 500용 리스트.
    전원 영문이면 영문만; 한 명만 비었을 때는 알라딘에 괄호 영문이 없는 경우가 많아
    그 한 명만 한글 표기로 채운다(나머지는 영문 유지).
    """
    if not translation_book or jp_ctx_for_500 or not author_info:
        return None
    out: list[str] = []
    miss: list[int] = []
    for i, ai in enumerate(author_info):
        en = (ai.get("english_name") or "").strip()
        if en:
            out.append(en)
        else:
            miss.append(i)
            out.append("")
    if not miss:
        return out
    if len(miss) != 1:
        return None
    i0 = miss[0]
    ko = (author_info[i0].get("name") or "").strip()
    if not ko:
        return None
    out[i0] = ko
    return out


def _norm_searchword_token(tok: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (tok or "").lower())


def _searchword_drop_title_prefix(parts: list[str], title_hint: str | None) -> list[str]:
    """SearchWord 토큰 앞부분에서 알라딘 API 등으로 이미 아는 영문 원제 단어를 제거."""
    if not parts or not (title_hint or "").strip():
        return parts
    hint_toks: list[str] = []
    for w in re.split(r"[\s:/–—\-]+", title_hint):
        t = _norm_searchword_token(w)
        if t:
            hint_toks.append(t)
    if not hint_toks:
        return parts
    i, j = 0, 0
    while i < len(parts) and j < len(hint_toks):
        pt = _norm_searchword_token(parts[i])
        if not pt:
            i += 1
            continue
        if pt == hint_toks[j]:
            i += 1
            j += 1
        else:
            break
    return parts[i:]


def _merge_searchword_initials(parts: list[str]) -> list[str]:
    """N K Jemisin → N.K. Jemisin (SearchWord가 이니셜을 끊은 경우)."""
    out: list[str] = []
    i = 0
    while i < len(parts):
        letters = re.sub(r"[^A-Za-z]", "", parts[i])
        if letters.isupper() and len(letters) == 1:
            initials: list[str] = []
            j = i
            while j < len(parts):
                lj = re.sub(r"[^A-Za-z]", "", parts[j])
                if lj.isupper() and len(lj) == 1:
                    initials.append(lj)
                    j += 1
                else:
                    break
            if len(initials) >= 2:
                out.append(".".join(initials) + ".")
                i = j
                continue
        out.append(parts[i])
        i += 1
    return out


def _titleize_foreign_caps_token(tok: str) -> str:
    t = (tok or "").strip()
    if not t:
        return t
    if re.fullmatch(r"(?:[A-Z]\.){2,}", t.replace(" ", "")):
        return t if t.endswith(".") else t + "."
    core = re.sub(r"[^A-Za-z]", "", t)
    if core.isupper() and len(core) >= 2:
        return core[:1].upper() + core[1:].lower()
    if "-" in t or "'" in t:
        return to_title_case(t)
    return t


def _orig_author_en_from_searchword_parts(parts: list[str]) -> str | None:
    if not parts:
        return None
    bits = [_titleize_foreign_caps_token(p) for p in parts if p.strip()]
    bits = [b for b in bits if b]
    return " ".join(bits).strip() or None


def scrape_aladin_product(item_id: str, orig_title_hint: str | None = None) -> dict:
    """
    알라딘 상품 페이지 크롤링.
    반환: {
        "orig_title": str|None,
        "orig_author_en": str|None,
        "kanji_map": dict,       # {"한글명": "漢字名"}
        "is_east_asian": bool,
        "text_blob": str,       # 한글（漢字） 보완용 원문
        "author_overview_urls": dict[str, str],  # 한글 표기 → 저자 프로필 URL
        "intro_persons": list[dict],  # 저자·역자소개 HTML에서 파싱 (700 보강용)
        "intro_author_pairs": list[dict],  # 한글 (영문) (역할) — 500 영문 원저자명용
    }
    """
    result = {
        "orig_title":             None,
        "orig_author_en":         None,
        "kanji_map":              {},
        "is_east_asian":          False,
        "text_blob":              "",
        "author_overview_urls":   {},
        "intro_persons":          [],
        "intro_author_pairs":     [],
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

    # 방법 1: SearchWord 링크 — API로 이미 아는 영문 원제가 있으면 접두 제거 후 원저자만 추출
    orig_link = soup.find("a", href=re.compile(r"SearchTarget=Foreign&SearchWord="))
    if orig_link:
        href  = orig_link.get("href", "")
        m = re.search(r"SearchWord=([^&\"]+)", href)
        if m:
            raw   = unquote(m.group(1).replace("+", " ")).strip()
            parts = raw.split()
            hint  = (orig_title_hint or result.get("orig_title") or "").strip()
            if hint:
                rest = _searchword_drop_title_prefix(parts, hint)
                rest = _merge_searchword_initials(rest)
                if rest:
                    en = _orig_author_en_from_searchword_parts(rest)
                    if en:
                        result["orig_author_en"] = en
            else:
                merged = _merge_searchword_initials(parts)
                author_parts = [
                    p
                    for p in merged
                    if re.sub(r"[-']", "", p).isupper()
                    and len(re.sub(r"[^A-Za-z]", "", p)) > 1
                ]
                title_parts = [p for p in merged if p not in author_parts]
                if title_parts:
                    result["orig_title"] = remove_year(" ".join(title_parts))
                if author_parts:
                    result["orig_author_en"] = _orig_author_en_from_searchword_parts(author_parts)

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
    result["intro_persons"] = parse_intro_author_persons(soup)
    result["intro_author_pairs"] = parse_intro_author_english_pairs(soup, page_text)

    return result


# ─────────────────────────────────────────────
# 원제·원저자 수집 메인 로직
# ─────────────────────────────────────────────
def fetch_gemini_orig_info(title: str, author_name: str, isbn13: str) -> dict:
    """
    Gemini API로 번역서의 원서명·원저자 영문명을 조회합니다.
    알라딘·국중에서 정보를 못 가져왔을 때만 호출됩니다.
    반환: {"orig_title": str|None, "orig_author_en": str|None}
    """
    result = {"orig_title": None, "orig_author_en": None}
    if not _GEMINI_AVAILABLE or not GEMINI_API_KEY:
        return result

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        prompt = f"""다음 번역서의 원서명과 원저자 영문명을 알려주세요.
책 제목(한국어): {title}
저자(한국어): {author_name}
ISBN-13: {isbn13}

반드시 아래 JSON 형식으로만 답하세요. 모르면 null로 답하세요.
다른 설명은 절대 쓰지 마세요.

{{"orig_title": "원서명 또는 null", "orig_author_en": "원저자 영문명 또는 null"}}"""

        response = model.generate_content(prompt)
        text = response.text.strip()

        # JSON 파싱
        # ```json ... ``` 형태 제거
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        data = json.loads(text)

        orig_title = data.get("orig_title")
        orig_author_en = data.get("orig_author_en")

        if orig_title and orig_title != "null":
            result["orig_title"] = remove_year(orig_title.strip())
        if orig_author_en and orig_author_en != "null":
            result["orig_author_en"] = orig_author_en.strip()

    except Exception:
        pass

    return result


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
    scraped       = scrape_aladin_product(item_id, orig_title_hint=orig_title)
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
    english_map: dict[str, str] = {}
    enrich_kanji_map_from_author_overview_pages(
        [a["name"] for a in non_trans],
        scraped.get("author_overview_urls") or {},
        kanji_map,
        english_map=english_map,
    )
    # 원제에 한자·가나가 없으면(서양 원서 등) 저자별 국내도서 키워드 한자 보강은 거의 항상 헛검색·지연만 유발 → 생략.
    orig_title_has_cjk_for_kanji_kw = bool(
        (orig_title or "").strip()
        and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", orig_title)
    )
    if orig_title_has_cjk_for_kanji_kw:
        enrich_kanji_map_from_book_author_keyword(
            [a["name"] for a in non_trans],
            kanji_map,
        )
    enrich_english_map_from_book_author_keyword(
        [a["name"] for a in non_trans],
        english_map,
    )

    orig_cjk_or_kana = bool(
        (orig_title or "").strip()
        and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", orig_title)
    )

    # 3순위: Gemini 폴백 — 원제·원저자 영문명 중 하나라도 없으면 호출
    if not orig_title or not orig_author_en:
        gemini = fetch_gemini_orig_info(title, primary_name, isbn13)
        if not orig_title and gemini["orig_title"]:
            orig_title = gemini["orig_title"]
        if not orig_author_en and gemini["orig_author_en"]:
            orig_author_en = gemini["orig_author_en"]

    # 각 저자별 한자명·프로필 괄호 영문 병기
    author_info = []
    for a in non_trans:
        nm = a["name"]
        en_prof = english_map.get(nm)
        if not en_prof:
            nk = _norm_author_display(nm)
            for k, v in english_map.items():
                if _norm_author_display(k) == nk:
                    en_prof = v
                    break
        author_info.append({
            "name":          nm,
            "kanji_name":    kanji_map.get(nm),
            "english_name":  en_prof,
            "is_east_asian": is_east_asian or orig_cjk_or_kana,
        })

    return {
        "orig_title":     orig_title,
        "orig_author_en": orig_author_en,
        "author_info":    author_info,
        "intro_persons":  scraped.get("intro_persons") or [],
        "intro_author_pairs": scraped.get("intro_author_pairs") or [],
    }


# ─────────────────────────────────────────────
# 245 책임표시
# ─────────────────────────────────────────────
def _role_label_for_245(role: str) -> str:
    """245 책임표시에 쓸 역할어."""
    r = (role or "").strip()
    if not r or r in PRIMARY_ROLES:
        return "지은이"
    return ROLE_LABEL.get(r, r)


def _role_excluded_from_245(role: str) -> bool:
    """245 $d/$e 책임표시에서 빼고 500 주기로 보낼 역할."""
    r = (role or "").strip()
    if r in ROLES_EXCLUDED_FROM_245:
        return True
    return _role_label_for_245(r) in ROLES_EXCLUDED_FROM_245


def _marc_suffix_from_responsibility_pairs(pairs: list[tuple[str, str]]) -> str | None:
    """
    (역할라벨, 이름) 목록 → MARC 책임표시.
    첫 저자 /$d, 같은 역할 추가 ,$e, 역할 변경 ;$e.
    """
    chunks: list[str] = []
    prev_label: str | None = None
    for label, name in pairs:
        if label in ROLES_EXCLUDED_FROM_245:
            continue
        name = name.strip()
        if not name:
            continue
        if not chunks:
            chunks.append(f"/$d {label}: {name}")
        elif label == prev_label:
            chunks.append(f" ,$e {name}")
        else:
            chunks.append(f" ;$e {label}: {name}")
        prev_label = label
    return "".join(chunks) if chunks else None


def _responsibility_pairs_from_authors(authors: list[dict]) -> list[tuple[str, str]]:
    """책임표시용 (역할라벨, 이름) — 개인 저자만, 알라딘 등장 순서."""
    pairs: list[tuple[str, str]] = []
    for a in authors:
        if a.get("is_org"):
            continue
        name = (a.get("name") or "").strip()
        if not name:
            continue
        if _role_excluded_from_245(a.get("role", "")):
            continue
        pairs.append((_role_label_for_245(a.get("role", "")), name))
    return pairs


def _build_responsibility_marc_suffix_from_authors(authors: list[dict]) -> str | None:
    """
    245 책임표시 MARC 접두.
    예: /$d 지은이: N. K. 제미신 ,$e 리베카 로언호스 ;$e 옮긴이: 이나경
    """
    return _marc_suffix_from_responsibility_pairs(_responsibility_pairs_from_authors(authors))


def _responsibility_plain_to_marc_suffix(text: str) -> str | None:
    """'지은이: A, B ; 옮긴이: C' → '/$d 지은이: A ,$e B ;$e 옮긴이: C'."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return None
    if t.startswith("/$"):
        return t
    pairs: list[tuple[str, str]] = []
    parts = [p.strip() for p in re.split(r"\s*;\s*", t) if p.strip()]
    for part in parts:
        m = re.match(r"^([^:]+):\s*(.+)$", part)
        if m:
            label = m.group(1).strip()
            for name in re.split(r"\s*,\s*", m.group(2).strip()):
                if name.strip():
                    pairs.append((label, name.strip()))
        elif part:
            label = pairs[-1][0] if pairs else "지은이"
            pairs.append((label, part))
    return _marc_suffix_from_responsibility_pairs(pairs)


def _split_title_subtitle_for_245(title: str, subtitle: str) -> tuple[str, str]:
    """
    표제 문자열에서 $a·$b 분리.
    예: '카프네 = Cafuné : 아베 아키코 장편소설' → ('카프네', '아베 아키코 장편소설')
    """
    t = strip_award_suffix_from_title(remove_series((title or "").strip()))
    sub = clean_subtitle((subtitle or "").strip())

    m = re.match(r"^(.+?)\s*=\s*[^:：]+[:：]\s*(.+)$", t)
    if m:
        a, b = m.group(1).strip(), clean_subtitle(m.group(2).strip())
        if b:
            return a, b

    for sep in (" : ", "：", ":"):
        if sep not in t:
            continue
        a, b = t.split(sep, 1)
        a, b = a.strip(), clean_subtitle(b.strip())
        if b:
            return a, b

    if sub:
        return t, sub
    return t, ""


# ─────────────────────────────────────────────
# MARC 필드 빌더
# ─────────────────────────────────────────────
def build_245(
    title: str,
    subtitle: str,
    authors: list[dict],
    responsibility: str | None = None,
) -> str:
    a_part, b_part = _split_title_subtitle_for_245(title, subtitle)

    field = f"$a {a_part}"
    if b_part:
        field += f" :$b {b_part}"

    resp_plain = (responsibility or "").strip()
    resp_marc = (
        _responsibility_plain_to_marc_suffix(resp_plain)
        if resp_plain
        else _build_responsibility_marc_suffix_from_authors(authors)
    )
    if resp_marc:
        field += f" {resp_marc}"

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


def build_500_role_notes(authors: list[dict]) -> list[str]:
    """245 제외 역할(감수 등) → 500 __ $a 감수: 이름 …"""
    by_role: dict[str, list[str]] = {}
    for a in authors:
        if a.get("is_org"):
            continue
        role = (a.get("role") or "").strip()
        if not _role_excluded_from_245(role):
            continue
        name = (a.get("name") or "").strip()
        if not name:
            continue
        label = _role_label_for_245(role)
        by_role.setdefault(label, []).append(name)
    fields: list[str] = []
    for label in sorted(by_role):
        names = by_role[label]
        fields.append(f"500 __ $a {label}: {', '.join(names)}")
    return fields


def personal_name_for_700_field(name: str) -> str:
    """
    700 $a용 표기. 필명(본명) 형태면 괄호 안 한글 본명은 제거하고 앞 표기만 사용.
    예: 로사장(김다솔) → 로사장. (지은이)·(村上春樹)·(Nalo Hopkinson) 등은 유지.
    """
    n = (name or "").strip()
    m = re.match(r"^(.+?)\s*[(（]\s*([^)）]+)\s*[)）]\s*$", n)
    if not m:
        return n
    outer, inner = m.group(1).strip(), m.group(2).strip()
    if not outer or inner in _KO_AUTHOR_PAREN_SKIP:
        return n
    if _is_probable_orig_script(inner):
        return n
    if _is_paren_latin_roman_name(inner):
        return outer if re.search(r"[\uac00-\ud7a3]", outer) else n
    if re.fullmatch(r"[가-힣·ㆍ\s]{2,24}", inner) and re.search(r"[가-힣]", inner):
        return outer
    return n


def build_700(author: dict) -> str:
    name = personal_name_for_700_field(author["name"].strip())
    if re.search(r"[A-Za-z]", name) and not re.search(r"[\uac00-\ud7a3]", name):
        name = english_name_reverse(name)
    return f"$a {name}"


# 700 0_ 판별: 필명·직함 접미, 반복 음절(홍홍), 비정상 이름 길이
_NAME_TITLE_SUFFIXES = (
    "선생님", "교수님", "박사", "교수", "선생", "작가", "기자", "님", "씨",
)

def _strip_pen_name_affixes(name: str) -> str:
    """…김홍홍박사 → 김홍홍 등 앞뒤 장식·직함 제거."""
    n = (name or "").strip()
    n = re.sub(r"^[.…·※\s]+", "", n)
    n = re.sub(r"[.…]+$", "", n)
    for suf in sorted(_NAME_TITLE_SUFFIXES, key=len, reverse=True):
        if n.endswith(suf) and len(n) > len(suf):
            n = n[:-len(suf)]
    return n.strip()


def _split_korean_surname_given(core: str) -> tuple[str, str] | None:
    """복성(남궁·제갈 등) 우선 분리."""
    for sur in KOREAN_SURNAMES_BY_LENGTH:
        if core.startswith(sur) and len(core) > len(sur):
            return sur, core[len(sur) :]
    return None


def _given_part_looks_valid(given: str) -> bool:
    """이름 부분: 한글 1~2음절, 동일 글자 반복(홍홍) 제외."""
    if not given or not re.match(r"^[가-힣]+$", given):
        return False
    if len(given) > 2:
        return False
    if len(given) == 2 and given[0] == given[1]:
        return False
    return True


def korean_personal_name_is_structured(name: str) -> bool:
    """
    등록 성씨 + 이름(1~2음절)으로 성·이름 구분이 가능하면 True → 700 1_.
    김치찌개(이름 3음절), 철수(성씨 없음), …김홍홍박사 등은 False → 700 0_.
    """
    raw = (name or "").strip()
    if not raw or not re.search(r"[가-힣]", raw):
        return False

    core_check = re.sub(r"[\s·ㆍ]", "", _strip_pen_name_affixes(raw))
    if core_check in _KOREAN_REAL_NAME_ALLOWLIST:
        return True

    if re.search(r"\s", raw):
        parts = [p for p in re.split(r"\s+", raw) if p]
        if len(parts) >= 2 and re.match(r"^[가-힣]+$", parts[0]):
            if parts[0] in KOREAN_SURNAMES:
                given = "".join(
                    p for p in parts[1:] if re.match(r"^[가-힣]+$", p)
                )
                if len(parts[0]) == 1 and len(parts[0]) + len(given) < 3:
                    return False
                if len(parts[0]) == 2 and len(parts[0]) + len(given) < 4:
                    return False
                return _given_part_looks_valid(given)
        return True

    core = re.sub(r"[\s·ㆍ]", "", _strip_pen_name_affixes(raw))
    if not core or not re.match(r"^[가-힣]+$", core):
        return False

    split = _split_korean_surname_given(core)
    if not split:
        return False
    sur, given = split
    if sur not in KOREAN_SURNAMES:
        return False
    if len(sur) == 1 and len(core) < 3:
        return False
    if len(sur) == 2 and len(core) < 4:
        return False
    return _given_part_looks_valid(given)


def is_korean_person_by_surname(name: str) -> bool:
    """등록 성씨(korean_surnames)로 한국인 저자·옮긴이 여부."""
    n = personal_name_for_700_field((name or "").strip())
    if not re.search(r"[가-힣]", n):
        return False
    core = re.sub(r"[\s·ㆍ]", "", _strip_pen_name_affixes(n))
    if core in _KOREAN_REAL_NAME_ALLOWLIST:
        return True
    split = _split_korean_surname_given(core)
    if split and split[0] in KOREAN_SURNAMES:
        return True
    parts = [p for p in re.split(r"\s+", n) if p]
    if parts and parts[0] in KOREAN_SURNAMES and re.match(r"^[가-힣]+$", parts[0]):
        return True
    return False


def _name_uses_roman_or_hanja(name: str) -> bool:
    """로마자·한자(가나 제외 CJK) 표기 — 한국인 한글 외자 규칙에서 제외."""
    n = (name or "").strip()
    if re.search(r"[A-Za-z]", n):
        return True
    if re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", n):
        return True
    return False


def _extract_korean_given_part(name: str) -> str | None:
    """등록 성씨 분리 후 이름(뒷부분) 문자열. 실명 예외·분리 불가면 None."""
    n = personal_name_for_700_field((name or "").strip())
    if not re.search(r"[가-힣]", n):
        return None
    core = re.sub(r"[\s·ㆍ]", "", _strip_pen_name_affixes(n))
    if core in _KOREAN_REAL_NAME_ALLOWLIST:
        return None
    if re.search(r"\s", n):
        parts = [p for p in re.split(r"\s+", n) if p]
        if len(parts) >= 2 and parts[0] in KOREAN_SURNAMES:
            given = "".join(p for p in parts[1:] if re.match(r"^[가-힣]+$", p))
            return given or None
        return None
    if not core or not re.match(r"^[가-힣]+$", core):
        return None
    split = _split_korean_surname_given(core)
    if not split or split[0] not in KOREAN_SURNAMES:
        return None
    return split[1]


def _korean_given_syllable_count(name: str) -> int | None:
    """등록 성씨 분리 후 이름(뒷부분) 한글 음절 수. 분리 불가면 None."""
    given = _extract_korean_given_part(name)
    return len(given) if given else None


def _korean_given_not_in_birth_registry(
    name: str,
    *,
    translation_book: bool = False,
    author_info: list | None = None,
) -> bool:
    """
    형식상 한글 개인명(성+이름 1~2음절)인데 출생통계상 plausible하지 않음 → 필명·비정상 의심.
    1글자 이름은 출생 건수 합이 MIN_SINGLE_CHAR_GIVEN_WEIGHT 미만이면 False.
    이름 DB가 없으면 검사 생략(False).
    """
    if not korean_given_names():
        return False
    if _name_uses_roman_or_hanja(name):
        return False
    if not is_korean_person_by_surname(name):
        return False
    if _is_translation_foreign_original_author(name, translation_book, author_info or []):
        return False
    given = _extract_korean_given_part(name)
    if not given or len(given) > 2:
        return False
    return not korean_given_name_is_plausible(given)


def _korean_hangul_short_given_exception(name: str) -> bool:
    """
    한국인 한글명, 성 제외 이름 1~2음절인데 structured만 실패(예: 김강, 조은).
    성 제외 3음절 이상(조은생각, 김아름다운)은 False → 700 0_.
    """
    if not is_korean_person_by_surname(name) or _name_uses_roman_or_hanja(name):
        return False
    if korean_personal_name_is_structured(name):
        return False
    given = _extract_korean_given_part(name)
    if not given or len(given) > 2:
        return False
    if korean_given_names() and not korean_given_name_is_plausible(given):
        return False
    return True


def _is_translation_foreign_original_author(
    name: str,
    translation_book: bool,
    author_info: list,
) -> bool:
    """번역서의 원저(외국인) — 한국인 외자 700 규칙에서 제외."""
    if not translation_book or not author_info:
        return False
    if is_korean_person_by_surname(name):
        return False
    key = _norm_author_lookup_key(name)
    if not key:
        return False
    return any(
        key == _norm_author_lookup_key((ai.get("name") or "").strip())
        for ai in author_info
    )


def marc700_second_indicator(
    name: str,
    role: str,
    *,
    translation_book: bool = False,
    author_info: list | None = None,
) -> str:
    """
    700 두 번째 지시자: 성·이름 구분 가능 → 1, 닉네임·필명·비정상 구조 → 0.
    한국인 한글명: 성 제외 3음절 이상 → 0. 미등록·희귀 1글자 이름 → 0.
    1~2음절 structured 예외(김강 등, 출생통계 plausible) → 1.
    """
    n = (name or "").strip()
    if not n:
        return "1"
    ai = author_info or []
    if (
        _korean_hangul_short_given_exception(n)
        and not _is_translation_foreign_original_author(n, translation_book, ai)
    ):
        return "1"
    if re.search(r"[A-Za-z]", n) and not re.search(r"[\uac00-\ud7a3]", n):
        return "1"
    if re.search(r"[\u3040-\u30ff]", n):
        return "1"
    if re.search(r"[가-힣]", n):
        if _korean_given_not_in_birth_registry(
            n, translation_book=translation_book, author_info=ai
        ):
            return "0"
        return "1" if korean_personal_name_is_structured(n) else "0"
    return "1"


def build_710(author: dict) -> str:
    return f"$a {author['name'].strip()}"


def _author_info_has_japanese_script(author_info: list[dict]) -> bool:
    """원저 표기(500 후보)에 한자·가나가 하나라도 있으면 일본 등 동아시아 원저로 본다."""
    for ai in author_info:
        kn = (ai.get("kanji_name") or "").strip()
        if kn and re.search(r"[\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]", kn):
            return True
    return False


def build_900(
    orig_author_ko: str | None,
    kanji_name: str | None = None,
    orig_author_en: str | None = None,
    translation_book: bool = False,
    japanese_family_book: bool = False,
) -> str | None:
    """
    - 서양 번역: translation_book 이고 japanese_family_book 이 False → 영문 원저 힌트가 있으면 한글 성·이름 도치.
    - 일본 등: japanese_family_book True → 한자·가나 원표기·일본어 음 규칙.
    - 비번역·서양: 영문 원저자명이 있으면 한글 성·이름 도치, 없으면 한글 표기.
    """
    if not orig_author_ko:
        return None
    ko = orig_author_ko.strip()

    if translation_book and not japanese_family_book:
        if orig_author_en:
            rom = orig_author_en.strip()
            latin = bool(re.search(r"[A-Za-z]", rom)) and not re.search(
                r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]",
                rom,
            )
            if latin:
                reversed_name = korean_name_reverse(ko)
                if reversed_name:
                    return f"900 10 $a {reversed_name}"
        return f"900 10 $a {ko}"

    if japanese_family_book:
        kn = (kanji_name or "").strip()
        if kn:
            gloss = _jp_name_reading_hangul(kn)
            if gloss:
                g0 = re.sub(r"\s+", "", gloss)
                k0 = re.sub(r"\s+", "", ko)
                if g0 != k0:
                    return f"900 10 $a {gloss}"
        if kn and re.search(r"[\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]", kn):
            return f"900 10 $a {ko}"
        if orig_author_en:
            rom = orig_author_en.strip()
            latin = bool(re.search(r"[A-Za-z]", rom)) and not re.search(
                r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]",
                rom,
            )
            if latin:
                reversed_name = korean_name_reverse(ko)
                if reversed_name:
                    return f"900 10 $a {reversed_name}"
        return f"900 10 $a {ko}"

    if kanji_name:
        ktype = kanji_type(kanji_name)
        if ktype == "kanji_kana":
            return None
        if ktype == "kanji_only":
            reading = kanji_to_korean_reading(kanji_name)
            if reading:
                return f"900 10 $a {reading}"
            return None

    if orig_author_en:
        reversed_name = korean_name_reverse(ko)
        if reversed_name:
            return f"900 10 $a {reversed_name}"
        return f"900 10 $a {ko}"

    reversed_name = korean_name_reverse(ko)
    if reversed_name:
        return f"900 10 $a {reversed_name}"
    return None


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
    intro_persons:  list[dict] = []
    intro_author_pairs: list[dict] = []

    if has_translator:
        orig_info = collect_orig_info(item, item_id, isbn13, title, authors)
        orig_title = orig_info["orig_title"]
        orig_author_en = orig_info["orig_author_en"]
        author_info    = orig_info["author_info"]
        intro_persons  = orig_info.get("intro_persons") or []
        intro_author_pairs = orig_info.get("intro_author_pairs") or []
        if not orig_author_en:
            blob = aladin_item_description_blob(item)
            orig_author_en = extract_english_author_after_korean_paren(blob)
        if author_info:
            blob2 = aladin_item_description_blob(item)
            n_auth = len(author_info)
            n_comm = (orig_author_en or "").count(",")
            thin = (
                not orig_author_en
                or len(orig_author_en) < 100
                or (n_comm + 1 < min(n_auth, 14))
            )
            if thin:
                names = _extract_paren_english_names_from_blob(blob2)
                if names and len(names) >= max(2, n_comm + 2):
                    orig_author_en = ", ".join(names[: min(len(names), n_auth + 4)])

    if subtitle and clean_subtitle(subtitle) == "":
        subtitle = ""
    title = strip_award_suffix_from_title(title)

    # MARC 필드 생성
    field_245 = build_245(title, subtitle, authors)
    field_246 = build_246(orig_title)
    translation_book = has_translator
    orig_title_has_cjk_or_kana = bool(
        (orig_title or "").strip()
        and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", orig_title)
    )

    # 500: 원저자명 — 한자(또는 가나·한자 혼합)·로마자만. 한글 음역(타나카 타츠야 등)은 500에 넣지 않음.
    # 원제(246)에 한자·가나가 있거나, 저자 원표기에 한자·가나가 있을 때만 '동아시아 원저'로 본다.
    # is_east_asian(페이지 키워드)은 합집·헌정집 소개에 '도쿄' 등이 나오면 오탐해 서양 500·영문 700 경로를 막으므로 쓰지 않음.
    jp_ctx_for_500 = orig_title_has_cjk_or_kana or _author_info_has_japanese_script(author_info)
    n_missing_kanji = sum(1 for x in author_info if not x.get("kanji_name"))
    allow_roman_500 = bool(orig_author_en) and n_missing_kanji == 1
    roman_once = False
    for ai in author_info:
        ai.pop("_500_script", None)
        ai.pop("_roman_for_900", None)

    orig_names_500_from_intro = try_orig_names_500_from_intro_english_pairs(
        translation_book, jp_ctx_for_500, author_info, intro_author_pairs
    )
    orig_names_500_from_profiles = try_orig_names_500_from_profile_english(
        translation_book, jp_ctx_for_500, author_info
    )
    if orig_names_500_from_intro is not None:
        orig_names_500 = orig_names_500_from_intro
        for ai, en in zip(author_info, orig_names_500_from_intro):
            ai["_500_script"] = "roman"
            ai["_roman_for_900"] = en
    elif orig_names_500_from_profiles is not None:
        # 프로필에 영문이 없는 저자 → 500·900 생략, 700(한글 부출)만.
        orig_names_500 = [
            t for t in orig_names_500_from_profiles if _looks_like_roman_orig_display(t)
        ]
        for ai, tok in zip(author_info, orig_names_500_from_profiles):
            if _looks_like_roman_orig_display(tok):
                ai["_500_script"] = "roman"
                ai["_roman_for_900"] = tok
            else:
                ai["_500_script"] = "orig_korean_700_only"
    else:
        orig_names_500 = []
        for ai in author_info:
            kanji = ai.get("kanji_name")
            if kanji:
                orig_names_500.append(kanji)
                ai["_500_script"] = "cjk"
            elif translation_book and jp_ctx_for_500 and ai.get("name"):
                # 한자·영문 원표기 없음 → 700만(500·900 생략)
                ai["_500_script"] = "orig_korean_700_only"
            elif allow_roman_500 and orig_author_en and not roman_once:
                orig_names_500.append(orig_author_en.strip())
                ai["_500_script"] = "roman"
                roman_once = True
        if not orig_names_500 and orig_author_en:
            blob = orig_author_en.strip()
            chunks = [x.strip() for x in re.split(r",\s*", blob) if x.strip()]
            if len(chunks) >= 2 and author_info:
                for idx, ai in enumerate(author_info):
                    if idx < len(chunks):
                        orig_names_500.append(chunks[idx])
                        ai["_500_script"] = "roman"
                        ai["_roman_for_900"] = chunks[idx]
                    elif ai.get("name"):
                        if translation_book and not jp_ctx_for_500:
                            ai["_500_script"] = "orig_korean_700_only"
                        else:
                            orig_names_500.append(ai["name"].strip())
                            ai["_500_script"] = "hangul_western"
            else:
                if (
                    len(chunks) == 1
                    and len(author_info) >= 3
                    and translation_book
                    and not jp_ctx_for_500
                    and len(author_info) > len(chunks)
                ):
                    orig_names_500.clear()
                    for ai in author_info:
                        if ai.get("name"):
                            if translation_book and not jp_ctx_for_500:
                                ai["_500_script"] = "orig_korean_700_only"
                            else:
                                orig_names_500.append(ai["name"].strip())
                                ai["_500_script"] = "hangul_western"
                else:
                    orig_names_500.append(blob)
                    if author_info:
                        if len(chunks) == 1 and len(author_info) >= 2 and translation_book and not jp_ctx_for_500:
                            for ai in author_info:
                                ai["_500_script"] = "orig_korean_700_only"
                        elif len(author_info) == 1:
                            author_info[0]["_500_script"] = "roman"
                            author_info[0]["_roman_for_900"] = blob
    if translation_book and not jp_ctx_for_500 and orig_names_500:
        orig_names_500 = [t for t in orig_names_500 if not _orig_500_token_has_hangul(t)]
    if jp_ctx_for_500 and orig_names_500:
        orig_names_500 = [
            t for t in orig_names_500 if not _is_hangul_only_orig_name_for_500(t)
        ]
    field_500_orig = f"500 __ $a 원저자명: {', '.join(orig_names_500)}" if orig_names_500 else None
    fields_500 = []
    if field_500_orig:
        fields_500.append(field_500_orig)
    fields_500.extend(build_500_role_notes(authors))

    # 700 — 246 있으면 원저 영문 부출은 성·이름 도치(한자 원저라도 영문 있으면 부출).
    persons    = [a for a in authors if not a["is_org"]]
    intro_non_org = [p for p in intro_persons if not p.get("is_org")]
    # API author 필드가 짧을 때(합저 등) 「저자 및 역자소개」에서 파싱한 명단으로 700을 채움.
    if has_translator and len(intro_non_org) >= max(2, len(persons)):
        persons_for_700 = intro_non_org
    else:
        persons_for_700 = persons
    fields_700 = []
    first_ai   = author_info[0] if author_info else None
    skip_first_person_700 = False
    n_prof_en = sum(1 for ai in author_info if (ai.get("english_name") or "").strip())
    western_700_english = (
        translation_book
        and not jp_ctx_for_500
        and bool(author_info)
        and n_prof_en >= len(author_info) - 1
    )
    if western_700_english:
        ko_to_en = {
            _norm_author_lookup_key((ai.get("name") or "").strip()): (ai.get("english_name") or "").strip()
            for ai in author_info
        }
        for a in persons_for_700:
            ind = marc700_second_indicator(
                a["name"],
                a.get("role", ""),
                translation_book=translation_book,
                author_info=author_info,
            )
            if a.get("role") in TRANS_ROLES:
                fields_700.append(f"700 {ind}_ {build_700(a)}")
            else:
                en = ko_to_en.get(_norm_author_lookup_key((a.get("name") or "").strip()))
                if en:
                    fields_700.append(f"700 {ind}_ $a {english_name_reverse(en)}")
                else:
                    fields_700.append(f"700 {ind}_ {build_700(a)}")
    else:
        if first_ai and orig_author_en:
            rom = orig_author_en.strip()
            # 한글·한자·가나가 섞인 문자열은 원제 등 오탐(예: 容疑者Xの献身 — 글자 X 때문에)이므로
            # '로마자 원저자명'으로 보지 않음.
            latin = bool(re.search(r"[A-Za-z]", rom)) and not re.search(
                r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf]",
                rom,
            )
            sc = rom.replace(";", ",")
            n_comma = sc.count(",")
            looks_anthology_rom = n_comma >= 2 or (n_comma >= 1 and len(rom) > 140)
            looks_truncated_rom = len(rom) <= 10 and rom.count(".") >= 1 and n_comma == 0 and len(rom.split()) <= 2
            if latin and (not first_ai.get("kanji_name") or translation_book):
                fn = (first_ai.get("name") or "").strip()
                mixed_lat_ko = bool(
                    fn and re.search(r"[A-Za-z]", fn) and re.search(r"[\uac00-\ud7a3]", fn)
                )
                if looks_anthology_rom or looks_truncated_rom or mixed_lat_ko:
                    pass
                elif first_ai.get("is_east_asian") or orig_title_has_cjk_or_kana:
                    fields_700.append(f"700 1_ $a {rom}")
                    skip_first_person_700 = True
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
        for a in persons_for_700:
            if skip_first_person_700 and a["name"].strip() == first_ko_name:
                skip_first_person_700 = False
                continue
            ind = marc700_second_indicator(
                a["name"],
                a.get("role", ""),
                translation_book=translation_book,
                author_info=author_info,
            )
            fields_700.append(f"700 {ind}_ {build_700(a)}")

    # 710
    orgs       = [a for a in authors if a["is_org"]]
    fields_710 = [f"710 0_ {build_710(a)}" for a in orgs]

    # 900 — 500이 비어도 로마자 원저(_500_script roman)만 있으면 900은 출력.
    # orig_korean_700_only: 알라딘에 영문 원저가 없는 한글 표기만 있는 저자 → 700에만 부출, 500·900 생략.
    fields_900 = []
    jp_book_for_roman = orig_title_has_cjk_or_kana or (
        bool(author_info) and any(ai.get("is_east_asian") for ai in author_info)
    )
    jp_family_book = (
        jp_book_for_roman
        or _author_info_has_japanese_script(author_info)
        or orig_title_has_cjk_or_kana
    )
    emit_900 = bool(author_info) and (
        field_500_orig
        or any(
            (ai.get("_500_script") or "") in ("roman", "cjk", "hangul", "hangul_western")
            for ai in author_info
        )
    )
    # 일본·동아시아: 500에 실릴 한자 원저만 900(한자음). 한글 음역만 있는 인물은 제외.
    if emit_900 and jp_ctx_for_500:
        emit_900 = field_500_orig is not None or any(
            ai.get("_500_script") == "cjk" for ai in author_info
        )
    if emit_900:
        for idx, ai in enumerate(author_info):
            if ai.get("_500_script") == "cjk":
                kn = (ai.get("kanji_name") or "").strip()
                kn_compact = re.sub(r"\s+", "", kn)
                if not kn_compact or kanji_type(kn_compact) != "kanji_only":
                    continue
                reading = kanji_to_korean_reading(kn_compact)
                if reading:
                    fields_900.append(f"900 10 $a {reading}")
                continue
            if ai.get("_500_script") == "hangul":
                if jp_ctx_for_500:
                    continue
                f900 = build_900(
                    ai["name"],
                    ai.get("kanji_name"),
                    orig_author_en=None,
                    translation_book=translation_book,
                    japanese_family_book=False,
                )
                if f900:
                    fields_900.append(f900)
                continue
            if ai.get("_500_script") == "orig_korean_700_only":
                continue
            if ai.get("_500_script") == "hangul_western":
                ko = (ai.get("name") or "").strip()
                rev = korean_name_reverse(ko)
                if rev:
                    fields_900.append(f"900 10 $a {rev}")
                elif ko:
                    fields_900.append(f"900 10 $a {ko}")
                continue
            if ai.get("_500_script") != "roman":
                continue
            en_for_900 = ai.get("_roman_for_900")
            if (
                not en_for_900
                and orig_author_en
                and not ai.get("kanji_name")
                and not jp_book_for_roman
                and (len(author_info) <= 1 or idx == 0)
            ):
                en_for_900 = orig_author_en
            f900 = build_900(
                ai["name"],
                ai["kanji_name"],
                orig_author_en=en_for_900,
                translation_book=translation_book,
                japanese_family_book=jp_family_book,
            )
            if f900:
                fields_900.append(f900)
        for ai in author_info:
            ai.pop("_500_script", None)
            ai.pop("_roman_for_900", None)

    marc: dict[str, object] = {
        "f245": f"245 00 {field_245}",
        "f246": field_246,
        "f700": fields_700,
        "f710": fields_710,
    }
    if fields_500:
        marc["f500"] = fields_500 if len(fields_500) > 1 else fields_500[0]
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
