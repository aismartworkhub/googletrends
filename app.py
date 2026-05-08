import os
import base64
import tempfile
import atexit
import time
import logging
from datetime import datetime, timezone
from functools import wraps
from io import StringIO

import pandas as pd

import jwt
from jwt import PyJWKClient
from flask import Flask, jsonify, request, render_template, g
from flask_cors import CORS
from google.cloud import bigquery
from google.api_core.exceptions import GoogleAPIError
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Render 배포 시 서비스 계정 JSON을 환경변수에서 복원
# GOOGLE_APPLICATION_CREDENTIALS_JSON = base64 인코딩된 JSON 내용
# ---------------------------------------------------------------------------

_tmp_credentials_file = None

_cred_json_b64 = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if _cred_json_b64 and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    _tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    _tmp.write(base64.b64decode(_cred_json_b64))
    _tmp.flush()
    _tmp.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _tmp.name
    _tmp_credentials_file = _tmp.name

    @atexit.register
    def _cleanup_tmp_credentials():
        try:
            os.unlink(_tmp_credentials_file)
        except OSError:
            pass

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GCP_PROJECT_ID          = os.getenv("GCP_PROJECT_ID")
SUPABASE_URL            = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY       = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_JWT_SECRET     = os.getenv("SUPABASE_JWT_SECRET")
SUPABASE_SERVICE_KEY    = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
CACHE_TTL               = int(os.getenv("CACHE_TTL", 300))

# JWKS 클라이언트 — Supabase 신규 비대칭키(RS256) 검증용
_jwks_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json", cache_keys=True)

# ---------------------------------------------------------------------------
# Supabase admin client (service_role — RLS 우회, 서버 전용)
# ---------------------------------------------------------------------------

def get_sb() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------------------------------------------------------------------------
# In-memory L1 캐시 (Supabase 앞단 속도 최적화)
# ---------------------------------------------------------------------------

_mem_cache: dict = {"data": None, "timestamp": 0.0}

BIGQUERY_SQL = """
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
        WHERE country_name = 'South Korea'
    )
QUALIFY ROW_NUMBER() OVER (PARTITION BY term ORDER BY score DESC) = 1
ORDER BY
    score DESC
LIMIT 25
"""

# ---------------------------------------------------------------------------
# 캐시 계층: 메모리 → Supabase → BigQuery
# ---------------------------------------------------------------------------

def fetch_trends() -> list[dict]:
    now = time.time()

    # L1: 인메모리 캐시
    if _mem_cache["data"] and (now - _mem_cache["timestamp"]) < CACHE_TTL:
        logger.info("L1 cache hit")
        return _mem_cache["data"]

    # L2: Supabase trends_cache 테이블
    try:
        sb = get_sb()
        result = (
            sb.table("trends_cache")
            .select("rank, term, score, refresh_date, fetched_at")
            .order("rank")
            .limit(25)
            .execute()
        )
        rows = result.data or []

        if rows:
            fetched_at_str = rows[0].get("fetched_at", "")
            fetched_at = datetime.fromisoformat(fetched_at_str.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - fetched_at).total_seconds()

            if age < CACHE_TTL:
                logger.info("L2 Supabase cache hit (age=%.0fs)", age)
                data = [
                    {
                        "rank": r["rank"],
                        "term": r["term"],
                        "score": r["score"],
                        "refresh_date": r["refresh_date"],
                    }
                    for r in rows
                ]
                _mem_cache["data"] = data
                _mem_cache["timestamp"] = now
                return data
    except Exception as exc:
        logger.warning("Supabase 캐시 조회 실패, BigQuery로 진행: %s", exc)

    # L3: BigQuery (최종 소스)
    logger.info("BigQuery 쿼리 실행 중…")
    client = bigquery.Client(project=GCP_PROJECT_ID)
    bq_rows = list(client.query(BIGQUERY_SQL).result())
    data = [
        {
            "rank": i + 1,
            "term": row.term,
            "score": int(row.score) if row.score is not None else 0,
            "refresh_date": str(row.refresh_date),
        }
        for i, row in enumerate(bq_rows)
    ]
    logger.info("BigQuery 완료 — %d건", len(data))

    # Supabase에 upsert (L2 캐시 갱신)
    try:
        if not data:
            raise ValueError("BigQuery 결과가 없습니다.")
        sb = get_sb()
        sb.table("trends_cache").upsert(
            data,
            on_conflict="refresh_date,rank",
        ).execute()
        logger.info("Supabase trends_cache upsert 완료")
    except Exception as exc:
        logger.warning("Supabase upsert 실패 (무시하고 계속): %s", exc)

    # L1 캐시 갱신
    _mem_cache["data"] = data
    _mem_cache["timestamp"] = now
    return data


