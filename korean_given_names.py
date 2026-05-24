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
    - 2글자: 통계에 등장하면 True
    - 1글자: 등장 + 출생 건수 합이 min_single_char_weight 이상
    """
    g = (given or "").strip()
    if not g:
        return False
    w = korean_given_name_weight(g)
    if w is None:
        return False
    if len(g) == 1:
        return w >= min_single_char_weight
    return True
