import psycopg2
from config import FISHERY_DB, FISHERY_TABLES


def get_fishery_conn():
    return psycopg2.connect(**FISHERY_DB)


def list_tables():
    """허용된 테이블 목록 + 설명"""
    conn = get_fishery_conn()
    cur = conn.cursor()
    result = []
    for table, info in FISHERY_TABLES.items():
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            cnt = cur.fetchone()[0]
        except:
            conn.rollback()
            cnt = 0
        result.append({
            'table_name': table,
            'description': info['description'],
            'row_count': cnt,
            'max_limit': info['max_limit'],
        })
    cur.close()
    conn.close()
    return result


def get_table_schema(table_name):
    """테이블 컬럼 정보"""
    if table_name not in FISHERY_TABLES:
        return None
    conn = get_fishery_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
    """, (table_name,))
    cols = [{'name': r[0], 'type': r[1], 'nullable': r[2]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return cols


def query_table(table_name, limit=100, offset=0, filters=None, sort_by=None, sort_order='asc', date_from=None, date_to=None):
    """범용 테이블 조회"""
    if table_name not in FISHERY_TABLES:
        return None

    max_limit = FISHERY_TABLES[table_name]['max_limit']
    limit = min(limit, max_limit)

    conn = get_fishery_conn()
    cur = conn.cursor()

    # 컬럼 목록
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position
    """, (table_name,))
    valid_cols = [r[0] for r in cur.fetchall()]

    # 쿼리 빌드
    sql = f"SELECT * FROM {table_name} WHERE 1=1"
    params = []

    # 날짜 범위 필터 (datetime 컬럼이 있는 경우)
    if date_from and 'datetime' in valid_cols:
        sql += " AND datetime >= %s"
        params.append(date_from)
    if date_to and 'datetime' in valid_cols:
        sql += " AND datetime <= %s"
        params.append(date_to + ' 23:59:59' if len(date_to) == 10 else date_to)

    if filters:
        for col, val in filters.items():
            if col in valid_cols:
                sql += f" AND {col}::text LIKE %s"
                params.append(f"%{val}%")

    if sort_by and sort_by in valid_cols:
        order = 'DESC' if sort_order.lower() == 'desc' else 'ASC'
        sql += f" ORDER BY {sort_by} {order}"

    sql += " LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cur.execute(sql, params)
    rows = cur.fetchall()

    # dict로 변환
    data = []
    for row in rows:
        d = {}
        for i, col in enumerate(valid_cols):
            val = row[i] if i < len(row) else None
            d[col] = str(val) if val is not None else None
        data.append(d)

    # 총 건수
    count_sql = f"SELECT COUNT(*) FROM {table_name} WHERE 1=1"
    count_params = []
    if date_from and 'datetime' in valid_cols:
        count_sql += " AND datetime >= %s"
        count_params.append(date_from)
    if date_to and 'datetime' in valid_cols:
        count_sql += " AND datetime <= %s"
        count_params.append(date_to + ' 23:59:59' if len(date_to) == 10 else date_to)
    if filters:
        for col, val in filters.items():
            if col in valid_cols:
                count_sql += f" AND {col}::text LIKE %s"
                count_params.append(f"%{val}%")
    cur.execute(count_sql, count_params)
    total = cur.fetchone()[0]

    cur.close()
    conn.close()
    return {'data': data, 'total': total, 'limit': limit, 'offset': offset}
