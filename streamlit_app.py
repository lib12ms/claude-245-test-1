import streamlit as st
import requests
import re
import time

API_BASE = st.secrets.get("API_BASE", "http://localhost:5000")

# ── 상수 ────────────────────────────────────────
ROLE_LABEL = {
    "옮긴이": "옮긴이", "역자": "옮긴이", "번역": "옮긴이",
    "그린이": "그린이", "그림": "그린이", "일러스트": "그린이",
    "사진": "사진", "감수": "감수", "편저": "편저", "편역": "편역",
    "엮은이": "엮은이", "편집": "엮은이", "해설": "해설",
}
PRIMARY_ROLES = {"지은이", "저자", "글", "글쓴이", ""}
PRIMARY_LABEL = {"지은이": "지은이", "저자": "지은이", "글": "지은이", "글쓴이": "지은이", "": "지은이"}

# ── 스타일 ──────────────────────────────────────
st.set_page_config(page_title="KORMARC 자동 생성기", page_icon="📚", layout="centered")

st.markdown("""
<style>
.marc-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #6c757d;
    margin: 14px 0 4px 0;
}
.server-status {
    font-size: 13px;
    padding: 8px 14px;
    border-radius: 6px;
    margin-bottom: 12px;
}
.status-ok { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
.status-wake { background: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
.status-err { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
</style>
""", unsafe_allow_html=True)

