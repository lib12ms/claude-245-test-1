import json
import re
import time
import uuid

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

ROLE_LABEL_245 = {
    "옮긴이": "옮긴이", "역자": "옮긴이", "번역": "옮긴이", "편역": "편역",
    "그린이": "그린이", "그림": "그린이", "일러스트": "그린이",
    "사진": "사진", "감수": "감수", "편저": "편저",
    "엮은이": "엮은이", "편집": "엮은이", "해설": "해설",
    "저자": "지은이", "글": "지은이", "글쓴이": "지은이", "지은이": "지은이",
    "원작": "원작",
}

# 역할 → 245 동사형
_ROLE_VERB_245_UI: dict[str, str] = {
    "지은이": "지음",  "저자":   "지음",  "글쓴이": "지음",  "": "지음",
    "옮긴이": "옮김",  "역자":   "옮김",  "번역":   "옮김",
    "엮은이": "엮음",  "편집":   "엮음",
    "편저":   "편저",  "편역":   "편역",
    "그린이": "그림",
    "글":     "글",    "그림":   "그림",  "원작":   "원작",
    "일러스트": "일러스트",  "드로잉": "드로잉",  "그래픽": "그래픽",
    "사진":   "사진",  "해설":   "해설",
}

# 동사형 → 역할 (역매핑, parse 용)
_VERB_TO_ROLE_UI: dict[str, str] = {
    "지음": "지은이",  "옮김": "옮긴이",  "엮음": "엮은이",
    "편저": "편저",    "편역": "편역",
    "그림": "그림",    "글":   "글",      "원작": "원작",
    "일러스트": "일러스트",  "드로잉": "드로잉",  "그래픽": "그래픽",
    "사진": "사진",    "해설": "해설",
}
PRIMARY_ROLES_245 = {"지은이", "저자", "글", "글쓴이", ""}
ROLES_EXCLUDED_FROM_245 = frozenset({"감수"})


def _role_label_for_245_ui(role: str) -> str:
    r = (role or "").strip()
    if not r or r in PRIMARY_ROLES_245:
        return "지은이"
    return ROLE_LABEL_245.get(r, r)


def extract_resp_suffix_from_f245(f245: str) -> str:
    """245 문자열에서 책임표시 접미사 (` /$d…` 또는 ` /$c …`)."""
    for marker in (" /$d", " /$c "):
        if marker in f245:
            return f245.split(marker, 1)[1].strip()
    return ""


def _parse_role_name_block(block: str, default_role: str) -> tuple[str, list[str]]:
    block = block.strip()
    if not block:
        return default_role, []
    # 구형: "역할: 이름"
    m = re.match(r"^([^:]+):\s*(.+)$", block)
    if m:
        role = m.group(1).strip()
        names = [n.strip() for n in re.split(r"\s*,\s*", m.group(2).strip()) if n.strip()]
        return role, names
    # 신형: "이름 동사형" — 끝 단어가 동사 매핑에 있는 경우
    for verb in sorted(_VERB_TO_ROLE_UI, key=len, reverse=True):
        if block.endswith(" " + verb):
            name = block[: -len(verb)].strip()
            if name:
                return _VERB_TO_ROLE_UI[verb], [name]
    return default_role, [block] if block else []


def parse_responsibility_entries(resp_suffix: str) -> list[dict]:
    """
    책임표시 접미사 → [{"role": "지은이", "name": "…"}, …].
    신규 (/$d … ,$e … ;$e …) 및 구형 (;$옮긴이: …) 모두 처리.
    """
    text = (resp_suffix or "").strip()
    if not text:
        return []

    if text.startswith("/$d"):
        text = text[4:].lstrip()
    elif text.startswith("$d"):
        text = text[2:].lstrip()

    # 구형: ;$옮긴이: (역할어가 식별기호)
    if re.search(r";\$[^e\s]", text) and not re.search(r";\$e\s", text):
        entries: list[dict] = []
        parts = re.split(r"\s*;\$", text)
        role = "지은이"
        for part in parts:
            part = part.strip()
            if not part:
                continue
            role, names = _parse_role_name_block(part, role)
            for name in names:
                entries.append({"role": role, "name": name})
        return entries

    _SENTINEL = "__sentinel__"
    entries: list[dict] = []
    current_role = "지은이"

    tokens = re.split(r"(\s*,\$e\s*|\s*;\$e\s*)", text)

    # 세그먼트를 ;$e 경계로 그룹화
    groups: list[list[str]] = []
    current_group: list[str] = []

    first = (tokens[0] or "").strip()
    if first:
        current_group.append(first)

    i = 1
    while i < len(tokens):
        delim = tokens[i]
        content = (tokens[i + 1] if i + 1 < len(tokens) else "").strip()
        i += 2
        if not content:
            continue
        if ";$e" in delim:
            if current_group:
                groups.append(current_group)
            current_group = [content]
        else:
            current_group.append(content)
    if current_group:
        groups.append(current_group)

    # 각 그룹에서 마지막 동사로 역할 결정 후 전체 적용
    for group in groups:
        group_role = current_role
        for content in reversed(group):
            r, _ = _parse_role_name_block(content, _SENTINEL)
            if r != _SENTINEL:
                group_role = r
                break
        current_role = group_role
        for content in group:
            _, names = _parse_role_name_block(content, current_role)
            for name in names:
                entries.append({"role": current_role, "name": name})

    return entries


