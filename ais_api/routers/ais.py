from fastapi import APIRouter, Query, HTTPException, Header, Request, BackgroundTasks
from services.ais_service import query_ais_track, query_ais_area, query_available_tables
from services.auth_service import validate_api_key, log_usage
from config import AIS_DB, MAX_ROWS, DEFAULT_LIMIT
import psycopg2, time, json

router = APIRouter(prefix="/api/v1/ais", tags=["AIS 항적 데이터"])


def get_ais_conn():
    return psycopg2.connect(**AIS_DB)


def check_api_key(x_api_key):
    result = validate_api_key(x_api_key)
    if not result:
        raise HTTPException(status_code=401, detail="유효하지 않은 API Key")
    if isinstance(result, dict) and 'error' in result:
        raise HTTPException(status_code=429, detail=result['error'])
    return result


@router.get("/track", summary="MMSI 기준 항적 조회")
def ais_track(
    request: Request,
    background_tasks: BackgroundTasks,
    mmsi: int = Query(..., description="선박 MMSI 번호"),
    start_date: str = Query(..., description="시작일 (YYYY-MM-DD)"),
    end_date: str = Query(..., description="종료일 (YYYY-MM-DD)"),
    limit: int = Query(DEFAULT_LIMIT, le=MAX_ROWS),
    x_api_key: str = Header(...),
):
    """
    특정 선박(MMSI)의 항적 데이터를 조회합니다.

    ## 데이터 기간
    - **2012년 1월 ~ 현재** (월별 테이블)
    - /api/v1/ais/tables 에서 사용 가능한 기간 확인 가능

    ## 반환 데이터
    - **lat, lon**: 위경도 (도 단위, 소수점)
    - **sog**: 대지속력 (knots)
    - **cog**: 대지침로 (0~360도)
    - **heading**: 선수방위
    - **rot**: 선회율
    - **nav_status**: 항해 상태 코드

    ## 예시
    ```
    GET /api/v1/ais/track?mmsi=440137010&start_date=2023-01-01&end_date=2023-01-31&limit=50000
    ```

    ## 주의사항
    - 기간이 길면 데이터가 많으므로 **1개월 단위**로 조회를 권장합니다.
    - 요청당 최대 1,000,000건
    - **API Key 필요** (헤더: `X-Api-Key`)
    """
    t0 = time.time()
    key_info = check_api_key(x_api_key)

    conn = get_ais_conn()
    try:
        data = query_ais_track(conn, mmsi, start_date, end_date, limit)
    finally:
        conn.close()

    duration_ms = int((time.time() - t0) * 1000)
    import json as _json
    response_bytes = len(_json.dumps(data, default=str).encode())
    background_tasks.add_task(log_usage, key_info['key_id'], '/ais/track', 200, len(data), response_bytes, request.client.host, duration_ms)
    return {"mmsi": mmsi, "count": len(data), "start_date": start_date, "end_date": end_date, "data": data}


@router.get("/area", summary="영역 기준 항적 조회")
def ais_area(
    request: Request,
    background_tasks: BackgroundTasks,
    lat_min: float = Query(...), lat_max: float = Query(...),
    lon_min: float = Query(...), lon_max: float = Query(...),
    start_date: str = Query(...), end_date: str = Query(...),
    mmsi: int = Query(None),
    limit: int = Query(DEFAULT_LIMIT, le=MAX_ROWS),
    x_api_key: str = Header(...),
):
    """영역 내 항적 데이터 조회. **API Key 필요**"""
    t0 = time.time()
    key_info = check_api_key(x_api_key)

    conn = get_ais_conn()
    try:
        data = query_ais_area(conn, lat_min, lat_max, lon_min, lon_max, start_date, end_date, mmsi, limit)
    finally:
        conn.close()

    duration_ms = int((time.time() - t0) * 1000)
    import json as _json
    response_bytes = len(_json.dumps(data, default=str).encode())
    background_tasks.add_task(log_usage, key_info['key_id'], '/ais/area', 200, len(data), response_bytes, request.client.host, duration_ms)
    return {"count": len(data), "area": {"lat": [lat_min, lat_max], "lon": [lon_min, lon_max]}, "data": data}


@router.get("/tables", summary="사용 가능한 데이터 기간")
def ais_tables(x_api_key: str = Header(...)):
    """월별 AIS 데이터 목록. **API Key 필요**"""
    check_api_key(x_api_key)
    conn = get_ais_conn()
    try:
        tables = query_available_tables(conn)
        periods = [f"{t[2:6]}-{t[6:8]}" for t in tables if len(t) == 8]
    finally:
        conn.close()
    return {"count": len(periods), "periods": periods}