# ── 서버 상태 확인 및 웨이크업 ──────────────────
def check_server():
    try:
        resp = requests.get(API_BASE + "/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False

def wakeup_server():
    """Render 슬립 서버 깨우기 - 최대 60초 대기"""
    for i in range(12):
        try:
            resp = requests.get(API_BASE + "/health", timeout=8)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(5)
    return False

# 앱 시작 시 서버 상태 확인 (캐시: 60초)
@st.cache_data(ttl=60)
def get_server_status():
    return check_server()

# ── 헤더 ────────────────────────────────────────
st.title("📚 KORMARC 자동 생성기")
st.caption("알라딘 API 연동 · 245 / 700 / 710 / 900 필드 자동 생성")

# ── 서버 상태 표시 ───────────────────────────────
server_ok = get_server_status()
if server_ok:
    st.markdown('<div class="server-status status-ok">🟢 서버 연결됨 — 바로 조회할 수 있습니다.</div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="server-status status-wake">🟡 서버가 슬립 상태입니다. 조회 시 자동으로 깨웁니다 (최대 30~60초 소요).</div>', unsafe_allow_html=True)

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
def fetch_book(isbn_clean):
    # 서버가 슬립 중이면 먼저 깨우기
    if not check_server():
        with st.status("⏳ 서버를 깨우는 중입니다... (최대 60초 소요)", expanded=True) as status:
            st.write("Render 무료 플랜은 15분 미사용 시 슬립 상태가 됩니다.")
            ok = wakeup_server()
            if ok:
                status.update(label="✅ 서버가 준비됐습니다!", state="complete")
            else:
                status.update(label="❌ 서버 연결 실패", state="error")
                st.error("서버를 깨울 수 없습니다. 잠시 후 다시 시도해주세요.")
                st.stop()

    try:
        resp = requests.get(
            API_BASE + "/api/isbn",
            params={"isbn": isbn_clean},
            timeout=20,
        )
        return resp
    except Exception as e:
        st.error("서버 연결 오류: " + str(e))
        st.stop()

if search and isbn_input:
    isbn_clean = isbn_input.replace("-", "").replace(" ", "")
    with st.spinner("도서 정보를 가져오는 중..."):
        resp = fetch_book(isbn_clean)
        try:
            data = resp.json()
        except Exception:
            st.error("응답을 읽을 수 없습니다. 다시 시도해주세요.")
            st.stop()
    if not resp.ok:
        st.error(data.get("error", "오류가 발생했습니다."))
        st.stop()
    # 서버 상태 캐시 갱신
    st.cache_data.clear()
    st.session_state["data"] = data

elif search and not isbn_input:
    st.warning("ISBN을 입력해 주세요.")

# ── 결과 표시 ────────────────────────────────────
if "data" in st.session_state:
    data = st.session_state["data"]

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
    st.info("✎ 표제·부제목을 수정하면 245 필드가 실시간 업데이트됩니다.")

    col_t, col_s = st.columns(2)
    with col_t:
        edit_title = st.text_input("$a 본표제", value=data.get("title", ""), key="et")
    with col_s:
        edit_subtitle = st.text_input("$b 부제목 (없으면 비워두세요)", value=data.get("subtitle", ""), key="es")

    # ── 245 재조립 ───────────────────────────────
    authors = data.get("authors", [])
    persons = [a for a in authors if not a.get("is_org")]
    primary = [a for a in persons if a.get("role", "") in PRIMARY_ROLES]
    secondary = [a for a in persons if a.get("role", "") not in PRIMARY_ROLES]

    role_groups = {}
    for a in secondary:
        lbl = ROLE_LABEL.get(a.get("role", ""), a.get("role", ""))
        role_groups.setdefault(lbl, []).append(a)

    f245 = "$a " + edit_title
    if edit_subtitle:
        f245 += " $b : " + edit_subtitle

    if primary:
        pl = PRIMARY_LABEL.get(primary[0].get("role", ""), "지은이")
        f245 += " /$d " + pl + ": " + primary[0]["name"]
        for a in primary[1:]:
            f245 += " ,$e " + PRIMARY_LABEL.get(a.get("role", ""), "지은이") + ": " + a["name"]
        for lbl, members in role_groups.items():
            for a in members:
                f245 += " ;$e " + lbl + ": " + a["name"]
    elif role_groups:
        all_m = [m for ms in role_groups.values() for m in ms]
        f245 += " /$d " + ROLE_LABEL.get(all_m[0].get("role", ""), all_m[0].get("role", "")) + ": " + all_m[0]["name"]
        for a in all_m[1:]:
            f245 += " ,$e " + ROLE_LABEL.get(a.get("role", ""), a.get("role", "")) + ": " + a["name"]

    f245_full = "245 00 " + f245

    st.divider()

    st.markdown('<div class="marc-label">245 00 — 표제와 책임표시사항</div>', unsafe_allow_html=True)
    st.code(f245_full, language=None)

    f700_list = data["marc"].get("f700", [])
    if f700_list:
        st.markdown('<div class="marc-label">700 1_ — 개인명 부출기입</div>', unsafe_allow_html=True)
        st.code("\n".join(f700_list), language=None)

    f710_list = data["marc"].get("f710", [])
    if f710_list:
        st.markdown('<div class="marc-label">710 0_ — 기관명 부출기입</div>', unsafe_allow_html=True)
        st.code("\n".join(f710_list), language=None)

    f900_list = data["marc"].get("f900", [])
    if f900_list:
        st.markdown('<div class="marc-label">900 10 — 한국어명 부출기입</div>', unsafe_allow_html=True)
        st.code("\n".join(f900_list), language=None)

    st.divider()

    all_fields = [f245_full] + f700_list + f710_list + f900_list
    st.text_area(
        "📋 전체 MARC 필드 (복사용)",
        value="\n".join(all_fields),
        height=160,
        help="Ctrl+A → Ctrl+C 로 전체 복사",
    )

    st.divider()
    with st.expander("💡 서버 슬립 방지 방법 (UptimeRobot 무료)"):
        st.markdown("""
1. [uptimerobot.com](https://uptimerobot.com) 무료 가입
2. **New Monitor** 클릭
3. 아래처럼 설정:
   - Monitor Type: `HTTP(s)`
   - URL: `""" + API_BASE + """/health`
   - Interval: `5 minutes`
4. 저장하면 5분마다 자동으로 서버를 깨워서 슬립 방지!
        """)
