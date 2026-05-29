"""
baby-name.kr 에서 2010~2026년 출생신고 이름 통계를 수집해
data/korean_given_name_weights.tsv 를 생성합니다.

실행: python scripts/build_given_names.py
"""

import time
import re
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "korean_given_name_weights.tsv"
BASE_URL = "https://baby-name.kr/annalRanking/{year}/"
YEARS = range(2010, 2027)
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_year(year: int) -> dict[str, int]:
    url = BASE_URL.format(year=year)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [{year}] 요청 실패: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    counts: dict[str, int] = {}

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            # 이름: 3번째 셀 (순위, 변동, 이름, 건수)
            name_cell = cells[2] if len(cells) >= 4 else cells[1]
            count_cell = cells[-1]

            name = name_cell.get_text(strip=True)
            count_text = re.sub(r"[^\d]", "", count_cell.get_text(strip=True))

            if not name or not count_text:
                continue
            if not re.match(r"^[가-힣]{1,3}$", name):
                continue
            try:
                counts[name] = counts.get(name, 0) + int(count_text)
            except ValueError:
                continue

    print(f"  [{year}] {len(counts)}개 이름 수집")
    return counts


def main():
    total: dict[str, int] = defaultdict(int)

    for year in YEARS:
        print(f"{year}년 수집 중...")
        year_data = fetch_year(year)
        for name, count in year_data.items():
            total[name] += count
        time.sleep(0.5)

    # 이름 길이 1~2음절만 저장 (3음절 이상은 structured 체크에서 이미 처리)
    filtered = {k: v for k, v in total.items() if 1 <= len(k) <= 2}
    sorted_data = sorted(filtered.items(), key=lambda x: (-x[1], x[0]))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("name\tweight\n")
        for name, weight in sorted_data:
            f.write(f"{name}\t{weight}\n")

    print(f"\n완료: {len(sorted_data)}개 이름 → {OUTPUT}")
    print(f"1음절: {sum(1 for k in filtered if len(k)==1)}개")
    print(f"2음절: {sum(1 for k in filtered if len(k)==2)}개")


if __name__ == "__main__":
    main()
