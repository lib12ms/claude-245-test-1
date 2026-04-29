import streamlit as st
import requests

API_BASE = st.secrets.get("API_BASE", "http://localhost:5000")

st.set_page_config(
    page_title="KORMARC 자동 생성기",
    page_icon="📚",
    layout="centered",
)

st.title("📚 KORMARC 자동 생성기")
st.caption("알라딘 API 연동 · 245 / 700 / 710 필드 자동 생성")
st.divider()

col_input, col_btn = st.columns([4, 1])
with col_input:
    isbn_input = st.text_input(
        "ISBN",
        placeholder="ISBN-13 또는 ISBN-10 입력  예) 9788934972464",
        label_visibility="collapsed",
        max_chars=17,
    )
with col_btn:
    search = st.button("조회하기", use_container_width=True, type="primary")

if search and isbn_input:
    isbn_clean = isbn_input.replace("-", "").replace(" ", "")
   with st.spinner("알라딘 API에서 도서 정보를 가져오는 중..."):
        try:
            resp = requests.get(f"{API_BASE}/api/isbn", params={"isbn": isbn_clean}, timeout=15)
            st.write(f"HTTP 상태코드: {resp.status_code}")
            st.write(f"응답 내용: {resp.text[:500]}")
            data = resp.json()
        except Exception as e:
            st.error(f"서버 연결 오류: {e}")
            st.write(f"API_BASE 주소: {API_BASE}")
            st.stop()
    if not resp.ok:
        st.error(data.get("error", "오류가 발생했습니다."))
        st.stop()
    st.session_state["data"] = data
elif search and not isbn_input:
    st.warning("ISBN을 입력해 주세요.")

if "data" in st.session_state:
    data = st.session_state["data"]

    col_cover, col_info = st.columns([1, 3])
    with col_cover:
        if data.get("cover"):
            st.image(data["cover"], width=150)
        else:
            st.markdown("_(표지 없음)_")
    with col_info:
        st.markdown(f"**저자** {data.get('author_raw', '—')}")
        st.markdown(f"**출판사** {data.get('publisher', '—')}")
        st.markdown(f"**출판일** {data.get('pub_date', '—')}")
        st.markdown(f"**ISBN-13** `{data.get('isbn13', '—')}`")

    st.divider()
    st.info("✎ 표제·부제목을 수정하면 245 필드가 실시간으로 업데이트됩니다.")

    col_t, col_s = st.columns(2)
    with col_t:
        edit_title = st.text_input("$a 본표제", value=data.get("title", ""), key="edit_title")
    with col_s:
        edit_subtitle = st.text_input("$b 부제목 (없으면 비워두세요)", value=data.get("subtitle", ""), key="edit_subtitle")

    authors = data.get("authors", [])
    PRIMARY = {"지은이", "저자", "글", "글쓴이", ""}
    ROLE_LABEL_MAP = {
        "옮긴이": "옮긴이", "역자": "옮긴이", "번역": "옮긴이",
        "그린이": "그린이", "그림": "그린이", "일러스트": "그린이",
        "사진": "사진", "감수": "감수", "편저": "편저", "편역": "편역",
        "엮은이": "엮은이", "편집": "엮은이", "해설": "해설",
    }

    persons = [a for a in authors if not a.get("is_org")]
    primary = [a for a in persons if a.get("role", "") in PRIMARY]
    secondary = [a for a in persons if a.get("role", "") not in PRIMARY]

    role_groups = {}
    for a in secondary:
        label = ROLE_LABEL_MAP.get(a.get("role", ""), a.get("role", ""))
        role_groups.setdefault(label, []).append(a["name"])

    f245 = f"$a {edit_title}"
    if edit_subtitle:
        f245 += f" $b : {edit_subtitle}"

    if primary:
        f245 += f" /$d {primary[0]['name']}"
        for a in primary[1:]:
            f245 += f" ,$e {a['name']}"
        for lbl, names in role_groups.items():
            for name in names:
                f245 += f" ;$e {name}"
    elif role_groups:
        all_names = [n for ns in role_groups.values() for n in ns]
        f245 += f" /$d {all_names[0]}"
        for n in all_names[1:]:
            f245 += f" ,$e {n}"

    f245_full = f"245 00 {f245}"

    st.divider()
    st.markdown("**245 00** — 표제와 책임표시사항")
    st.code(f245_full, language=None)

    f700_list = data["marc"].get("f700", [])
    if f700_list:
        st.markdown("**700 1_** — 개인명 부출기입")
        st.code("\n".join(f700_list), language=None)

    f710_list = data["marc"].get("f710", [])
    if f710_list:
        st.markdown("**710 0_** — 기관명 부출기입")
        st.code("\n".join(f710_list), language=None)

    st.divider()
    all_fields = [f245_full] + f700_list + f710_list
    st.text_area(
        "전체 MARC 필드 (복사용)",
        value="\n".join(all_fields),
        height=120,
        help="Ctrl+A → Ctrl+C 로 전체 복사하세요.",
    )
