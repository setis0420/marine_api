import hashlib
import secrets
import jwt
from datetime import datetime, timedelta
from config import JWT_SECRET, JWT_ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS
from config import LOCAL_DB, DEFAULT_DAILY_REQUESTS, DEFAULT_DAILY_DATA_MB, DEFAULT_MONTHLY_REQUESTS
import psycopg2


def get_local_conn():
    return psycopg2.connect(
        host=LOCAL_DB['host'], dbname=LOCAL_DB['database'],
        user=LOCAL_DB['user'], password=LOCAL_DB['password'], port=LOCAL_DB['port']
    )


def init_db():
    """API 사용자 테이블 초기화"""
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_users (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            username VARCHAR(100) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            full_name VARCHAR(200),
            affiliation VARCHAR(200),
            role VARCHAR(20) DEFAULT 'user',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES api_users(id) ON DELETE CASCADE,
            key_value VARCHAR(64) UNIQUE NOT NULL,
            key_prefix VARCHAR(12) NOT NULL,
            name VARCHAR(100),
            is_active BOOLEAN DEFAULT TRUE,
            daily_request_limit INTEGER DEFAULT %s,
            daily_data_limit_mb INTEGER DEFAULT %s,
            monthly_request_limit INTEGER DEFAULT %s,
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP
        )
    """, (DEFAULT_DAILY_REQUESTS, DEFAULT_DAILY_DATA_MB, DEFAULT_MONTHLY_REQUESTS))
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_log (
            id BIGSERIAL PRIMARY KEY,
            api_key_id INTEGER REFERENCES api_keys(id),
            endpoint VARCHAR(200),
            status_code INTEGER,
            response_rows INTEGER DEFAULT 0,
            response_bytes BIGINT DEFAULT 0,
            request_ip VARCHAR(45),
            request_at TIMESTAMP DEFAULT NOW(),
            duration_ms INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_daily (
            id SERIAL PRIMARY KEY,
            api_key_id INTEGER REFERENCES api_keys(id),
            date DATE NOT NULL,
            request_count INTEGER DEFAULT 0,
            data_bytes BIGINT DEFAULT 0,
            UNIQUE(api_key_id, date)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_value ON api_keys(key_value)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_daily_key ON api_usage_daily(api_key_id, date)")
    conn.commit()
    cur.close()
    conn.close()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${pw_hash}"


def verify_password(password: str, hashed: str) -> bool:
    salt, stored_hash = hashed.split('$', 1)
    return hashlib.sha256((salt + password).encode()).hexdigest() == stored_hash


def create_access_token(user_id: int, username: str, role: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "username": username, "role": role, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def generate_api_key() -> tuple:
    """(raw_key, key_hash, key_prefix) 반환"""
    raw = "jnu_" + secrets.token_hex(24)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:12]
    return raw, key_hash, prefix


def register_user(email, username, password, full_name=None, affiliation=None):
    conn = get_local_conn()
    cur = conn.cursor()
    pw_hash = hash_password(password)
    try:
        cur.execute("""
            INSERT INTO api_users (email, username, password_hash, full_name, affiliation)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (email, username, pw_hash, full_name, affiliation))
        user_id = cur.fetchone()[0]
        conn.commit()

        # 기본 API Key 자동 생성
        raw, key_hash, prefix = generate_api_key()
        cur.execute("""
            INSERT INTO api_keys (user_id, key_value, key_prefix, name)
            VALUES (%s, %s, %s, %s)
        """, (user_id, key_hash, prefix, 'default'))
        conn.commit()

        return {"user_id": user_id, "api_key": raw}
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return None
    finally:
        cur.close()
        conn.close()


def login_user(username, password):
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash, role, is_active FROM api_users WHERE username=%s OR email=%s",
                (username, username))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user or not verify_password(password, user[2]):
        return None
    if not user[4]:
        return None

    token = create_access_token(user[0], user[1], user[3])
    return {"access_token": token, "token_type": "bearer", "user_id": user[0], "role": user[3]}


def validate_api_key(raw_key):
    """API Key 검증 + 할당량 체크"""
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    conn = get_local_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT k.id, k.user_id, k.is_active, k.daily_request_limit, k.daily_data_limit_mb,
               k.monthly_request_limit, k.expires_at, u.is_active as user_active, u.role
        FROM api_keys k JOIN api_users u ON k.user_id = u.id
        WHERE k.key_value = %s
    """, (key_hash,))
    key_info = cur.fetchone()

    if not key_info or not key_info[2] or not key_info[7]:
        cur.close()
        conn.close()
        return None

    if key_info[6] and key_info[6] < datetime.now():
        cur.close()
        conn.close()
        return None

    # 일일 사용량 체크
    today = datetime.now().date()
    cur.execute("SELECT request_count, data_bytes FROM api_usage_daily WHERE api_key_id=%s AND date=%s",
                (key_info[0], today))
    usage = cur.fetchone()

    daily_requests = usage[0] if usage else 0
    daily_bytes = usage[1] if usage else 0

    cur.close()
    conn.close()

    if key_info[8] != 'admin':  # admin은 제한 없음
        if daily_requests >= key_info[3]:
            return {"error": "daily_request_limit_exceeded"}
        if daily_bytes >= key_info[4] * 1024 * 1024:
            return {"error": "daily_data_limit_exceeded"}

    return {
        "key_id": key_info[0],
        "user_id": key_info[1],
        "role": key_info[8],
        "daily_requests": daily_requests,
        "daily_limit": key_info[3],
    }


def log_usage(key_id, endpoint, status_code, rows, bytes_size, ip, duration_ms):
    """사용량 기록 (백그라운드)"""
    try:
        conn = get_local_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO api_usage_log (api_key_id, endpoint, status_code, response_rows, response_bytes, request_ip, duration_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (key_id, endpoint, status_code, rows, bytes_size, ip, duration_ms))

        today = datetime.now().date()
        cur.execute("""
            INSERT INTO api_usage_daily (api_key_id, date, request_count, data_bytes)
            VALUES (%s, %s, 1, %s)
            ON CONFLICT (api_key_id, date)
            DO UPDATE SET request_count = api_usage_daily.request_count + 1,
                          data_bytes = api_usage_daily.data_bytes + %s
        """, (key_id, today, bytes_size, bytes_size))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f'[log_usage ERROR] {e}')
