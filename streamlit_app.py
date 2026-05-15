import json
import time

import requests
import streamlit as st

# Streamlit 규칙: set_page_config가 스크립트에서 첫 호출이어야 함 (secrets보다 먼저)
st.set_page_config(page_title="KORMARC 자동 생성기", page_icon="📚", layout="centered")

try:
    API_BASE = str(st.secrets["API_BASE"]).strip().rstrip("/")
except Exception:
    API_BASE = "http://localhost:5000"

ISBN_API_TIMEOUT = 120
try:
    _tmo = st.secrets["ISBN_API_TIMEOUT"]
    ISBN_API_TIMEOUT = max(30, min(int(str(_tmo).strip()), 300))
except Exception:
    ISBN_API_TIMEOUT = 120


def _api_root() -> str:
    return (API_BASE or "").strip().rstrip("/")


def _health_url() -> str:
    return f"{_api_root()}/health"


def _isbn_endpoint() -> str:
    return f"{_api_root()}/api/isbn"


st.markdown("""
<style>
.marc-label {
    font-size: 11px; font-weight: 700; letter-spacing: 0.1em;
    text-transform: uppercase; color: #6c757d; margin: 14px 0 4px 0;
}
.status-ok   { background:#d4edda; color:#155724; border:1px solid #c3e6cb; border-radius:6px; padding:8px 14px; font-size:13px; margin-bottom:12px; }
.status-wake { background:#fff3cd; color:#856404; border:1px solid #ffeeba; border-radius:6px; padding:8px 14px; font-size:13px; margin-bottom:12px; }
</style>
""", unsafe_allow_html=True)

st.title("📚 KORMARC 자동 생성기")
st.caption("알라딘 API 연동 · 245 / 246 / 500 / 700 / 710 / 900 필드 자동 생성")
st.caption("백엔드: " + (_api_root() or "(설정 없음)"))


