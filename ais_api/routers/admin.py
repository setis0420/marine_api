from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel
from services.auth_service import decode_token, get_local_conn, generate_api_key
import hashlib

router = APIRouter(prefix="/api/v1/admin", tags=["관리자"])


def require_admin(authorization: str):
    try:
        token = authorization.replace("Bearer ", "")
        payload = decode_token(token)
        if payload.get('role') != 'admin':
            raise HTTPException(status_code=403, detail="관리자 권한 필요")
        return payload
    except HTTPException:
        raise
    except:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰")


@router.get("/users", summary="사용자 목록")
def list_users(authorization: str = Header(...)):
    """전체 사용자 목록 (관리자 전용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.email, u.username, u.full_name, u.affiliation, u.role, u.is_active,
               COALESCE(u.ai_enabled, FALSE), u.ai_daily_limit, u.created_at,
               COALESCE(SUM(d.request_count), 0) as total_requests,
               COALESCE(SUM(d.data_bytes), 0) as total_bytes
        FROM api_users u
        LEFT JOIN api_keys k ON u.id = k.user_id
        LEFT JOIN api_usage_daily d ON k.id = d.api_key_id
        GROUP BY u.id ORDER BY u.id
    """)
    users = []
    for r in cur.fetchall():
        users.append({
            'id': r[0], 'email': r[1], 'username': r[2], 'full_name': r[3],
            'affiliation': r[4], 'role': r[5], 'is_active': r[6],
            'ai_enabled': r[7], 'ai_daily_limit': r[8],
            'created_at': str(r[9]),
            'total_requests': r[10], 'total_data_mb': round(r[11] / 1024 / 1024, 1),
        })
    cur.close()
    conn.close()
    return {'count': len(users), 'users': users}


@router.get("/analyst/usage", summary="사용자별 AI Analyst 사용량/비용")
def analyst_usage(authorization: str = Header(...)):
    """모든 사용자의 AI 호출 수와 토큰·비용 (관리자 전용)"""
    require_admin(authorization)
    try:
        from routers.analyst import compute_cost, DAILY_LIMIT
    except Exception:
        compute_cost = None
        DAILY_LIMIT = 10

    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.username, u.full_name, u.affiliation, u.ai_enabled,
               COUNT(l.id) AS total_calls,
               COUNT(l.id) FILTER (WHERE l.created_at::date = CURRENT_DATE) AS today_calls,
               COALESCE(SUM(l.input_tokens), 0) AS inp,
               COALESCE(SUM(l.cache_creation_tokens), 0) AS cw,
               COALESCE(SUM(l.cache_read_tokens), 0) AS cr,
               COALESCE(SUM(l.output_tokens), 0) AS outp,
               MAX(l.created_at) AS last_at
        FROM api_users u
        LEFT JOIN api_analyst_log l ON u.id = l.user_id
        GROUP BY u.id, u.username, u.full_name, u.affiliation, u.ai_enabled
        ORDER BY total_calls DESC, u.id
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()

    users = []
    total_cost_usd = 0.0
    total_cost_krw = 0.0
    for r in rows:
        cost = compute_cost(r[7], r[8], r[9], r[10]) if compute_cost else {'usd': 0, 'krw': 0}
        total_cost_usd += cost['usd']
        total_cost_krw += cost['krw']
        users.append({
            'user_id': r[0], 'username': r[1], 'full_name': r[2], 'affiliation': r[3],
            'ai_enabled': r[4],
            'total_calls': r[5], 'today_calls': r[6], 'daily_limit': DAILY_LIMIT,
            'input_tokens': r[7], 'cache_creation_tokens': r[8],
            'cache_read_tokens': r[9], 'output_tokens': r[10],
            'cost_usd': cost['usd'], 'cost_krw': cost['krw'],
            'last_at': str(r[11]) if r[11] else None,
        })
    return {
        'daily_limit': DAILY_LIMIT,
        'total_cost_usd': round(total_cost_usd, 4),
        'total_cost_krw': round(total_cost_krw, 2),
        'users': users,
    }


