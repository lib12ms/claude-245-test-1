# -*- coding: utf-8 -*-
"""
041 $h(원어 코드) 기반 700/900 이름 도치 결정 로직.

배경: 일본 저자인데 한자(kanji_name)도, 원제 한자도, 저자 bio 키워드("일본인" 등)도
없는 책(예: ISBN 9791124070987 "장사의 神 실전편" - 우노 다카시)은 지금까지
JAPANESE_SURNAMES 성씨 사전(app.py의 is_japanese_person_by_surname)으로만 잡고 있었음.

041 $h가 준비되면 그 코드 하나로 East Asian/Western을 바로 가르기 때문에 훨씬 단순해짐.
$h가 없는데 옮긴이가 있는 케이스(번역서인데 $h만 못 잡힌 경우)는 실제 데이터에서 매우
드물어서, 성씨 사전으로 따로 처리하지 않고 항상 LLM 폴백으로 넘긴다(2026-06-19 결정).

의존성 없음 — Flask·OpenAI 등 불필요.
"""

import re

# ISDS 3자리 언어코드 → 한국어 언어명 (lang_field.py 팀원 파일과 통일, 2026-07-07)
# 주의: 팔리어는 MARC 표준 코드 'pli' 사용 (팀원 파일의 'pal'은 비표준)
ISDS_LANGUAGE_CODES: dict[str, str] = {
    'kor': '한국어', 'eng': '영어',  'jpn': '일본어',   'chi': '중국어',
    'zho': '중국어', 'rus': '러시아어', 'ara': '아랍어', 'fre': '프랑스어', 'ger': '독일어',
    'ita': '이탈리아어', 'spa': '스페인어', 'por': '포르투갈어', 'tur': '터키어',
    'dut': '네덜란드어', 'nld': '네덜란드어', 'gre': '그리스어', 'grc': '고대 그리스어',
    'lat': '라틴어',  'swe': '스웨덴어', 'nor': '노르웨이어', 'dan': '덴마크어',
    'fin': '핀란드어', 'hun': '헝가리어', 'cze': '체코어',
    'pol': '폴란드어', 'heb': '히브리어', 'per': '페르시아어', 'rum': '루마니아어',
    'san': '산스크리트어', 'pli': '팔리어', 'hin': '힌디어',
    'vie': '베트남어',
    'und': '미정의',
}

ALLOWED_CODES: frozenset[str] = frozenset(ISDS_LANGUAGE_CODES.keys()) - {"und"}

# 동아시아=비도치, 서양=도치 (2026-06-25 번역서 상위 15개 언어 기준으로 확장,
# 기존 1215_main.py 분류보다 swe/pol/heb 추가)
EAST_ASIAN_LANG_CODES = {"jpn", "chi", "zho"}

# 베트남어는 성이 이미 맨 앞(가족성+중간이름+이름)이라 비도치 — 서양식 도치 대상 아님.
# 동아시아와 어족이 다르므로 별도 집합으로 추적(사유 로그 구분용).
VIETNAMESE_LANG_CODES = {"vie"}

# pli(팔리어)·san(산스크리트어): 성씨 없는 단일 이름 구조 — 비도치, 700 지시기호 0_.
# ara(아랍어): 고전·현대 명명 방식 혼재(0.6% 비중), 비도치로 통일(2026-06-25, 오탐 시 재검토).
OTHER_NO_INVERSION_LANG_CODES = {"pli", "san", "ara"}

# pli·san은 도치뿐 아니라 "성씨 자체가 없는 단일 이름" 구조라, MARC 700 첫 번째
# 지시기호도 "1"(성·이름 구조)이 아니라 "0"(단일/비구조 이름)이어야 함. 사용자가
# 국립중앙도서관 실제 레코드에서 이 저자들이 700 0_ 으로 카탈로깅된 걸 직접 확인하고
# 확정함(2026-06-25). ara는 고전/현대 혼재로 성씨 유무 자체가 불확실해 여기엔 포함하지
# 않음(도치 여부만 위 OTHER_NO_INVERSION_LANG_CODES에서 비도치로 통일, 지시기호는 보류).
NO_SURNAME_LANG_CODES = {"pli", "san"}