def check_server():
    try:
        resp = requests.get(_health_url(), timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def wakeup_server():
    for _ in range(12):
        try:
            resp = requests.get(_health_url(), timeout=8)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


@st.cache_data(ttl=60)
def get_server_status():
    return check_server()


try:
    server_ok = get_server_status()
except Exception:
    server_ok = False
if server_ok:
    st.markdown('<div class="status-ok">🟢 서버 연결됨</div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="status-wake">🟡 서버 슬립 상태 — 조회 시 자동으로 깨웁니다 (최대 60초 소요)</div>', unsafe_allow_html=True)

st.divider()

col1, col2 = st.columns([4, 1])
with col1:
    isbn_input = st.text_input(
        "ISBN", placeholder="ISBN-13 또는 ISBN-10 예) 9791124070871",
        label_visibility="collapsed", max_chars=17,
    )
with col2:
    search = st.button("조회", use_container_width=True, type="primary")


def fetch_book(isbn_clean):
    if not check_server():
        with st.status("⏳ 서버를 깨우는 중...", expanded=True) as status:
            ok = wakeup_server()
            if ok:
                status.update(label="✅ 서버 준비 완료!", state="complete")
            else:
                status.update(label="❌ 서버 연결 실패", state="error")
                st.error("서버를 깨울 수 없습니다. 잠시 후 다시 시도해주세요.")
                st.stop()
    try:
        # (connect 초, read 초) — 서버가 느려도 응답 본문 대기만 길게
        resp = requests.get(
            _isbn_endpoint(),
            params={"isbn": isbn_clean},
            timeout=(10, ISBN_API_TIMEOUT),
        )
        return resp
    except Exception as e:
        st.error("서버 연결 오류: " + str(e))
        st.stop()


def parse_isbn_response(resp):
    """
    /api/isbn JSON 파싱. 실패 시 (None, 사람이 읽을 수 있는 이유).
    """
    if not resp.content:
        return None, "빈 본문"
    try:
        return resp.json(), None
    except ValueError:
        pass
    text = (resp.text or "").lstrip("\ufeff").strip()
    if not text:
        return None, "본문이 비어 있음"
    try:
        return json.loads(text), None
    except ValueError:
        return None, "JSON 형식이 아님"


if search and isbn_input:
    isbn_clean = isbn_input.replace("-", "").replace(" ", "")
    with st.spinner("도서 정보를 가져오는 중..."):
        resp = fetch_book(isbn_clean)
    data, parse_err = parse_isbn_response(resp)
    if data is None:
        full_url = f"{_isbn_endpoint()}?isbn={isbn_clean}"
        body_l = (resp.text or "")[:800].lower()
        if "<html" in body_l or "<!doctype" in body_l:
            if resp.status_code >= 500:
                hint = (
                    "응답이 **HTML 오류 페이지**입니다. **Gunicorn/Render 타임아웃**(조회가 너무 오래 걸림), "
                    "**워커 크래시**, 또는 **잘못된 URL**일 수 있습니다. "
                    "[Render 대시보드 → Logs](https://dashboard.render.com)에서 오류를 확인하고, "
                    "시작 명령에 `--timeout 180`(이상)이 있는지 확인하세요. "
                    "`API_BASE`는 **이 Flask API의 루트**(예: `https://….onrender.com`, `/health`가 `{\"status\":\"ok\"}`)여야 합니다."
                )
            else:
                hint = (
                    "응답이 **HTML**입니다. `API_BASE`에 **Streamlit 주소**(.streamlit.app)가 들어갔거나, "
                    "Flask가 아닌 페이지를 열었을 수 있습니다. **Render에 배포한 API 주소**만 넣으세요."
                )
        elif _api_root().startswith("http://localhost") or _api_root().startswith("127.0.0.1"):
            hint = "Streamlit Cloud에서는 **localhost**로는 PC의 서버에 닿을 수 없습니다. **Render에 배포한 주소**를 Secrets의 `API_BASE`에 넣으세요."
        elif resp.status_code >= 500:
            hint = "백엔드 **500**입니다. [Render](https://dashboard.render.com) → 해당 Web Service → **Logs**에서 빨간 Python 오류를 확인하세요."
        else:
            hint = "`API_BASE`가 **Flask API 루트**인지 확인하세요. (끝에 `/api/isbn`을 붙이지 말고, 도메인만)"
        st.error(
            "응답을 JSON으로 읽을 수 없습니다. "
            f"HTTP **{resp.status_code}** ({parse_err}).\n\n"
            f"**실제로 요청한 주소:** `{full_url}`\n\n"
            + hint
        )
        snippet = (resp.text or "")[:1500].strip()
        if snippet:
            with st.expander("응답 미리보기 (처음 1500자)"):
                st.code(snippet, language=None)
        st.stop()
    if not resp.ok:
        msg = data.get("error", "오류가 발생했습니다.")
        if data.get("traceback"):
            with st.expander("서버 상세 (Render에 `API_DEBUG=true` 설정 시)"):
                st.code(data["traceback"], language=None)
        st.error(msg)
        st.stop()
    st.cache_data.clear()
    st.session_state["data"] = data
elif search and not isbn_input:
    st.warning("ISBN을 입력해 주세요.")


if "data" in st.session_state:
    data = st.session_state["data"]
    marc = data.get("marc", {})

    # ── 도서 기본 정보 ───────────────────────────
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

    # ── 표제 수정 입력 ───────────────────────────
    st.info("✎ 표제·부제목을 수정하면 245 필드가 실시간 업데이트됩니다.")

    col_t, col_s, col_n = st.columns([3, 3, 1])
    with col_t:
        edit_title = st.text_input("$a 본표제", value=data.get("title", ""), key="et")
    with col_s:
        edit_subtitle = st.text_input("$b 부제목", value=data.get("subtitle", ""), key="es")
    with col_n:
        edit_part = st.text_input("$n 권차", value=data.get("part_number", ""), key="en")

    # ── 245 필드: 백엔드 값 기반으로 표제만 실시간 수정 ──
    # 백엔드에서 내려온 f245에서 $a 이후 부분을 교체
    f245_from_api = marc.get("f245", "")

    # 표제/부제목/권차 수정이 있으면 $a~$b 부분만 교체, 나머지($d 이후)는 백엔드 값 유지
    if f245_from_api:
        # $d 이후 책임표시 부분 추출
        resp_part = ""
        if " /$d " in f245_from_api:
            resp_part = " /$d " + f245_from_api.split(" /$d ", 1)[1]
        elif " /$c " in f245_from_api:
            resp_part = " /$c " + f245_from_api.split(" /$c ", 1)[1]

        # 표제 재조립
        f245_body = "$a " + edit_title
        if edit_part:
            f245_body += " $n " + edit_part
        if edit_subtitle:
            f245_body += " $b : " + edit_subtitle
        f245_body += resp_part

        f245_full = "245 00 " + f245_body
    else:
        f245_full = f245_from_api

    st.divider()

    # ── MARC 필드 표시 ───────────────────────────
    st.markdown('<div class="marc-label">245 00 — 표제와 책임표시사항</div>', unsafe_allow_html=True)
    st.code(f245_full, language=None)

    f246 = marc.get("f246", "")
    if f246:
        st.markdown('<div class="marc-label">246 19 — 원제</div>', unsafe_allow_html=True)
        st.code(f246, language=None)

    f500 = marc.get("f500", "")
    if f500:
        st.markdown('<div class="marc-label">500 \\\\ — 원저자명 주기</div>', unsafe_allow_html=True)
        st.code(f500, language=None)

    f700_list = marc.get("f700", [])
    if f700_list:
        st.markdown('<div class="marc-label">700 1_ / 700 0_ — 개인명 부출기입</div>', unsafe_allow_html=True)
        st.code("\n".join(f700_list), language=None)

    f710_list = marc.get("f710", [])
    if f710_list:
        st.markdown('<div class="marc-label">710 0_ — 기관명 부출기입</div>', unsafe_allow_html=True)
        st.code("\n".join(f710_list), language=None)

    f900_list = marc.get("f900", [])
    if f900_list:
        st.markdown('<div class="marc-label">900 10 — 원저자 한글명 부출기입</div>', unsafe_allow_html=True)
        st.code("\n".join(f900_list), language=None)

    st.divider()

    # ── 전체 복사용 ──────────────────────────────
    all_fields = [f245_full]
    if f246:
        all_fields.append(f246)
    if f500:
        all_fields.append(f500)
    all_fields += f700_list + f710_list + f900_list

    st.text_area(
        "📋 전체 MARC 필드 (복사용)",
        value="\n".join(all_fields),
        height=180,
        help="Ctrl+A → Ctrl+C 로 전체 복사",
    )

    st.divider()

    with st.expander("💡 서버 슬립 방지 (UptimeRobot 무료)"):
        st.markdown(f"""
1. [uptimerobot.com](https://uptimerobot.com) 무료 가입
2. **New Monitor** → Monitor Type: `HTTP(s)`
3. URL: `{_health_url()}` / Interval: `5 minutes`
4. 저장하면 5분마다 자동 핑 → 슬립 방지!
""")