@router.patch("/users/{user_id}/ai", summary="AI 권한 토글")
def toggle_ai(user_id: int, enabled: bool, authorization: str = Header(...)):
    """사용자의 AI Analyst 사용 권한을 변경합니다 (관리자 전용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("UPDATE api_users SET ai_enabled=%s WHERE id=%s", (enabled, user_id))
    conn.commit()
    cur.close(); conn.close()
    return {"message": f"사용자 {user_id} AI 권한 → {enabled}"}


@router.patch("/users/{user_id}/ai-limit", summary="AI 일일 한도 설정")
def set_ai_limit(user_id: int, limit: int = Query(..., ge=0, le=100000),
                 authorization: str = Header(...)):
    """사용자의 AI Analyst 하루 호출 한도를 지정합니다. 0=사용 불가 (관리자 전용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("UPDATE api_users SET ai_daily_limit=%s WHERE id=%s", (limit, user_id))
    conn.commit()
    cur.close(); conn.close()
    return {"message": f"사용자 {user_id} AI 하루 한도 → {limit}"}


@router.post("/users/{user_id}/ai-limit-reset", summary="AI 한도 기본값 복귀")
def reset_ai_limit(user_id: int, authorization: str = Header(...)):
    """사용자별 AI 한도를 제거 (글로벌 기본값 사용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("UPDATE api_users SET ai_daily_limit=NULL WHERE id=%s", (user_id,))
    conn.commit()
    cur.close(); conn.close()
    return {"message": f"사용자 {user_id} AI 한도 기본값 복귀"}


class QuotaUpdate(BaseModel):
    daily_request_limit: int = None
    daily_data_limit_mb: int = None
    monthly_request_limit: int = None
    is_active: bool = None


@router.patch("/users/{user_id}/quota", summary="사용자 할당량 설정")
def update_quota(user_id: int, quota: QuotaUpdate, authorization: str = Header(...)):
    """사용자의 API Key 할당량을 변경합니다 (관리자 전용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()

    updates = []
    params = []
    if quota.daily_request_limit is not None:
        updates.append("daily_request_limit = %s")
        params.append(quota.daily_request_limit)
    if quota.daily_data_limit_mb is not None:
        updates.append("daily_data_limit_mb = %s")
        params.append(quota.daily_data_limit_mb)
    if quota.monthly_request_limit is not None:
        updates.append("monthly_request_limit = %s")
        params.append(quota.monthly_request_limit)
    if quota.is_active is not None:
        updates.append("is_active = %s")
        params.append(quota.is_active)

    if updates:
        params.append(user_id)
        cur.execute(f"UPDATE api_keys SET {', '.join(updates)} WHERE user_id = %s", params)
        conn.commit()

    cur.close()
    conn.close()
    return {"message": f"사용자 {user_id} 할당량 업데이트 완료"}


@router.patch("/users/{user_id}/role", summary="사용자 역할 변경")
def update_role(user_id: int, role: str = Query(..., description="user 또는 admin"),
                authorization: str = Header(...)):
    """사용자 역할을 변경합니다 (관리자 전용)"""
    require_admin(authorization)
    if role not in ('user', 'admin'):
        raise HTTPException(status_code=400, detail="role은 'user' 또는 'admin'")
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("UPDATE api_users SET role=%s WHERE id=%s", (role, user_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": f"사용자 {user_id} 역할 → {role}"}


@router.get("/users/{user_id}/keys", summary="사용자 API Key 조회")
def user_keys(user_id: int, authorization: str = Header(...)):
    """사용자의 API Key 목록과 할당량 (관리자 전용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, key_prefix, name, is_active, daily_request_limit, daily_data_limit_mb, monthly_request_limit, created_at
        FROM api_keys WHERE user_id = %s ORDER BY id
    """, (user_id,))
    keys = [{
        'id': r[0], 'prefix': r[1], 'name': r[2], 'is_active': r[3],
        'daily_request_limit': r[4], 'daily_data_limit_mb': r[5],
        'monthly_request_limit': r[6], 'created_at': str(r[7]),
    } for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {'user_id': user_id, 'keys': keys}


@router.post("/users/{user_id}/reset-key", summary="사용자 API Key 재발급")
def reset_key(user_id: int, authorization: str = Header(...)):
    """사용자의 기존 키를 비활성화하고 새 키 발급 (관리자 전용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()

    # 기존 키 비활성화
    cur.execute("UPDATE api_keys SET is_active = false WHERE user_id = %s", (user_id,))

    # 새 키 발급
    raw, key_hash, prefix = generate_api_key()
    cur.execute("""
        INSERT INTO api_keys (user_id, key_value, key_prefix, name)
        VALUES (%s, %s, %s, %s)
    """, (user_id, key_hash, prefix, 'admin_reset'))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": f"사용자 {user_id} 새 키 발급 완료", "api_key": raw}


@router.get("/users/{user_id}/logs", summary="사용자 접근 로그")
def user_logs(user_id: int, limit: int = Query(100, le=1000),
              offset: int = Query(0),
              authorization: str = Header(...)):
    """사용자의 최근 API 호출 로그 (관리자 전용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT l.id, l.endpoint, l.status_code, l.response_rows, l.response_bytes,
               l.request_ip, l.request_at, l.duration_ms, k.key_prefix
        FROM api_usage_log l
        JOIN api_keys k ON l.api_key_id = k.id
        WHERE k.user_id = %s
        ORDER BY l.id DESC
        LIMIT %s OFFSET %s
    """, (user_id, limit, offset))
    logs = [{
        'id': r[0], 'endpoint': r[1], 'status_code': r[2],
        'rows': r[3], 'bytes': r[4], 'ip': r[5],
        'request_at': str(r[6]), 'duration_ms': r[7],
        'key_prefix': r[8],
    } for r in cur.fetchall()]
    cur.execute("""
        SELECT COUNT(*) FROM api_usage_log l
        JOIN api_keys k ON l.api_key_id = k.id WHERE k.user_id = %s
    """, (user_id,))
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    return {'user_id': user_id, 'total': total, 'count': len(logs), 'logs': logs}


@router.get("/logs", summary="전체 최근 접근 로그")
def all_logs(limit: int = Query(200, le=1000),
             offset: int = Query(0),
             endpoint: str = Query(None, description="엔드포인트 필터 (부분일치)"),
             status_code: int = Query(None),
             authorization: str = Header(...)):
    """전체 사용자 최근 API 호출 로그 (관리자 전용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    where = ""
    params = []
    if endpoint:
        where += " AND l.endpoint LIKE %s"
        params.append(f"%{endpoint}%")
    if status_code is not None:
        where += " AND l.status_code = %s"
        params.append(status_code)
    cur.execute(f"""
        SELECT l.id, u.id, u.username, l.endpoint, l.status_code,
               l.response_rows, l.response_bytes, l.request_ip,
               l.request_at, l.duration_ms, k.key_prefix
        FROM api_usage_log l
        JOIN api_keys k ON l.api_key_id = k.id
        JOIN api_users u ON k.user_id = u.id
        WHERE 1=1 {where}
        ORDER BY l.id DESC
        LIMIT %s OFFSET %s
    """, params + [limit, offset])
    logs = [{
        'id': r[0], 'user_id': r[1], 'username': r[2],
        'endpoint': r[3], 'status_code': r[4],
        'rows': r[5], 'bytes': r[6], 'ip': r[7],
        'request_at': str(r[8]), 'duration_ms': r[9],
        'key_prefix': r[10],
    } for r in cur.fetchall()]
    cur.execute(f"""
        SELECT COUNT(*) FROM api_usage_log l
        JOIN api_keys k ON l.api_key_id = k.id
        JOIN api_users u ON k.user_id = u.id
        WHERE 1=1 {where}
    """, params)
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    return {'total': total, 'count': len(logs), 'logs': logs}


@router.get("/users/{user_id}/usage", summary="사용자 사용량 조회")
def user_usage(user_id: int, days: int = Query(30, description="최근 N일"),
               authorization: str = Header(...)):
    """사용자의 최근 사용량 (일별 + 엔드포인트별 + 총합)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()

    # 1) 총합
    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(response_rows), 0), COALESCE(SUM(response_bytes), 0),
               MIN(request_at), MAX(request_at)
        FROM api_usage_log l
        JOIN api_keys k ON l.api_key_id = k.id
        WHERE k.user_id = %s
    """, (user_id,))
    r = cur.fetchone()
    summary = {
        'total_requests': r[0], 'total_rows': r[1], 'total_bytes': r[2],
        'total_mb': round(r[2] / 1024 / 1024, 2) if r[2] else 0,
        'first_request': str(r[3]) if r[3] else None,
        'last_request': str(r[4]) if r[4] else None,
    }

    # 2) 일별
    cur.execute("""
        SELECT d.date, SUM(d.request_count), SUM(d.data_bytes)
        FROM api_usage_daily d
        JOIN api_keys k ON d.api_key_id = k.id
        WHERE k.user_id = %s AND d.date >= CURRENT_DATE - %s
        GROUP BY d.date ORDER BY d.date DESC
    """, (user_id, days))
    daily = [{'date': str(r[0]), 'requests': r[1],
              'bytes': r[2], 'data_mb': round(r[2] / 1024 / 1024, 2)}
             for r in cur.fetchall()]

    # 3) 엔드포인트별
    cur.execute("""
        SELECT l.endpoint, COUNT(*), SUM(l.response_rows), SUM(l.response_bytes),
               ROUND(AVG(l.duration_ms)::numeric, 0)
        FROM api_usage_log l
        JOIN api_keys k ON l.api_key_id = k.id
        WHERE k.user_id = %s
        GROUP BY l.endpoint ORDER BY SUM(l.response_bytes) DESC NULLS LAST
    """, (user_id,))
    by_endpoint = [{'endpoint': r[0], 'requests': r[1], 'rows': r[2],
                    'bytes': r[3], 'data_mb': round((r[3] or 0) / 1024 / 1024, 2),
                    'avg_duration_ms': int(r[4]) if r[4] else 0}
                   for r in cur.fetchall()]

    cur.close()
    conn.close()
    return {'user_id': user_id, 'days': days,
            'summary': summary, 'daily': daily, 'by_endpoint': by_endpoint}


@router.get("/usage/summary", summary="전체 사용자 사용량 요약")
def usage_summary(authorization: str = Header(...)):
    """사용자별 총 요청/데이터량 요약 (관리자 전용)"""
    require_admin(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.username, u.full_name, u.affiliation,
               COUNT(l.id) as requests,
               COALESCE(SUM(l.response_rows), 0) as rows,
               COALESCE(SUM(l.response_bytes), 0) as bytes,
               MAX(l.request_at) as last_at
        FROM api_users u
        LEFT JOIN api_keys k ON u.id = k.user_id
        LEFT JOIN api_usage_log l ON l.api_key_id = k.id
        GROUP BY u.id, u.username, u.full_name, u.affiliation
        ORDER BY bytes DESC, requests DESC
    """)
    result = [{
        'user_id': r[0], 'username': r[1], 'full_name': r[2], 'affiliation': r[3],
        'requests': r[4], 'rows': r[5], 'bytes': r[6],
        'data_mb': round(r[6] / 1024 / 1024, 2) if r[6] else 0,
        'last_at': str(r[7]) if r[7] else None,
    } for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {'users': result}