def entries_from_authors(authors: list[dict]) -> list[dict]:
    entries: list[dict] = []
    for a in authors or []:
        if a.get("is_org"):
            continue
        name = (a.get("name") or "").strip()
        if not name:
            continue
        role = _role_label_for_245_ui(a.get("role", ""))
        if role in ROLES_EXCLUDED_FROM_245:
            continue
        entries.append({
            "role": role,
            "name": name,
        })
    return entries


def build_responsibility_marc(entries: list[dict]) -> str:
    """저자 목록 → /$d 이름 지음 ;$e 이름 옮김"""
    groups: list[tuple[str, list[str]]] = []
    for e in entries:
        role = (e.get("role") or "지은이").strip() or "지은이"
        if role in ROLES_EXCLUDED_FROM_245:
            continue
        name = (e.get("name") or "").strip()
        if not name:
            continue
        if groups and groups[-1][0] == role:
            groups[-1][1].append(name)
        else:
            groups.append((role, [name]))

    parts: list[str] = []
    for i, (role, names) in enumerate(groups):
        verb = _ROLE_VERB_245_UI.get(role, role)
        if i == 0:
            e_str = names[0] + "".join(f" ,$e {n}" for n in names[1:]) + f" {verb}"
            parts.append(f"/$d {e_str}")
        else:
            e_str = f";$e {names[0]}" + "".join(f" ,$e {n}" for n in names[1:]) + f" {verb}"
            parts.append(f" {e_str}")
    return "".join(parts)


def init_resp_entries_for_data(data: dict) -> list[dict]:
    marc = data.get("marc", {}) or {}
    f245 = marc.get("f245", "") or ""
    suffix = extract_resp_suffix_from_f245(f245)
    entries = parse_responsibility_entries(suffix)
    if entries:
        return entries
    return entries_from_authors(data.get("authors") or [])


