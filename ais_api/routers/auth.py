from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from services.auth_service import register_user, login_user, decode_token, generate_api_key, get_local_conn
import hashlib

router = APIRouter(prefix="/api/v1/auth", tags=["인증"])


class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str
    full_name: str = None
    affiliation: str = None


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/register", summary="회원가입")
def register(req: RegisterRequest):
    import traceback
    """
    회원가입 후 API Key가 자동 발급됩니다.

    - **email**: 이메일
    - **username**: 사용자명 (로그인 ID)
    - **password**: 비밀번호
    - **affiliation**: 소속 (예: 제주대학교)

    반환된 **api_key**는 다시 볼 수 없으니 반드시 저장하세요!
    """
    try:
        result = register_user(req.email, req.username, req.password, req.full_name, req.affiliation)
        if not result:
            raise HTTPException(status_code=400, detail="이미 등록된 이메일 또는 사용자명입니다")
        return {"message": "회원가입 완료", **result}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/login", summary="로그인")
def login(req: LoginRequest):
    """
    로그인하여 JWT 토큰을 발급받습니다.

    - **username**: 사용자명 또는 이메일
    - **password**: 비밀번호
    """
    import traceback
    try:
        result = login_user(req.username, req.password)
        if not result:
            raise HTTPException(status_code=401, detail="잘못된 사용자명 또는 비밀번호")
        return result
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/regenerate-key", summary="API Key 재발급")
def regenerate_key(authorization: str = Header(...)):
    """
    기존 API Key를 비활성화하고 새 키를 발급합니다.

    새 키는 이 응답에서만 볼 수 있으니 반드시 저장하세요!
    """
    try:
        token = authorization.replace("Bearer ", "")
        payload = decode_token(token)
    except:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰")

    user_id = int(payload['sub'])
    conn = get_local_conn()
    cur = conn.cursor()

    # 기존 키 비활성화
    cur.execute("UPDATE api_keys SET is_active = false WHERE user_id = %s", (user_id,))

    # 새 키 발급
    raw, key_hash, prefix = generate_api_key()
    cur.execute("""
        INSERT INTO api_keys (user_id, key_value, key_prefix, name)
        VALUES (%s, %s, %s, %s)
    """, (user_id, key_hash, prefix, 'regenerated'))
    conn.commit()
    cur.close()
    conn.close()

    return {"message": "새 API Key가 발급되었습니다", "api_key": raw}


@router.get("/me", summary="내 정보")
def me(authorization: str = Header(...)):
    """JWT 토큰으로 내 정보를 확인합니다. (Bearer 토큰)"""
    try:
        token = authorization.replace("Bearer ", "")
        payload = decode_token(token)
    except:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰")

    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, email, username, full_name, affiliation, role, created_at FROM api_users WHERE id=%s",
                (int(payload['sub']),))
    user = cur.fetchone()

    cur.execute("SELECT id, key_prefix, name, daily_request_limit, daily_data_limit_mb, is_active FROM api_keys WHERE user_id=%s",
                (int(payload['sub']),))
    keys = [{'id': r[0], 'prefix': r[1], 'name': r[2], 'daily_requests': r[3], 'daily_data_mb': r[4], 'active': r[5]}
            for r in cur.fetchall()]

    cur.close()
    conn.close()

    if not user:
        raise HTTPException(status_code=404, detail="사용자 없음")

    return {
        'id': user[0], 'email': user[1], 'username': user[2],
        'full_name': user[3], 'affiliation': user[4], 'role': user[5],
        'created_at': str(user[6]), 'api_keys': keys,
    }
