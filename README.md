# 🇰🇷 대한민국 실시간 인기 검색어

Google BigQuery 공개 데이터셋에서 대한민국 실시간 인기 검색어 Top 25를 가져와 웹에 표시하는 서비스입니다.

## 기술 스택

| 역할 | 기술 |
|------|------|
| Backend | Flask (Python) |
| Auth / DB | Supabase |
| Data Source | Google BigQuery (`bigquery-public-data.google_trends.top_terms`) |
| 로그인 | Google OAuth 2.0 (Supabase 연동) |

---

## 파일 구조

```
googletrendbigquery/
├── app.py                  ← Flask 서버 (API, 인증 미들웨어, 3단계 캐싱)
├── schema.sql              ← Supabase 테이블 정의 (SQL Editor에서 실행)
├── requirements.txt        ← Python 패키지 목록
├── .env.example            ← 환경변수 템플릿
└── templates/
    └── index.html          ← 프론트엔드 (Supabase Auth + 트렌드 카드 UI)
```

---

## 구현된 기능

### API 엔드포인트

| 메서드 | 경로 | 인증 | 설명 |
|--------|------|------|------|
| GET | `/` | 없음 | 메인 웹 페이지 |
| GET | `/api/trends` | JWT 필수 | 인기 검색어 25개 반환 |
| POST | `/api/cache/clear` | JWT 필수 | 인메모리 캐시 강제 초기화 |
| GET | `/health` | 없음 | 서버 상태 및 캐시 나이 확인 |

### 3단계 캐시 구조

BigQuery 호출 비용을 최소화하기 위해 계층형 캐시를 사용합니다.

```
요청
 │
 ├─► L1 인메모리 캐시 (TTL 이내) ──► 응답 (가장 빠름)
 │
 ├─► L2 Supabase trends_cache 테이블 (TTL 이내) ──► 응답 + L1 갱신
 │
 └─► L3 BigQuery 실시간 조회 ──► 응답 + L2(Supabase) upsert + L1 갱신
```

서버를 재시작해도 Supabase에 저장된 캐시를 먼저 읽으므로 불필요한 BigQuery 호출을 방지합니다.

### Supabase 테이블

| 테이블 | 역할 |
|--------|------|
| `trends_cache` | BigQuery 결과 영속 저장. `refresh_date + rank` 기준 upsert |
| `access_logs` | 사용자별 API 호출 기록. 모니터링 및 분석용 |

### 핵심 기능

- **Supabase JWT 인증** — `Authorization: Bearer <token>` 헤더를 검증하는 `require_auth` 데코레이터로 모든 API 보호
- **Google OAuth 로그인** — Supabase JS를 통한 구글 계정 로그인/로그아웃
- **반응형 카드 UI** — 1~3위 금·은·동 강조, 스코어 바, 페이드 애니메이션

### `/api/trends` 응답 예시

```json
{
  "success": true,
  "count": 25,
  "mem_cache_age_seconds": 42,
  "data": [
    { "rank": 1, "term": "검색어", "score": 100, "refresh_date": "2026-05-07" },
    ...
  ]
}
```

---

## 시작하기

### 사전 준비

- Python 3.10 이상
- Google Cloud 프로젝트 (BigQuery API 활성화)
- Supabase 프로젝트

### 1단계 — Supabase 테이블 생성

Supabase 대시보드 → **SQL Editor** → `schema.sql` 파일 내용 전체 붙여넣기 → **Run**

생성되는 테이블:
- `public.trends_cache` — 트렌드 데이터 캐시 (RLS: 인증 사용자 읽기만 허용)
- `public.access_logs` — API 호출 로그 (RLS: 본인 로그만 조회 가능)

### 2단계 — 환경변수 설정

```bash
cp .env.example .env
```

`.env` 파일을 열어 아래 값을 채웁니다.

```env
GCP_PROJECT_ID=your_gcp_project_id
GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json

SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_ANON_KEY=your_supabase_anon_key
SUPABASE_JWT_SECRET=your_supabase_jwt_secret
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key

FLASK_SECRET_KEY=랜덤-문자열-입력
FLASK_DEBUG=false
CACHE_TTL=300
```

#### 값을 찾는 방법

| 변수 | 위치 |
|------|------|
| `GCP_PROJECT_ID` | GCP 콘솔 → 프로젝트 선택 드롭다운 |
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP 콘솔 → IAM → 서비스 계정 → 키 생성 (JSON) |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | Supabase 대시보드 → Project Settings → API |
| `SUPABASE_JWT_SECRET` | Supabase 대시보드 → Project Settings → API → JWT Settings → JWT Secret |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase 대시보드 → Project Settings → API → service_role |

