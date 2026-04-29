import streamlit as st
import requests
import re

API_BASE = st.secrets.get("API_BASE", "http://localhost:5000")

st.set_page_config(
    page_title="KORMARC 자동 생성기",
    page_icon="📚",
    layout="centered",
)

# ── 스타일 ──────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Nanum+Myeongjo:wght@400;700&family=IBM+Plex+Mono&display=swap');
.marc-box {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    background: #f8f9fa;
    border: 1px solid #dee2e6;
    border-left: 4px solid #2c4a2e;
    border-radius: 4px;
    padding: 12px 16px;
    line-height: 2;
    white-space: pre-wrap;
    word-break: break-all;
}
.field-label {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #495057;
    margin: 12px 0 4px 0;
}
</style>
""", unsafe_allow_html=True)

st.title("📚 KORMARC 자동 생성기")
st.caption("알라딘 API · 245 / 700 / 710 / 900 필드 자동 생성")
st.divider()

# ── ISBN 입력 ────────────────────────────────────
col1, col2 = st.columns([4, 1])
with col1:
    isbn_input = st.text_input(
        "ISBN", placeholder="ISBN-13 또는 ISBN-10  예) 9791124070871",
        label_visibility="collapsed", max_chars=17,
    )
with col2:
    search = st.button("조회", use_container_width=True, type="primary")

# ── API 호출 ─────────────────────────────────────
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

# ── 결과 표시 ────────────────────────────────────
if "data" in st.session_state:
    data = st.session_state["data"]

    # 도서 정보
    col_img, col_info = st.columns([1, 3])
    with col_img:
        if data.get("cover"):
            st.image(data["cover"], width=140)
        else:
            st.caption("_(표지 없음)_")
    with col_info:
        st.markdown("**저자** " + data.get("author_raw", "—"))
        st.markdown("**출판사** " + data.get("publisher", "—"))
        st.markdown("**출판일** " + data.get("pub_date", "—"))
        st.markdown("**ISBN-13** `" + data.get("isbn13", "—") + "`")

    st.divider()

    # ── 표제 편집 ───────────────────────────────
    st.info("✎ 표제·부제목을 수정하면 245 필드가 실시간 업데이트됩니다.")
    col_t, col_s = st.columns(2)
    with col_t:
        edit_title = st.text_input("$a 본표제", value=data.get("title", ""), key="et")
    with col_s:
        edit_subtitle = st.text_input("$b 부제목 (없으면 비워두세요)", value=data.get("subtitle", ""), key="es")

    # ── 245 재조립 (클라이언트) ─────────────────
    authors = data.get("authors", [])
    PRIMARY = {"지은이", "저자", "글", "글쓴이", ""}
    PRIMARY_LBL = {"지은이": "지은이", "저자": "지은이", "글": "지은이", "글쓴이": "지은이", "": "지은이"}
    ROLE_LBL = {
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
        lbl = ROLE_LBL.get(a.get("role", ""), a.get("role", ""))
        role_groups.setdefault(lbl, []).append(a)

    f245 = "$a " + edit_title
    if edit_subtitle:
        f245 += " $b : " + edit_subtitle

    if primary:
        pl = PRIMARY_LBL.get(primary[0].get("role", ""), "지은이")
        f245 += " /$d " + pl + ": " + primary[0]["name"]
        for a in primary[1:]:
            pl2 = PRIMARY_LBL.get(a.get("role", ""), "지은이")
            f245 += " ,$e " + pl2 + ": " + a["name"]
        for lbl, members in role_groups.items():
            for a in members:
                f245 += " ;$e " + lbl + ": " + a["name"]
    elif role_groups:
        all_m = [m for ms in role_groups.values() for m in ms]
        fl = ROLE_LBL.get(all_m[0].get("role", ""), all_m[0].get("role", ""))
        f245 += " /$d " + fl + ": " + all_m[0]["name"]
        for a in all_m[1:]:
            al = ROLE_LBL.get(a.get("role", ""), a.get("role", ""))
            f245 += " ,$e " + al + ": " + a["name"]

    f245_full = "245 00 " + f245

    st.divider()

    # ── MARC 출력 ────────────────────────────────
    st.markdown('<div class="field-label">245 00 — 표제와 책임표시사항</div>', unsafe_allow_html=True)
    st.code(f245_full, language=None)

    f700_list = data["marc"].get("f700", [])
    if f700_list:
        st.markdown('<div class="field-label">700 1_ — 개인명 부출기입</div>', unsafe_allow_html=True)
        st.code("\n".join(f700_list), language=None)

    f710_list = data["marc"].get("f710", [])
    if f710_list:
        st.markdown('<div class="field-label">710 0_ — 기관명 부출기입</div>', unsafe_allow_html=True)
        st.code("\n".join(f710_list), language=None)

    f900_list = data["marc"].get("f900", [])
    if f900_list:
        st.markdown('<div class="field-label">900 10 — 한국어명 부출기입</div>', unsafe_allow_html=True)
        st.code("\n".join(f900_list), language=None)

    st.divider()

    all_fields = [f245_full] + f700_list + f710_list + f900_list
    st.text_area(
        "📋 전체 MARC 필드 (복사용)",
        value="\n".join(all_fields),
        height=160,
        help="Ctrl+A → Ctrl+C 로 전체 복사",
    )
