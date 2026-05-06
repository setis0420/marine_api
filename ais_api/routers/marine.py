"""
Marine DB 허용 테이블 조회 라우터 (171 marine)
- fishery 라우터와 동일 패턴
- API Key 필요
"""
from fastapi import APIRouter, Query, HTTPException, Header, Request, BackgroundTasks
from services.auth_service import validate_api_key, log_usage
from config import LOCAL_DB, MARINE_TABLES
import psycopg2, time, json
from datetime import datetime, date
from decimal import Decimal
import math

router = APIRouter(prefix="/api/v1/marine", tags=["Marine 데이터 (171)"])


def check_api_key(x_api_key: str):
    result = validate_api_key(x_api_key)
    if not result:
        raise HTTPException(status_code=401, detail="유효하지 않은 API Key")
    if isinstance(result, dict) and 'error' in result:
        raise HTTPException(status_code=429, detail=result['error'])
    return result


def get_conn():
    return psycopg2.connect(
        host=LOCAL_DB['host'], dbname=LOCAL_DB['database'],
        user=LOCAL_DB['user'], password=LOCAL_DB['password'], port=LOCAL_DB['port']
    )


def to_json_safe(v):
    if v is None: return None
    if isinstance(v, (datetime, date)): return v.isoformat()
    if isinstance(v, Decimal):
        return float(v) if v.is_finite() else None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v): return None
        return v
    if isinstance(v, (bytes, bytearray)): return f'<binary {len(v)}>'
    if isinstance(v, (int, bool)): return v
    return str(v)


@router.get("/tables", summary="조회 가능한 marine 테이블 목록")
def tables(x_api_key: str = Header(...)):
    """marine DB 화이트리스트 테이블 + 행수"""
    check_api_key(x_api_key)
    conn = get_conn()
    cur = conn.cursor()
    out = []
    for name, info in MARINE_TABLES.items():
        try:
            if name in ('fishing_voyage', 'fishing_spatio_temporal'):
                # 큰 테이블은 reltuples 추정치
                cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname=%s", (name,))
                cnt = cur.fetchone()[0] or 0
            else:
                cur.execute(f"SELECT COUNT(*) FROM {name}")
                cnt = cur.fetchone()[0]
        except Exception:
            conn.rollback(); cnt = 0
        out.append({
            'table_name': name,
            'description': info['description'],
            'row_count': cnt,
            'max_limit': info['max_limit'],
        })
    cur.close(); conn.close()
    return {'count': len(out), 'tables': out}


@router.get("/tables/{table_name}/schema", summary="marine 테이블 스키마")
def schema(table_name: str, x_api_key: str = Header(...)):
    check_api_key(x_api_key)
    if table_name not in MARINE_TABLES:
        raise HTTPException(status_code=404, detail=f"허용 안 된 테이블: {table_name}. /api/v1/marine/tables 참조")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
    """, (table_name,))
    cols = [{'name': r[0], 'type': r[1], 'nullable': r[2] == 'YES'} for r in cur.fetchall()]
    cur.close(); conn.close()
    return {'table': table_name, 'description': MARINE_TABLES[table_name]['description'], 'columns': cols}


@router.get("/tables/{table_name}", summary="marine 테이블 데이터 조회")
def query_table(
    table_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(...),
    limit: int = Query(100, le=1000000),
    offset: int = Query(0),
    sort_by: str = Query(None),
    sort_order: str = Query('asc'),
    date_from: str = Query(None, description="날짜 필터 (datetime/landing_date 자동 적용)"),
    date_to: str = Query(None),
):
    """
    marine DB 테이블 조회 (화이트리스트만).

    동적 필터: 쿼리 파라미터로 컬럼명=값 (LIKE 부분일치)
    """
    t0 = time.time()
    key_info = check_api_key(x_api_key)

    if table_name not in MARINE_TABLES:
        raise HTTPException(status_code=404, detail=f"허용 안 된 테이블: {table_name}")

    max_limit = MARINE_TABLES[table_name]['max_limit']
    limit = min(limit, max_limit)

    conn = get_conn()
    cur = conn.cursor()

    # 컬럼 목록
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position""", (table_name,))
    valid_cols = [r[0] for r in cur.fetchall()]

    # 쿼리 빌드
    sql = f"SELECT * FROM {table_name} WHERE 1=1"
    params = []

    # 날짜 필터 (datetime, landing_date 등 자동 매칭)
    date_cols = [c for c in valid_cols if c in ('datetime', 'landing_date', 'start_time', 'end_time')]
    date_col = date_cols[0] if date_cols else None
    if date_from and date_col:
        sql += f" AND {date_col} >= %s"
        params.append(date_from)
    if date_to and date_col:
        sql += f" AND {date_col} <= %s"
        params.append(date_to + ' 23:59:59' if len(date_to) == 10 else date_to)

    # 동적 필터
    skip_params = {'x_api_key', 'limit', 'offset', 'sort_by', 'sort_order', 'date_from', 'date_to'}
    filters = {k: v for k, v in request.query_params.items() if k not in skip_params}
    for col, val in filters.items():
        if col in valid_cols:
            sql += f" AND {col}::text LIKE %s"
            params.append(f'%{val}%')

    if sort_by and sort_by in valid_cols:
        sql += f" ORDER BY {sort_by} {'DESC' if sort_order.lower()=='desc' else 'ASC'}"

    sql += " LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cur.execute(sql, params)
    rows = cur.fetchall()
    data = [{valid_cols[i]: to_json_safe(v) for i, v in enumerate(row)} for row in rows]

    # 총 건수 (대용량 테이블은 추정치)
    if table_name in ('fishing_voyage', 'fishing_spatio_temporal'):
        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname=%s", (table_name,))
        total = cur.fetchone()[0] or 0
    else:
        count_sql = f"SELECT COUNT(*) FROM {table_name} WHERE 1=1"
        count_params = []
        if date_from and date_col:
            count_sql += f" AND {date_col} >= %s"; count_params.append(date_from)
        if date_to and date_col:
            count_sql += f" AND {date_col} <= %s"; count_params.append(date_to + ' 23:59:59' if len(date_to) == 10 else date_to)
        for col, val in filters.items():
            if col in valid_cols:
                count_sql += f" AND {col}::text LIKE %s"; count_params.append(f'%{val}%')
        cur.execute(count_sql, count_params)
        total = cur.fetchone()[0]

    cur.close(); conn.close()

    duration_ms = int((time.time() - t0) * 1000)
    response_bytes = len(json.dumps(data, default=str).encode())
    background_tasks.add_task(log_usage, key_info['key_id'], f'/marine/{table_name}',
                               200, len(data), response_bytes, request.client.host, duration_ms)

    return {
        'table': table_name,
        'description': MARINE_TABLES[table_name]['description'],
        'total': total,
        'count': len(data),
        'limit': limit,
        'offset': offset,
        'data': data,
    }
