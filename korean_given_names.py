"""
출생신고 통계 기반 한국인 **이름(성 제외)** 목록·빈도.

data/korean_given_name_weights.tsv — scripts/build_korean_given_names.py 로 갱신.
출처: randkid/name (대법원 전자가족관계등록, 2008~2019)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DATA_TSV = Path(__file__).resolve().parent / "data" / "korean_given_name_weights.tsv"
_DATA_TXT = Path(__file__).resolve().parent / "data" / "korean_given_names.txt"

# 1글자 이름: 출생 건수 합(2008~2019) 미만이면 필명·비정상 의심 → 700 0_
# 치=2, 강=1300, 은=1289 (randkid/name 기준)
MIN_SINGLE_CHAR_GIVEN_WEIGHT = 50

# 2008년 이전 전통 한국 이름(한자음)에 자주 쓰이나 2008~2019 통계에 없는 음절.
# TSV 폴백으로 사용: 두 음절이 모두 이 집합 또는 TSV 음절에 속하면 실제 이름으로 간주.
# "그파"처럼 한국 이름에 거의 쓰이지 않는 음절은 포함하지 않음.
_TRADITIONAL_NAME_SYLLABLES: frozenset[str] = frozenset({
    # ㅂ
    "병", "봉", "복", "범", "빈", "배",
    # ㅈ
    "재", "종", "준", "진", "장", "정", "주", "절", "겸",
    # ㅎ
    "호", "환", "화", "훈", "학", "혁", "형", "흥", "희",
    # ㅇ
    "영", "용", "원", "운", "완", "의", "열",
    # ㄱ
    "구", "근", "국", "길", "건",
    # ㄷ
    "덕", "달", "동",
    # ㅅ
    "순", "석", "선", "성", "상", "승", "세", "섭",
    # ㄴ
    "남",
    # ㄹ
    "연", "림",
    # ㅁ
    "만", "무",
    # ㅊ
    "철", "춘", "창", "천",
    # ㅌ
    "태",
    # ㅍ (한자음으로 이름에 쓰이는 경우: 표·풍 등 — 보수적으로 포함)
    # ㅋ — 한국 이름에 거의 없음, 제외
    # ㅈ 추가
    "전",
    # ㅇ 추가
    "옥",
    # 기타 자주 쓰이는 전통 이름 음절
    "두", "후", "항", "달", "겸", "인", "경", "식",
})


@lru_cache(maxsize=1)
def korean_given_name_weights() -> dict[str, int]:
    if _DATA_TSV.is_file():
        weights: dict[str, int] = {}
        for line in _DATA_TSV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("name"):
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            name, w = parts[0].strip(), parts[1].strip()
            if name and w.isdigit():
                weights[name] = int(w)
        return weights

    # 구버전 txt fallback (빈도 없음 → 1글자·2글자 모두 등록만 확인)
    if _DATA_TXT.is_file():
        return {
            line.strip(): 1
            for line in _DATA_TXT.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    return {}


@lru_cache(maxsize=1)
def korean_given_names() -> frozenset[str]:
    return frozenset(korean_given_name_weights().keys())


@lru_cache(maxsize=1)
def korean_given_name_syllables() -> frozenset[str]:
    """등록된 이름에 쓰인 모든 음절 집합. 2음절 이름 폴백 판별용."""
    syllables: set[str] = set()
    for name in korean_given_names():
        for ch in name:
            syllables.add(ch)
    return frozenset(syllables)


def korean_given_name_weight(given: str) -> int | None:
    g = (given or "").strip()
    if not g:
        return None
    w = korean_given_name_weights().get(g)
    return w if w is not None else None


def is_registered_korean_given_name(given: str) -> bool:
    """성 제외 이름 부분이 출생신고 통계에 한 번이라도 등장하는지."""
    return korean_given_name_weight(given) is not None


def korean_given_name_is_plausible(
    given: str,
    *,
    min_single_char_weight: int = MIN_SINGLE_CHAR_GIVEN_WEIGHT,
) -> bool:
    """
    700 1_ 에 쓸 만한 등록 이름인지.
    - 1글자: 통계 등장 + 출생 건수 합이 min_single_char_weight 이상
    - 2글자: 통계에 등장하면 True
      통계에 없어도 두 음절이 모두 TSV 음절 또는 전통 이름 음절(_TRADITIONAL_NAME_SYLLABLES)에
      속하면 True (병기·재호·철수 등 구세대 이름 오탈락 방지)
    """
    g = (given or "").strip()
    if not g:
        return False
    w = korean_given_name_weight(g)
    if w is not None:
        if len(g) == 1:
            return w >= min_single_char_weight
        return True
    # 2음절 이름이 통계 목록에 없을 때: 두 음절 모두 알려진 이름 음절이면 실제 이름으로 간주
    if len(g) == 2:
        syllables = korean_given_name_syllables() | _TRADITIONAL_NAME_SYLLABLES
        return all(ch in syllables for ch in g)
    return False
