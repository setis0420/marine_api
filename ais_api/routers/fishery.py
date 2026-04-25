from fastapi import APIRouter, Query, HTTPException, Header, Request, BackgroundTasks
from services.fishery_service import list_tables, get_table_schema, query_table
from services.auth_service import validate_api_key, log_usage
from config import FISHERY_TABLES
import time, json

router = APIRouter(prefix="/api/v1/fishery", tags=["수산 데이터"])


def check_api_key(x_api_key: str):
    result = validate_api_key(x_api_key)
    if not result:
        raise HTTPException(status_code=401, detail="유효하지 않은 API Key")
    if isinstance(result, dict) and 'error' in result:
        raise HTTPException(status_code=429, detail=result['error'])
    return result


@router.get("/tables", summary="사용 가능한 테이블 목록")
def tables(x_api_key: str = Header(...)):
    """
    조회 가능한 수산 데이터 테이블 목록을 반환합니다.

    **API Key 필요** (헤더: `X-Api-Key`)
    """
    check_api_key(x_api_key)
    data = list_tables()
    return {"count": len(data), "tables": data}


@router.get("/tables/{table_name}/schema", summary="테이블 스키마")
def table_schema(table_name: str, x_api_key: str = Header(...)):
    """테이블의 컬럼 이름, 타입 정보를 반환합니다."""
    check_api_key(x_api_key)
    cols = get_table_schema(table_name)
    if cols is None:
        raise HTTPException(status_code=404, detail=f"테이블 '{table_name}' 없음")
    return {"table": table_name, "columns": cols}


@router.get("/tables/{table_name}", summary="테이블 데이터 조회")
def table_data(
    table_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(...),
    limit: int = Query(100, le=1000000, description="최대 반환 건수 (max 1,000,000)"),
    offset: int = Query(0, description="건너뛸 건수 (페이지네이션)"),
    sort_by: str = Query(None, description="정렬 컬럼명 (예: datetime)"),
    sort_order: str = Query('asc', description="정렬 순서: asc(오름차순) 또는 desc(내림차순)"),
    date_from: str = Query(None, description="날짜 시작 (YYYY-MM-DD). datetime 컬럼 기준 필터"),
    date_to: str = Query(None, description="날짜 종료 (YYYY-MM-DD). datetime 컬럼 기준 필터"),
):
    """
    수산 데이터 테이블을 조회합니다.

    ## 사용법

    - **페이지네이션**: `limit`과 `offset`으로 원하는 범위를 지정하세요.
    - **날짜 필터**: `date_from`, `date_to`로 기간을 지정하세요 (datetime 컬럼이 있는 테이블만).
    - **컬럼 필터**: 쿼리 파라미터로 컬럼명=값 형태로 필터 가능 (LIKE 검색, 부분일치).
    - **정렬**: `sort_by`와 `sort_order`로 정렬 가능.

    ## 주요 테이블 기간

    | 테이블 | 기간 | 비고 |
    |--------|------|------|
    | fish_landing_suhyub | 2013~2024 | 수협 위판 데이터 (1억건, 반드시 날짜 필터 사용) |
    | fish_cpue | 다양 | CPUE 데이터 |
    | fishing_effort | 다양 | 어획노력량 |
    | fishing_tac | 다양 | TAC(총허용어획량) |

    ## 예시

    ```
    # 2024년 위판 데이터 조회
    GET /api/v1/fishery/tables/fish_landing_suhyub?date_from=2024-01-01&date_to=2024-12-31&limit=10000

    # 꽃게 위판만 조회
    GET /api/v1/fishery/tables/fish_landing_suhyub?fish_name=꽃게&date_from=2024-01-01&date_to=2024-06-30&limit=5000

    # 자망어업 어선 목록
    GET /api/v1/fishery/tables/fishing_shipinfo?fishing_type=자망&limit=1000
    ```

    ## 주의사항
    - **대용량 테이블(fish_landing_suhyub 등)은 반드시 날짜 필터를 사용하세요.** 필터 없이 조회하면 가장 오래된 데이터부터 반환됩니다.
    - 요청당 최대 1,000,000건까지 조회 가능합니다.
    - **API Key 필요** (헤더: `X-Api-Key`)
    """
    t0 = time.time()
    key_info = check_api_key(x_api_key)

    if table_name not in FISHERY_TABLES:
        raise HTTPException(status_code=404, detail=f"테이블 '{table_name}' 없음. /api/v1/fishery/tables 참조")

    # 동적 필터 추출 (알려진 파라미터 제외)
    skip_params = {'x_api_key', 'limit', 'offset', 'sort_by', 'sort_order', 'date_from', 'date_to'}
    filters = {k: v for k, v in request.query_params.items() if k not in skip_params}

    result = query_table(table_name, limit, offset, filters or None, sort_by, sort_order, date_from, date_to)
    if result is None:
        raise HTTPException(status_code=404, detail=f"테이블 '{table_name}' 없음")

    duration_ms = int((time.time() - t0) * 1000)
    response_bytes = len(json.dumps(result['data']).encode())

    # 사용량 로깅 (백그라운드)
    background_tasks.add_task(
        log_usage, key_info['key_id'], f'/fishery/{table_name}',
        200, len(result['data']), response_bytes, request.client.host, duration_ms
    )

    return {
        "table": table_name,
        "description": FISHERY_TABLES[table_name]['description'],
        "total": result['total'],
        "count": len(result['data']),
        "limit": result['limit'],
        "offset": result['offset'],
        "data": result['data'],
    }
