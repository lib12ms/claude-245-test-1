# 📚 KORMARC 자동 생성기

알라딘 Open API를 이용해 **ISBN 한 번**으로 KORMARC **245**, **700**, **710** 필드를 자동 생성하는 도서관 사서용 도구입니다.

---

## 아키텍처

```
[Streamlit Cloud]          [Render]
 streamlit_app.py   →  HTTP  →  app.py (Flask API)
  프론트엔드                      백엔드
                                    ↓
                              알라딘 Open API
```

- **백엔드 (Render)** : Flask + Gunicorn, `/api/isbn` 엔드포인트 제공
- **프론트엔드 (Streamlit Cloud)** : 사용자 UI, 백엔드 API 호출

---

## 파일 구조

```
kormarc_v2/
├── backend/
│   ├── app.py              # Flask API 서버 (Render 배포)
│   ├── requirements.txt    # flask, flask-cors, requests, gunicorn
│   └── render.yaml         # Render 배포 설정
│
└── frontend/
    ├── streamlit_app.py    # Streamlit UI (Streamlit Cloud 배포)
    ├── requirements.txt    # streamlit, requests
    └── .streamlit/
        └── secrets.toml    # API_BASE URL 설정
```

---

## 배포 방법

### 1단계 — 백엔드: Render에 Flask 배포

1. `backend/` 폴더를 GitHub 저장소에 올립니다.
2. [Render](https://render.com) 로그인 → **New Web Service** 선택
3. 해당 GitHub 저장소 연결
4. 아래와 같이 설정합니다.

| 항목 | 값 |
|------|-----|
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app` |

5. **Environment Variables** 탭에서 추가:

| Key | Value |
|-----|-------|
| `ALADIN_API_KEY` | `ttbboyeong09010919001` |

6. 배포 완료 후 Render가 제공하는 URL 복사  
   예) `https://kormarc-api.onrender.com`

---

### 2단계 — 프론트엔드: Streamlit Cloud에 배포

1. `frontend/` 폴더를 GitHub 저장소에 올립니다.
2. [Streamlit Cloud](https://streamlit.io/cloud) 로그인 → **New app** 선택
3. 해당 저장소 연결, `streamlit_app.py` 파일 선택
4. **Advanced settings → Secrets** 에 아래 내용 입력:

```toml
API_BASE = "https://kormarc-api.onrender.com"
```

> Render URL을 1단계에서 복사한 실제 주소로 바꿔 입력하세요.

5. **Deploy** 클릭

---

## 로컬 실행 (개발용)

### 백엔드

```bash
cd backend
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

### 프론트엔드

```bash
cd frontend
pip install -r requirements.txt

# .streamlit/secrets.toml 에서 API_BASE를 로컬로 변경
# API_BASE = "http://localhost:5000"

streamlit run streamlit_app.py
# → http://localhost:8501
```

---

## MARC 필드 생성 규칙

### 245 — 표제와 책임표시사항

| 식별기호 | 내용 | 예시 |
|---------|------|------|
| `$a` | 본표제 | `기분이 태도가 되지 않게` |
| `$b` | 부표제 (있을 때만) | `: 기분 따라 행동하다 손해보는 당신을 위한 심리 수업` |
| `/$c` | 첫 번째 저자 | `홍길동` |
| `,$e` | 두 번째 저자부터 공동저자 반복 | `,$e 이순신` |
| `;$e` | 역할어가 다른 저자 (역자·옮긴이·그린이 등) | `;$e 노경아` |

**출력 예시**

```
# 저자 1명
245 00 $a 채식주의자 /$c 한강.

# 부제목 + 역자
245 00 $a 기분이 태도가 되지 않게 $b : 기분 따라 행동하다 손해보는 당신을 위한 심리 수업 /$c 우에니시 아키라 ;$e 노경아.

# 공동저자 여러 명 + 역자
245 00 $a 자바의 정석 $b : 기초편 /$c 남궁성 ,$e 이순신 ,$e 장보고 ;$e Jane Smith.
```

### 700 1_ — 개인명 부출기입

두 번째 저자부터 생성. `$e` 역할어 표시 없음. 서양인은 `성, 이름` 역순 변환.

```
700 1_ $a 이순신,
700 1_ $a Smith, Jane,
```

### 710 0_ — 기관명 부출기입

기관·단체·협의회 등 키워드로 자동 판별해 700 대신 710으로 출력.

```
710 0_ $a 한국도서관협회.
```

---

## 주의사항

- 생성된 필드는 반드시 **목록 규칙에 따라 검토 후** 사용하세요.
- **245 지시기호**는 주기입표목(1XX) 유무에 따라 수동 조정이 필요합니다.
  - 1XX 있음 → `245 00`
  - 1XX 없음 → `245 10`
- Render 무료 플랜은 15분 비활성 시 슬립 상태가 됩니다. 첫 요청이 느릴 수 있습니다.
- API 키는 Render 환경변수로만 관리하고 코드에 직접 커밋하지 마세요.
