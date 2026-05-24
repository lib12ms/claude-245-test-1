"""
출생신고 통계 기반 한국인 **이름(성 제외)** 목록.

data/korean_given_names.txt — scripts/build_korean_given_names.py 로 갱신.
출처: randkid/name (대법원 전자가족관계등록, 2008~2019)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data" / "korean_given_names.txt"


@lru_cache(maxsize=1)
def korean_given_names() -> frozenset[str]:
    if not _DATA.is_file():
        return frozenset()
    names = {
        line.strip()
        for line in _DATA.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    return frozenset(names)


def is_registered_korean_given_name(given: str) -> bool:
    """성 제외 이름 부분이 출생신고 통계에 등장하는지."""
    g = (given or "").strip()
    if not g:
        return False
    return g in korean_given_names()
