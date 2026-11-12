"""main.py - 제주 미세먼지 백엔드 (FastAPI)

공공데이터 API -1.받기-> FastAPI -2.가공-3.예측-> 4.Supabase 저장
Vercel(React) -- 결과만 요청 --> FastAPI   (키는 전부 서버 안에만)

환경변수 (로컬은 .env, Railway는 Variables)
  SUPABASE_URL, SUPABASE_SECRET_KEY   Supabase 연결 (없으면 메모리 임시 저장)
  DATA_GO_KR_KEY                      공공데이터포털 인증키 (없으면 연습 데이터)
  ADMIN_TOKEN                         /collect 를 아무나 못 누르게 하는 암호
  FRONT_ORIGINS                       프론트 주소 (쉼표로 여러 개)
  SIDO_NAME=제주  COLLECT_MINUTES=60  AUTO_COLLECT=1  DEMO_MODE=0
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()  # 로컬에선 .env 읽기, Railway에선 Variables가 이미 들어 있음

import collect  # noqa: E402
import db  # noqa: E402
import predict  # noqa: E402
import stations  # noqa: E402  (측정소 위치표)

log = logging.getLogger("uvicorn.error")

DATA_KEY = os.getenv("DATA_GO_KR_KEY", "").strip()
SIDO = os.getenv("SIDO_NAME", "제주").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DEMO = os.getenv("DEMO_MODE", "0") == "1" or not DATA_KEY
SOURCE = "demo" if DEMO else "airkorea"
AUTO = os.getenv("AUTO_COLLECT", "1") == "1"
MINUTES = int(os.getenv("COLLECT_MINUTES", "60"))
FRONT_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONT_ORIGINS", "").split(",") if o.strip()]

store = db.get_store()


def run_collect(hours: int = 1) -> dict:
    """수집 한 번 = 받기 -> 가공 -> 저장 -> 기록"""
    items = collect.fake_raw(hours) if DEMO else collect.fetch_raw(SIDO, DATA_KEY)
    rows, stats = collect.clean(items, SIDO, SOURCE)
    saved = store.save_readings(rows)
    entry = {"ran_at": datetime.now(timezone.utc).isoformat(), "source": SOURCE,
             **stats, "saved": saved}
    store.save_log(entry)
    log.info("collect %s", entry)
    return entry


async def auto_loop():
    """서버가 켜져 있는 동안 COLLECT_MINUTES마다 자동 수집"""
    try:
        if DEMO and not store.recent(SOURCE, 1):
            await asyncio.to_thread(run_collect, 72)  # 예측을 바로 볼 수 있게 72시간치
    except Exception:
        log.exception("초기 연습 데이터 생성 실패")
    while True:
        try:
            await asyncio.to_thread(run_collect)
        except Exception:
            log.exception("자동 수집 실패")
        await asyncio.sleep(MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(auto_loop()) if AUTO else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="제주 미세먼지 백엔드", lifespan=lifespan)
# Vercel은 배포마다 jnu-vibe-day3-frontend-xxxx-...vercel.app 같은 개별 주소도 만들므로 함께 허용
FRONT_ORIGIN_REGEX = os.getenv("FRONT_ORIGIN_REGEX", r"https://jnu-vibe-day3-frontend[a-z0-9-]*\.vercel\.app")
app.add_middleware(CORSMiddleware, allow_origins=FRONT_ORIGINS, allow_origin_regex=FRONT_ORIGIN_REGEX,
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def home():
    return {"service": "제주 미세먼지 백엔드", "mode": SOURCE, "docs": "/docs"}


@app.get("/health")
def health():
    missing = [k for k in ["SUPABASE_URL", "SUPABASE_SECRET_KEY", "DATA_GO_KR_KEY",
                           "ADMIN_TOKEN", "FRONT_ORIGINS"] if not os.getenv(k, "").strip()]
    return {"ok": True, "mode": SOURCE, "store": store.kind,
            "auto_collect_minutes": MINUTES if AUTO else None, "missing": missing}


@app.post("/collect")
def collect_now(x_admin_token: str = Header(default=""),
                hours: int = Query(1, ge=1, le=168, description="연습 모드에서만: 몇 시간치 만들지")):
    """지금 바로 한 번 수집 (/docs 에서 Try it out)"""
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN 을 먼저 설정하세요")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "x-admin-token 이 틀렸습니다")
    try:
        return run_collect(hours if DEMO else 1)
    except Exception as e:
        raise HTTPException(502, str(e))


def _latest_rows():
    rows = store.recent(SOURCE)
    seen, latest = set(), []
    for r in rows:  # 시각 내림차순 -> 측정소별 첫 행이 최신
        if r["station"] not in seen:
            seen.add(r["station"])
            latest.append(r)
    return latest


@app.get("/air/latest")
def air_latest():
    rows = _latest_rows()
    rows.sort(key=lambda r: r["pm10"] or 0, reverse=True)
    result = []
    for r in rows:
        pos = stations.coord(r["station"])  # (위도, 경도) 또는 None
        result.append({"station": r["station"], "time": db.kst(r["data_time"]),
                       "pm10": r["pm10"], "pm10_grade": r["pm10_grade"],
                       "pm25": r["pm25"], "pm25_grade": r["pm25_grade"],
                       "lat": pos[0] if pos else None,   # 좌표가 없으면 null
                       "lng": pos[1] if pos else None})
    return result


@app.get("/air/summary")
def air_summary():
    rows = _latest_rows()
    pm10s = [r["pm10"] for r in rows if r["pm10"] is not None]
    pm25s = [r["pm25"] for r in rows if r["pm25"] is not None]
    worst = max(rows, key=lambda r: r["pm10"] or 0) if rows else None
    logs = store.logs(1)
    avg10 = round(sum(pm10s) / len(pm10s)) if pm10s else None
    return {
        "mode": SOURCE,
        "stations": len(rows),
        "avg_pm10": avg10,
        "avg_pm25": round(sum(pm25s) / len(pm25s)) if pm25s else None,
        "avg_pm10_grade": collect.grade_pm10(avg10) if avg10 is not None else None,
        "worst_station": worst and {"station": worst["station"], "pm10": worst["pm10"]},
        "last_collect": logs[0] if logs else None,
    }


@app.get("/air/predict")
def air_predict(station: str):
    hist = store.history(SOURCE, station)
    if not hist:
        raise HTTPException(404, f"'{station}' 측정소 데이터가 없습니다")
    return {"station": station, **predict.predict_next(hist)}


@app.get("/db/status")
def db_status():
    """DB에 지금까지 얼마나 쌓였는지"""
    st = store.stats(SOURCE)
    return {"store": store.kind, "mode": SOURCE, "rows": st["rows"],
            "first": st["first"] and db.kst(st["first"]),
            "last": st["last"] and db.kst(st["last"])}


@app.get("/air/history")
def air_history(station: str, hours: int = Query(48, ge=1, le=168)):
    """측정소의 시간대별 기록 (오래된 순)"""
    hist = store.history(SOURCE, station, limit=hours)
    return [{"time": db.kst(h["data_time"]), "pm10": h["pm10"], "pm25": h["pm25"]} for h in hist]


@app.get("/air/compare")
def air_compare(station: str):
    """가장 최근 값 vs 어제 같은 시간 값"""
    hist = store.history(SOURCE, station, limit=72)
    if not hist:
        raise HTTPException(404, f"'{station}' 측정소 데이터가 없습니다")
    last = hist[-1]
    t = datetime.fromisoformat(last["data_time"])
    yest = next((h for h in hist
                 if abs((datetime.fromisoformat(h["data_time"]) - (t - timedelta(days=1))).total_seconds()) < 1800),
                None)
    diff = (last["pm10"] - yest["pm10"]) if yest and last["pm10"] is not None and yest["pm10"] is not None else None
    return {"station": station,
            "now": {"time": db.kst(last["data_time"]), "pm10": last["pm10"]},
            "yesterday": yest and {"time": db.kst(yest["data_time"]), "pm10": yest["pm10"]},
            "diff_pm10": diff}


@app.get("/logs")
def logs():
    return store.logs()