def decide_700_indicator_by_lang(lang_h: str | None) -> str | None:
    """
    041 $h 코드만으로 700 첫 번째 지시기호(0=단일/비구조 이름, 1=성·이름 구조) 결정.
    pli/san → "0" (성씨 없음 확정). 그 외 코드 또는 None → None(미결정, 호출 쪽의
    기존 로직(app.py의 marc700_second_indicator 등)으로 폴백).

    주의: 동아시아(jpn/chi/zho)·베트남(vie)은 도치는 안 하지만 성씨 자체는 있으므로
    여기서 "0"을 반환하지 않음 — 비도치(reorder_name_by_lang_code)와 무성씨(이 함수)는
    서로 다른 축이라 혼동하지 말 것.
    """
    code = (lang_h or "").strip().lower()
    if code in NO_SURNAME_LANG_CODES:
        return "0"
    return None


WESTERN_LANG_CODES = {
    "eng", "fre", "ger", "spa", "ita", "rus", "por", "dut", "nld", "tur",
    "swe", "pol", "heb",
    # 2026-06-25 NLK 1000건 집계에서 추가 확인된 서양 어순(이름+성) 언어.
    # kor(원어=한국어, 0.8%)는 의미가 불분명해서(원저작 재수록 등 추정) 제외 — 사용자 요청.
    "grc", "lat", "gre",
    # 덴마크어·체코어·루마니아어도 영어·프랑스어와 동일하게 "이름+성" 어순 → 도치 필요
    "dan", "cze", "rum",
    # 2026-07-07 팀원 파일 통합 시 추가:
    # nor(노르웨이어)·fin(핀란드어): 서양 "이름+성" 어순 → 도치.
    # hun(헝가리어): 헝가리 자국 어순은 성+이름이지만 국제 표기(API/GPT 반환값)는
    # 이름+성 순서로 들어오므로 도치 처리.
    # per(페르시아어): 이란식 이름 순서가 이름+성(서양과 동일)이므로 도치(2026-07-07).
    # hin(힌디어): 국중 실데이터 확인 — 도치 적용(2026-07-07).
    "nor", "fin", "hun", "per", "hin",
}


def extract_lang_h(tag_041_text: str | None) -> str | None:
    """"041 $akor $heng" 같은 완성된 041 문자열에서 $h 코드만 뽑아냄."""
    if not tag_041_text:
        return None
    m = re.search(r"\$h([a-z]{3})", tag_041_text, re.IGNORECASE)
    return m.group(1).lower() if m else None


def _invert_western(name: str) -> str:
    """공백 기준 마지막 토큰을 성으로 보고 '성, 이름' 형태로 도치."""
    parts = name.split()
    if len(parts) == 2:
        return f"{parts[1]}, {parts[0]}"
    if len(parts) >= 3:
        family = parts[-1]
        given = " ".join(parts[:-1])
        return f"{family}, {given}"
    return name


def reorder_name_by_lang_code(name: str, lang_h: str | None) -> str | None:
    """
    041 $h 코드만으로 비도치/도치 결정.
    동아시아 코드 → 비도치(그대로), 서양 코드 → 도치.
    코드가 없거나(None) 미분류 코드(kor/und 등)면 None(미결정) 반환 → 호출 쪽에서 폴백 필요.
    """
    s = (name or "").strip()
    if not s or not lang_h:
        return None
    code = lang_h.strip().lower()
    if (
        code in EAST_ASIAN_LANG_CODES
        or code in VIETNAMESE_LANG_CODES
        or code in OTHER_NO_INVERSION_LANG_CODES
    ):
        return s
    if code in WESTERN_LANG_CODES:
        return _invert_western(s)
    return None