def log_access(user_id: str, user_email: str, endpoint: str) -> None:
    """access_logs 테이블에 API 호출 기록 (실패해도 메인 응답에 영향 없음)."""
    try:
        get_sb().table("access_logs").insert(
            {"user_id": user_id, "user_email": user_email, "endpoint": endpoint}
        ).execute()
    except Exception as exc:
        logger.warning("access_logs 기록 실패: %s", exc)

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


def _decode_token(token: str) -> dict:
    """RS256(JWKS) → HS256(Legacy Secret) 순서로 JWT 검증 시도."""
    # 1차: Supabase 신규 비대칭키(RS256)
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience="authenticated",
        )
    except jwt.exceptions.PyJWKClientError:
        pass  # JWKS에 해당 키 없음 → Legacy 방식으로 재시도

    # 2차: Legacy HS256 시크릿
    if SUPABASE_JWT_SECRET:
        return jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )

    raise jwt.InvalidTokenError("검증 가능한 키가 없습니다.")


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Authorization 헤더가 없거나 형식이 잘못되었습니다."}), 401

        token = auth_header.split(" ", 1)[1]

        try:
            g.user = _decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "토큰이 만료되었습니다. 다시 로그인해주세요."}), 401
        except jwt.InvalidTokenError as exc:
            logger.warning("JWT 검증 실패: %s", exc)
            return jsonify({"error": "유효하지 않은 토큰입니다."}), 401

        return f(*args, **kwargs)

    return decorated

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template(
        "index.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "mem_cache_age_seconds": int(time.time() - _mem_cache["timestamp"]),
    })


@app.route("/api/trends")
@require_auth
def get_trends():
    try:
        data = fetch_trends()
        log_access(
            user_id=g.user.get("sub", ""),
            user_email=g.user.get("email", ""),
            endpoint="/api/trends",
        )
        return jsonify({
            "success": True,
            "count": len(data),
            "mem_cache_age_seconds": int(time.time() - _mem_cache["timestamp"]),
            "data": data,
        })
    except GoogleAPIError as exc:
        logger.error("BigQuery API 오류: %s", exc)
        return jsonify({"success": False, "error": "BigQuery 조회 중 오류가 발생했습니다."}), 502
    except Exception as exc:
        logger.exception("예상치 못한 오류: %s", exc)
        return jsonify({"success": False, "error": "서버 내부 오류"}), 500


@app.route("/api/cache/clear", methods=["POST"])
@require_auth
def clear_cache():
    """L1 인메모리 캐시 초기화 (Supabase 데이터는 유지)."""
    _mem_cache["data"] = None
    _mem_cache["timestamp"] = 0.0
    log_access(
        user_id=g.user.get("sub", ""),
        user_email=g.user.get("email", ""),
        endpoint="/api/cache/clear",
    )
    logger.info("캐시 초기화 — 요청자: %s", g.user.get("email", "unknown"))
    return jsonify({"success": True, "message": "캐시가 초기화되었습니다."})


# ---------------------------------------------------------------------------
# CSV 파싱
# ---------------------------------------------------------------------------

