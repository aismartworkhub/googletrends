-- ============================================================
-- Supabase SQL Editor에서 이 파일을 실행하세요.
-- ============================================================

-- ── 1. trends_cache ─────────────────────────────────────────
-- BigQuery 결과를 영속 저장. 서버 재시작해도 캐시 유지됨.

CREATE TABLE IF NOT EXISTS public.trends_cache (
    id          BIGSERIAL    PRIMARY KEY,
    refresh_date DATE        NOT NULL,
    rank        INTEGER      NOT NULL,
    term        TEXT         NOT NULL,
    score       INTEGER      NOT NULL DEFAULT 0,
    fetched_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- refresh_date + rank 조합은 유일해야 함 (upsert 기준)
CREATE UNIQUE INDEX IF NOT EXISTS trends_cache_date_rank
    ON public.trends_cache (refresh_date, rank);

-- 최신 데이터 조회 성능 최적화
CREATE INDEX IF NOT EXISTS trends_cache_fetched_at
    ON public.trends_cache (fetched_at DESC);

-- RLS 활성화
ALTER TABLE public.trends_cache ENABLE ROW LEVEL SECURITY;

-- 인증된 사용자는 읽기만 가능 (쓰기는 service_role 키로만)
CREATE POLICY "authenticated can read trends"
    ON public.trends_cache
    FOR SELECT
    TO authenticated
    USING (true);


-- ── 2. access_logs ──────────────────────────────────────────
-- 사용자별 API 호출 기록. 서비스 모니터링 및 분석용.

CREATE TABLE IF NOT EXISTS public.access_logs (
    id          BIGSERIAL    PRIMARY KEY,
    user_id     UUID         REFERENCES auth.users(id) ON DELETE SET NULL,
    user_email  TEXT,
    endpoint    TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 사용자별 조회 성능 최적화
CREATE INDEX IF NOT EXISTS access_logs_user_id
    ON public.access_logs (user_id, created_at DESC);

-- RLS 활성화
ALTER TABLE public.access_logs ENABLE ROW LEVEL SECURITY;

-- 본인 로그만 조회 가능
CREATE POLICY "users can read own logs"
    ON public.access_logs
    FOR SELECT
    TO authenticated
    USING (auth.uid() = user_id);


-- ── 3. csv_uploads ──────────────────────────────────────────
-- 사용자가 업로드한 Google Trends CSV 메타정보.

CREATE TABLE IF NOT EXISTS public.csv_uploads (
    id          BIGSERIAL    PRIMARY KEY,
    user_id     UUID         REFERENCES auth.users(id) ON DELETE SET NULL,
    user_email  TEXT,
    keyword     TEXT         NOT NULL,
    filename    TEXT         NOT NULL,
    row_count   INTEGER      NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS csv_uploads_created_at
    ON public.csv_uploads (created_at DESC);

ALTER TABLE public.csv_uploads ENABLE ROW LEVEL SECURITY;

-- 모든 로그인 사용자가 읽기 가능 (공유)
CREATE POLICY "authenticated can read csv_uploads"
    ON public.csv_uploads
    FOR SELECT
    TO authenticated
    USING (true);


-- ── 4. csv_data ─────────────────────────────────────────────
-- CSV의 날짜별 검색 관심도 수치.

CREATE TABLE IF NOT EXISTS public.csv_data (
    id          BIGSERIAL    PRIMARY KEY,
    upload_id   BIGINT       REFERENCES public.csv_uploads(id) ON DELETE CASCADE NOT NULL,
    date        DATE         NOT NULL,
    value       INTEGER      NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS csv_data_upload_date
    ON public.csv_data (upload_id, date);

ALTER TABLE public.csv_data ENABLE ROW LEVEL SECURITY;

-- 모든 로그인 사용자가 읽기 가능 (공유)
CREATE POLICY "authenticated can read csv_data"
    ON public.csv_data
    FOR SELECT
    TO authenticated
    USING (true);


-- ── 5. 컬럼 추가 (이미 테이블이 있는 경우 실행) ───────────────
-- 두 CSV 형식(시계열 / 실시간 트렌드) 구분용 컬럼
ALTER TABLE public.csv_uploads ADD COLUMN IF NOT EXISTS chart_type TEXT NOT NULL DEFAULT 'line';
-- 실시간 트렌드 CSV의 키워드 이름 저장용 컬럼
ALTER TABLE public.csv_data    ADD COLUMN IF NOT EXISTS label TEXT;
