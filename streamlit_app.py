import streamlit as st
import requests

API_BASE = st.secrets.get("API_BASE", "http://localhost:5000")

st.set_page_config(
    page_title="KORMARC 자동 생성기",
    page_icon="📚",
    layout="centered",
)

st.title("📚 KORMARC 자동 생성기")
st.caption("알라딘 API 연동 · 245 / 700 / 710 / 900 필드 자동 생성")
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
    with st.spinner("도서 정보를 가져오는 중..."):
        try:
            resp = requests.get(
                API_BASE + "/api/isbn",
                params={"isbn": isbn_clean},
                timeout=20,
            )
            data = resp.json()
        except Exception as e:
            st.error("서버 연결 오류: " + str(e))
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
        st.markdown("**저자** " + data.get("author_raw", "—"))
        st.markdown("**출판사** " + data.get("publisher", "—"))
        st.markdown("**출판일** " + data.get("pub_date", "—"))
        st.markdown("**ISBN-13** `" + data.get("isbn13", "—") + "`")

    st.divider()
    st.info("✎ 표제·부제목을 수정하면 245 필드가 실시간으로 업데이트됩니다.")

    col_t, col_s = st.columns(2)
    with col_t:
        edit_title = st.text_input(
            "$a 본표제",
            value=data.get("title", ""),
            key="edit_title",
        )
    with col_s:
        edit_subtitle = st.text_input(
            "$b 부제목 (없으면 비워두세요)",
            value=data.get("subtitle", ""),
            key="edit_subtitle",
        )

    import re

    def is_korean(name):
        return bool(re.search(r"[\uac00-\ud7a3]", name))

    def is_western_str(name):
        return bool(re.search(r"[A-Za-z]", name)) and not is_korean(name)

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
        if label not in role_groups:
            role_groups[label] = []
        role_groups[label].append(a)

    f245 = "$a " + edit_title
    if edit_subtitle:
        f245 += " $b : " + edit_subtitle

    if primary:
        f245 += " /$d " + primary[0]["name"]
        for a in primary[1:]:
            f245 += " ,$e " + a["name"]
        for lbl in role_groups:
            for a in role_groups[lbl]:
                f245 += " ;$e " + lbl + " " + a["name"]
    elif role_groups:
        all_members = []
        for members in role_groups.values():
            all_members.extend(members)
        f245 += " /$d " + all_members[0]["name"]
        for a in all_members[1:]:
            f245 += " ,$e " + a["name"]

    f245_full = "245 00 " + f245

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

    f900_list = data["marc"].get("f900", [])
    if f900_list:
        st.markdown("**900 10** — 한국어명 부출기입")
        st.code("\n".join(f900_list), language=None)

    st.divider()
    all_fields = [f245_full] + f700_list + f710_list + f900_list
    st.text_area(
        "전체 MARC 필드 (복사용)",
        value="\n".join(all_fields),
        height=150,
        help="Ctrl+A → Ctrl+C 로 전체 복사하세요.",
    )