def _entries_with_ids(entries: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in entries:
        out.append({
            "id": e.get("id") or str(uuid.uuid4()),
            "role": e.get("role") or "지은이",
            "name": e.get("name") or "",
        })
    return out


def reset_book_ui_state(data: dict) -> None:
    """
    새 ISBN 조회 성공 시 이전 도서의 Streamlit 위젯 상태를 제거.
    (고정 key et/es/en, 복사용 text_area 등이 새 data와 섞이는 현상 방지)
    """
    drop: list[str] = []
    for k in st.session_state:
        if k in ("data", "resp_entries", "_resp_isbn", "book_ui_id", "active_isbn"):
            drop.append(k)
        elif k in ("et", "es", "en"):
            drop.append(k)
        elif k.startswith(("resp_role_", "resp_name_", "resp_del_", "marc_all_")):
            drop.append(k)
    for k in drop:
        st.session_state.pop(k, None)

    st.session_state["data"] = data
    st.session_state["active_isbn"] = data.get("isbn13") or ""
    st.session_state["book_ui_id"] = str(uuid.uuid4())
    st.session_state["_resp_isbn"] = st.session_state["active_isbn"]
    st.session_state["resp_entries"] = _entries_with_ids(init_resp_entries_for_data(data))


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
                    "[Render 대시보드 → Logs](https://dashboard.render.com)에서 오류를 확인하세요. "
                    "공개 URL은 **약 30초**를 넘기면 HTML 500이 납니다. "
                    "`API_BASE`는 Flask 루트(예: `https://….onrender.com`, `/health` → `{\"status\":\"ok\"}`)여야 합니다."
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
    reset_book_ui_state(data)
    st.rerun()
elif search and not isbn_input:
    st.warning("ISBN을 입력해 주세요.")


if "data" in st.session_state:
    data = st.session_state["data"]
    marc = data.get("marc", {})
    ui_id = st.session_state.get("book_ui_id") or st.session_state.get("active_isbn") or "default"

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

    # ── 245 표제·책임표시 수정 ───────────────────
    st.info("✎ 표제·부제목·책임표시(역할어·이름)를 수정하면 245 필드가 실시간 업데이트됩니다.")

    col_t, col_s, col_n = st.columns([3, 3, 1])
    with col_t:
        edit_title = st.text_input(
            "$a 본표제", value=data.get("title", ""), key=f"et_{ui_id}",
        )
    with col_s:
        edit_subtitle = st.text_input(
            "$b 부제목", value=data.get("subtitle", ""), key=f"es_{ui_id}",
        )
    with col_n:
        edit_part = st.text_input(
            "$n 권차", value=data.get("part_number", ""), key=f"en_{ui_id}",
        )

    isbn_key = data.get("isbn13") or ""
    if st.session_state.get("_resp_isbn") != isbn_key:
        st.session_state["_resp_isbn"] = isbn_key
        st.session_state["resp_entries"] = _entries_with_ids(init_resp_entries_for_data(data))
    if "resp_entries" not in st.session_state:
        st.session_state["resp_entries"] = _entries_with_ids(init_resp_entries_for_data(data))

    st.markdown("**책임표시** — `/$d`(첫 저자), `,$e`(같은 역할), `;$e`(역할 변경)")
    rh1, rh2, rh3 = st.columns([2, 4, 1])
    with rh1:
        st.caption("역할어 (지은이·옮긴이 …)")
    with rh2:
        st.caption("이름")
    with rh3:
        st.caption("")

    updated_entries: list[dict] = []
    for entry in st.session_state["resp_entries"]:
        eid = entry["id"]
        rc, nc, dc = st.columns([2, 4, 1])
        with rc:
            role_val = st.text_input(
                "역할",
                value=entry.get("role", "지은이"),
                key=f"resp_role_{eid}",
                label_visibility="collapsed",
            )
        with nc:
            name_val = st.text_input(
                "이름",
                value=entry.get("name", ""),
                key=f"resp_name_{eid}",
                label_visibility="collapsed",
            )
        with dc:
            st.write("")
            if st.button("삭제", key=f"resp_del_{eid}"):
                st.session_state["resp_entries"] = [
                    e for e in st.session_state["resp_entries"] if e["id"] != eid
                ]
                st.rerun()
        updated_entries.append({"id": eid, "role": role_val, "name": name_val})
    st.session_state["resp_entries"] = updated_entries

    ba, bb = st.columns(2)
    with ba:
        if st.button("＋ 책임표시 추가", use_container_width=True):
            st.session_state["resp_entries"].append({
                "id": str(uuid.uuid4()),
                "role": "지은이",
                "name": "",
            })
            st.rerun()
    with bb:
        if st.button("API 값으로 되돌리기", use_container_width=True):
            st.session_state["resp_entries"] = _entries_with_ids(init_resp_entries_for_data(data))
            for entry in st.session_state["resp_entries"]:
                eid = entry["id"]
                st.session_state.pop(f"resp_role_{eid}", None)
                st.session_state.pop(f"resp_name_{eid}", None)
            st.rerun()

    f245_body = "$a " + edit_title
    if edit_part:
        f245_body += " $n " + edit_part
    if edit_subtitle:
        f245_body += " :$b " + edit_subtitle
    resp_marc = build_responsibility_marc(st.session_state["resp_entries"])
    if resp_marc:
        f245_body += f" {resp_marc}"
    f245_full = "245 00 " + f245_body

    st.divider()

    # ── MARC 필드 표시 ───────────────────────────
    st.markdown('<div class="marc-label">245 00 — 표제와 책임표시사항</div>', unsafe_allow_html=True)
    st.code(f245_full, language=None)

    f246 = marc.get("f246", "")
    if f246:
        st.markdown('<div class="marc-label">246 19 — 원제</div>', unsafe_allow_html=True)
        st.code(f246, language=None)

    f500_raw = marc.get("f500", "")
    f500_list = f500_raw if isinstance(f500_raw, list) else ([f500_raw] if f500_raw else [])
    if f500_list:
        st.markdown('<div class="marc-label">500 \\\\ — 일반주기</div>', unsafe_allow_html=True)
        st.code("\n".join(f500_list), language=None)

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

    f940 = marc.get("f940", "")
    if f940:
        st.markdown('<div class="marc-label">940 — 한글 표제 읽기 (숫자·영문 포함 시)</div>', unsafe_allow_html=True)
        st.code(f940, language=None)

    st.divider()

    # ── 전체 복사용 ──────────────────────────────
    all_fields = [f245_full]
    if f246:
        all_fields.append(f246)
    all_fields.extend(f500_list)
    all_fields += f700_list + f710_list + f900_list
    if f940:
        all_fields.append(f940)

    st.text_area(
        "📋 전체 MARC 필드 (복사용)",
        value="\n".join(all_fields),
        height=180,
        key=f"marc_all_{ui_id}",
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