### 3단계 — Supabase Google OAuth 활성화

1. Supabase 대시보드 → **Authentication → Providers → Google** → Enable
2. GCP 콘솔에서 OAuth 2.0 클라이언트 ID 생성 후 Client ID / Secret 입력
3. 승인된 리디렉션 URI에 `https://your-project-id.supabase.co/auth/v1/callback` 추가

### 4단계 — 의존성 설치

```bash
pip install -r requirements.txt
```

### 5단계 — 서버 실행

```bash
python app.py
```

브라우저에서 `http://localhost:5000` 접속 후 **Google 계정으로 시작하기** 클릭

---

## 환경변수 레퍼런스

| 변수 | 필수 | 기본값 | 설명 |
|------|------|--------|------|
| `GCP_PROJECT_ID` | ✅ | — | BigQuery 요청에 사용할 GCP 프로젝트 ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | ✅ | — | 서비스 계정 JSON 키 경로 |
| `SUPABASE_URL` | ✅ | — | Supabase 프로젝트 URL |
| `SUPABASE_ANON_KEY` | ✅ | — | Supabase 공개 키 (프론트엔드에서 사용) |
| `SUPABASE_JWT_SECRET` | ✅ | — | JWT 서명 검증용 시크릿 (절대 외부 노출 금지) |
| `SUPABASE_SERVICE_ROLE_KEY` | ✅ | — | Supabase DB 쓰기용 서버 전용 키 (절대 외부 노출 금지) |
| `FLASK_SECRET_KEY` | ✅ | dev-secret | Flask 세션 암호화 키 |
| `FLASK_DEBUG` | — | false | `true` 시 핫리로드 및 상세 에러 활성화 |
| `CACHE_TTL` | — | 300 | 캐시 유지 시간 (초, L1·L2 공통 적용) |

---

## Render 배포

### 1단계 — 서비스 계정 JSON base64 변환

Render는 파일을 직접 올릴 수 없으므로 JSON 내용을 환경변수로 전달합니다.

**PowerShell (Windows):**
```powershell
[Convert]::ToBase64String([System.IO.File]::ReadAllBytes('service-account.json'))
```

**Mac / Linux:**
```bash
base64 -w 0 service-account.json
```

출력된 긴 문자열을 복사해 둡니다.

### 2단계 — Render에 서비스 생성

1. [render.com](https://render.com) → **New → Web Service**
2. GitHub 저장소 연결 (이 프로젝트 폴더를 push한 레포 선택)
3. 아래 설정 확인 (또는 `render.yaml`이 자동 감지됨):

| 항목 | 값 |
|------|----|
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app --workers 2 --timeout 120` |

### 3단계 — 환경변수 설정

Render 대시보드 → **Environment** 탭에서 아래 변수를 입력합니다.

| 변수 | 값 |
|------|----|
| `GCP_PROJECT_ID` | GCP 프로젝트 ID |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | 1단계에서 복사한 base64 문자열 |
| `SUPABASE_URL` | `https://your-project-id.supabase.co` |
| `SUPABASE_ANON_KEY` | Supabase anon 키 |
| `SUPABASE_JWT_SECRET` | Supabase JWT 시크릿 |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service_role 키 |
| `FLASK_SECRET_KEY` | 랜덤 문자열 (Render가 자동 생성 가능) |
| `CACHE_TTL` | `300` |

> `FLASK_DEBUG`는 Render에서 설정하지 않아도 됩니다 (기본값 false).

### 4단계 — Supabase OAuth 리디렉션 URI 추가

Render가 부여한 도메인(`https://google-trends-kr.onrender.com` 등)을:
- **GCP 콘솔 → OAuth 2.0 클라이언트 → 승인된 JS 원본 + 리디렉션 URI**에 추가
- **Supabase 대시보드 → Authentication → URL Configuration → Redirect URLs**에 추가

### 5단계 — 배포 확인

```
https://your-service.onrender.com/health
```

`{"status": "ok", ...}` 응답이 오면 정상 배포입니다.

> **참고:** Render 무료 플랜은 15분 비활성 후 슬립 상태가 됩니다. 첫 요청 시 30초 내외의 콜드 스타트가 발생할 수 있습니다.

---

## BigQuery SQL

```sql
SELECT
    term,
    score,
    refresh_date
FROM
    `bigquery-public-data.google_trends.international_top_terms`
WHERE
    country_name = 'South Korea'
    AND refresh_date = (
        SELECT MAX(refresh_date)
        FROM `bigquery-public-data.google_trends.international_top_terms`
    )
ORDER BY
    score DESC
LIMIT 25;
```