def decide_700_name_order(
    name: str,
    *,
    lang_h: str | None = None,
    kanji_name: str | None = None,
    has_translator: bool = False,
) -> tuple[str | None, str]:
    """
    700 $a에 쓸 표기와 판단 근거를 함께 반환.
    formatted_name이 None이면 이 함수만으론 결정 불가 → 호출 쪽에서 LLM 등 추가 폴백 필요.

    has_translator: 245 필드에 옮긴이(역자)가 있는지 여부 — app.py가 이미 갖고 있는
      `has_translator`/`translation_book` 신호를 그대로 넘기면 됨. 콜리그의 041 로직이
      "한국 저자 원저작엔 041 태그 자체를 생략"하는지 "041은 항상 찍고 $h만 비우는지"는
      알 필요 없음 — 우리가 직접 아는 신호(옮긴이 존재 여부)로 가르기 때문.
      (2026-06-19 팀 결정: 041 $h 코드가 합류되면 이 기준으로 사용하기로 확정)

    우선순위:
      1) kanji_name 있음 → 이미 동아시아 확정 → 비도치
      2) 041 $h 있음 → 코드 기반 결정 (041 준비되면 가장 먼저 타는 경로, LLM 불필요)
      3) $h 없고 옮긴이도 없음(원저작, 번역서 아님) → 원어=한국어로 간주, 한국 저자·비도치
      4) $h 없는데 옮긴이는 있음(번역서인데 $h만 못 잡음) → None → LLM 폴백
         (2026-06-19 결정: 이 케이스 자체가 실제 데이터에서 매우 드물어서, 성씨 사전으로
         공짜 처리할 가치가 적다고 판단 — 차라리 항상 LLM에 맡기기로 함. 성씨 사전 단계 제거)

    주의: 이 함수는 700 $a 표기(도치 여부)만 결정함. MARC 700 첫 번째 지시기호(0/1)는
    별도 축이라 여기 포함 안 됨 — `decide_700_indicator_by_lang(lang_h)`를 같이 호출해서
    pli/san처럼 성씨 자체가 없는 경우 "0"으로 덮어쓸지 판단할 것 (2026-06-25 추가).
    """
    s = (name or "").strip()
    if not s:
        return None, "empty_name"

    if kanji_name:
        return s, "kanji_present_east_asian"

    by_lang = reorder_name_by_lang_code(s, lang_h)
    if by_lang is not None:
        return by_lang, f"lang_h={lang_h}"

    if not lang_h and not has_translator:
        return s, "no_h_no_translator_assumed_korean"

    return None, "no_h_has_translator_rare_case_llm_fallback"


if __name__ == "__main__":
    # 041이 이미 있는 경우 (서양 번역서 예시)
    tag_041 = "041 $akor $heng"
    lang_h = extract_lang_h(tag_041)
    print(decide_700_name_order("Andy Weir", lang_h=lang_h))
    # ('Weir, Andy', 'lang_h=eng')

    # 041 $h=jpn이면 LLM 없이 바로 결정됨
    print(decide_700_name_order("우노 다카시", lang_h="jpn"))
    # ('우노 다카시', 'lang_h=jpn')

    # 한국인 저자 원저작 — 041 $h 없음 + 245에 옮긴이도 없음(번역서 아님) → 한국 저자로 간주
    print(decide_700_name_order("김민지", lang_h=None, has_translator=False))
    # ('김민지', 'no_h_no_translator_assumed_korean')

    # 번역서인데 $h를 못 잡은 경우 — 실제 데이터에서 매우 드문 케이스라 성씨 사전 없이
    # 항상 LLM 폴백으로 넘김 (2026-06-19 결정)
    print(decide_700_name_order("우노 다카시", lang_h=None, has_translator=True))
    # (None, 'no_h_has_translator_rare_case_llm_fallback')

    # 2026-06-25 추가: swe/pol/heb 도치, vie는 비도치
    print(decide_700_name_order("Fredrik Backman", lang_h="swe"))
    # ('Backman, Fredrik', 'lang_h=swe')

    print(decide_700_name_order("Olga Tokarczuk", lang_h="pol"))
    # ('Tokarczuk, Olga', 'lang_h=pol')

    print(decide_700_name_order("Amos Oz", lang_h="heb"))
    # ('Oz, Amos', 'lang_h=heb')

    print(decide_700_name_order("응우옌 후이 티엡", lang_h="vie"))
    # ('응우옌 후이 티엡', 'lang_h=vie')

    # 2026-06-25 추가: pli/san은 $a 표기는 비도치($a 그대로)지만, 지시기호는 성씨가
    # 없으므로 "0"으로 덮어써야 함 — decide_700_name_order와 decide_700_indicator_by_lang을
    # 같이 호출해서 합친다.
    print(decide_700_name_order("나가르주나", lang_h="san"))
    # ('나가르주나', 'lang_h=san')
    print(decide_700_indicator_by_lang("san"))
    # '0'
    print(decide_700_indicator_by_lang("jpn"))
    # None (동아시아는 성씨가 있으므로 기존 로직(보통 "1")을 그대로 씀)