def _parse_korean_volume(s: str) -> int:
    """'1천+', '2만+', '500+' 등 한국어 검색량 표기를 정수로 변환."""
    import re
    s = re.sub(r"[+,\s]", "", str(s).strip())
    units = {"억": 100_000_000, "만": 10_000, "천": 1_000, "백": 100}
    for unit, mult in units.items():
        if unit in s:
            num = s.split(unit)[0]
            try:
                return int(float(num) * mult)
            except ValueError:
                return 0
    try:
        return int(s)
    except ValueError:
        return 0


def _parse_korean_date(s: str) -> str | None:
    """'2026년 5월 7일 오후 7시 10분 0초 UTC+9' → '2026-05-07'."""
    import re
    m = re.match(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", str(s).strip())
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def parse_trends_csv(content: str) -> tuple[str, str, list[dict]]:
    """Google Trends CSV를 파싱해 (키워드, chart_type, 데이터 리스트) 반환.

    chart_type:
      'line'  — 시간별 관심도 CSV (날짜 × 관심도 0-100)
      'bar'   — 실시간 트렌드 CSV (키워드 × 검색량)
    """
    import re

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    logger.info("CSV 원본 첫 6줄: %s", lines[:6])

    def clean(s: str) -> str:
        return s.strip().strip('"').strip("'").strip()

    if not lines:
        raise ValueError("빈 파일입니다.")

    first_data_line = next((clean(l) for l in lines if clean(l)), "")
    first_col = first_data_line.split(",")[0].lower()

    # ── 실시간 트렌드 형식 감지 ──────────────────────────────────
    TRENDING_HEADERS = ("트렌드", "trend", "검색어", "keyword")
    if any(first_col.startswith(h) for h in TRENDING_HEADERS):
        logger.info("실시간 트렌드 CSV 형식 감지")
        header_line = first_data_line
        # 헤더 다음 데이터 파싱
        data_lines = [l for l in lines[1:] if clean(l)]
        rows = []
        for line in data_lines:
            parts = [clean(p) for p in line.split(",")]
            if len(parts) < 2:
                continue
            label = parts[0]            # 트렌드 키워드명
            volume = _parse_korean_volume(parts[1]) if len(parts) > 1 else 0
            date_str = None
            if len(parts) > 2:
                date_str = _parse_korean_date(parts[2])
            rows.append({
                "label": label,
                "date": date_str or "1970-01-01",
                "value": volume,
            })
        if not rows:
            raise ValueError("유효한 데이터 행이 없습니다.")
        keyword = f"실시간 트렌드 ({rows[0]['date'] if rows else ''})"
        return keyword, "bar", rows

    # ── 시간별 관심도 형식 ────────────────────────────────────────
    TIMESERIES_HEADERS = ("날짜", "date", "week", "month", "시간", "time")
    header_idx = None
    keyword = "Unknown"

    for i, line in enumerate(lines):
        c = clean(line).lower()
        if any(c.startswith(kw) for kw in TIMESERIES_HEADERS):
            header_idx = i
            parts = line.split(",", 1)
            if len(parts) > 1:
                keyword = clean(parts[1]).split(":")[0].strip()
            break

    # 날짜 패턴 역추적 fallback
    if header_idx is None:
        date_re = re.compile(r'^"?\d{4}-\d{2}')
        for i, line in enumerate(lines):
            if date_re.match(line.strip()):
                for j in range(i - 1, -1, -1):
                    if clean(lines[j]):
                        header_idx = j
                        parts = lines[j].split(",", 1)
                        if len(parts) > 1:
                            keyword = clean(parts[1]).split(":")[0].strip()
                        break
                if header_idx is None:
                    header_idx = i
                break

    if header_idx is None:
        raise ValueError(
            "CSV 형식을 인식할 수 없습니다.\n"
            "• 시간별 관심도: trends.google.com에서 키워드 검색 후 '시간에 따른 관심도' 그래프의 ↓ 버튼으로 다운로드\n"
            "• 실시간 트렌드: trends.google.com/trending 에서 ↓ 버튼으로 다운로드"
        )

    logger.info("시계열 헤더 행 %d: %s  /  키워드: %s", header_idx, lines[header_idx], keyword)
    data_text = "\n".join(lines[header_idx:])
    df = pd.read_csv(StringIO(data_text), header=0)
    df.columns = ["date", "value"] + list(df.columns[2:])
    df = df[["date", "value"]].copy()
    df["date"] = df["date"].astype(str).str.split(r"\s").str[0].str.strip()
    df["date"] = df["date"].apply(lambda d: d + "-01" if re.match(r"^\d{4}-\d{2}$", d) else d)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["value"] = pd.to_numeric(df["value"], errors="coerce").fillna(0).astype(int)
    df = df.dropna(subset=["date"])

    rows = [{"label": None, "date": r["date"], "value": r["value"]} for r in df.to_dict(orient="records")]
    if not rows:
        raise ValueError("유효한 데이터 행이 없습니다.")

    return keyword, "line", rows


# ---------------------------------------------------------------------------
# CSV Routes
# ---------------------------------------------------------------------------

@app.route("/api/csv/upload", methods=["POST"])
@require_auth
def upload_csv():
    if "file" not in request.files:
        return jsonify({"error": "file 필드가 없습니다."}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "CSV 파일만 업로드 가능합니다."}), 400

    try:
        content = file.read().decode("utf-8-sig")
        keyword, chart_type, rows = parse_trends_csv(content)
    except Exception as exc:
        logger.warning("CSV 파싱 오류: %s", exc)
        return jsonify({"error": f"CSV 파싱 오류: {exc}"}), 400

    sb = get_sb()

    upload = sb.table("csv_uploads").insert({
        "user_id":    g.user.get("sub"),
        "user_email": g.user.get("email", ""),
        "keyword":    keyword,
        "filename":   file.filename,
        "row_count":  len(rows),
        "chart_type": chart_type,
    }).execute()

    upload_id = upload.data[0]["id"]

    batch = [{"upload_id": upload_id, "label": r.get("label"), "date": r["date"], "value": r["value"]} for r in rows]
    sb.table("csv_data").insert(batch).execute()

    log_access(g.user.get("sub", ""), g.user.get("email", ""), "/api/csv/upload")
    logger.info("CSV 업로드 완료 — keyword=%s rows=%d", keyword, len(rows))

    return jsonify({"success": True, "upload_id": upload_id, "keyword": keyword, "row_count": len(rows)})


