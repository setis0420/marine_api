from fastapi import APIRouter, Query, HTTPException, Header
from services.ship_service import query_fishing_ships, query_fishing_ship_detail, query_cooperatives
from services.auth_service import validate_api_key
from config import LOCAL_DB
import psycopg2

router = APIRouter(prefix="/api/v1/ships", tags=["선박 정보"])


def check_api_key(x_api_key: str):
    result = validate_api_key(x_api_key)
    if not result:
        raise HTTPException(status_code=401, detail="유효하지 않은 API Key")
    if isinstance(result, dict) and 'error' in result:
        raise HTTPException(status_code=429, detail=result['error'])
    return result


def get_local_conn():
    return psycopg2.connect(
        host=LOCAL_DB['host'], dbname=LOCAL_DB['database'],
        user=LOCAL_DB['user'], password=LOCAL_DB['password'], port=LOCAL_DB['port']
    )


@router.get("/fishing", summary="어선 목록 조회")
def fishing_ships(
    mmsi: int = Query(None, description="MMSI 번호"),
    fishing_type: str = Query(None, description="어업종류 (예: 자망, 통발, 선망)"),
    limit: int = Query(100, le=1000),
    offset: int = Query(0),
    x_api_key: str = Header(...),
):
    """
    어선 정보를 조회합니다. **API Key 필요** (헤더: `X-Api-Key`)
    """
    check_api_key(x_api_key)
    conn = get_local_conn()
    try:
        data = query_fishing_ships(conn, mmsi, fishing_type, limit, offset)
        return {"count": len(data), "data": data}
    finally:
        conn.close()


@router.get("/fishing/{mmsi}", summary="어선 상세 정보")
def fishing_ship_detail(mmsi: int, x_api_key: str = Header(...)):
    """특정 MMSI의 어선 상세 정보를 반환합니다. **API Key 필요**"""
    check_api_key(x_api_key)
    conn = get_local_conn()
    try:
        data = query_fishing_ship_detail(conn, mmsi)
        if not data:
            raise HTTPException(status_code=404, detail=f"MMSI {mmsi} not found")
        return data
    finally:
        conn.close()


@router.get("/cooperatives", summary="수협 관할구역 조회")
def cooperatives(name: str = Query(None, description="수협명 검색 (부분 일치)"),
                 x_api_key: str = Header(...)):
    """수협 관할구역 정보. **API Key 필요**"""
    check_api_key(x_api_key)
    conn = get_local_conn()
    try:
        data = query_cooperatives(conn, name)
        return {"count": len(data), "data": data}
    finally:
        conn.close()