@app.route("/api/csv/list")
@require_auth
def list_csvs():
    sb = get_sb()
    result = (
        sb.table("csv_uploads")
        .select("id, keyword, filename, row_count, user_email, created_at")
        .order("created_at", desc=True)
        .execute()
    )
    return jsonify({"success": True, "data": result.data})


@app.route("/api/csv/<int:upload_id>")
@require_auth
def get_csv_data(upload_id):
    sb = get_sb()

    meta = sb.table("csv_uploads").select("*").eq("id", upload_id).execute()
    if not meta.data:
        return jsonify({"error": "찾을 수 없습니다."}), 404

    data = (
        sb.table("csv_data")
        .select("date, value, label")
        .eq("upload_id", upload_id)
        .order("date")
        .execute()
    )

    return jsonify({"success": True, "meta": meta.data[0], "data": data.data})


@app.route("/api/csv/<int:upload_id>", methods=["DELETE"])
@require_auth
def delete_csv(upload_id):
    sb = get_sb()
    meta = sb.table("csv_uploads").select("user_id").eq("id", upload_id).execute()
    if not meta.data:
        return jsonify({"error": "찾을 수 없습니다."}), 404

    sb.table("csv_uploads").delete().eq("id", upload_id).execute()
    log_access(g.user.get("sub", ""), g.user.get("email", ""), f"/api/csv/{upload_id} DELETE")
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    port = int(os.getenv("PORT", 5000))
    app.run(debug=debug, host="0.0.0.0", port=port)
