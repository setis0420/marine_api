from datetime import date, datetime
import psycopg2


def get_tables_for_range(start_date: date, end_date: date):
    """날짜 범위에 해당하는 월별 테이블명 리스트"""
    tables = []
    current = start_date.replace(day=1)
    while current <= end_date:
        tables.append(f"db{current.strftime('%Y%m')}")
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return tables


def query_ais_track(conn, mmsi: int, start_date: str, end_date: str, limit: int = 10000):
    """MMSI 기준 항적 조회"""
    sd = datetime.strptime(start_date, '%Y-%m-%d').date()
    ed = datetime.strptime(end_date, '%Y-%m-%d').date()
    tables = get_tables_for_range(sd, ed)

    all_rows = []
    cur = conn.cursor()
    for table in tables:
        try:
            cur.execute(f"""
                SELECT datetime, mmsi, lat/10000000.0 as lat, lon/10000000.0 as lon,
                       sog/10.0 as sog, cog/10.0 as cog, heading, rot, nav_status
                FROM {table}
                WHERE mmsi = %s AND datetime >= %s AND datetime < %s
                  AND lon != 0 AND lat != 0
                ORDER BY datetime
                LIMIT %s
            """, (mmsi, start_date, end_date, limit - len(all_rows)))
            all_rows.extend(cur.fetchall())
            if len(all_rows) >= limit:
                break
        except Exception:
            conn.rollback()
    cur.close()

    return [
        {
            'datetime': str(r[0]),
            'mmsi': r[1],
            'lat': float(r[2]),
            'lon': float(r[3]),
            'sog': float(r[4]),
            'cog': float(r[5]),
            'heading': r[6],
            'rot': r[7],
            'nav_status': r[8],
        }
        for r in all_rows
    ]


def query_ais_area(conn, lat_min, lat_max, lon_min, lon_max,
                   start_date: str, end_date: str, mmsi=None, limit=10000):
    """영역 기준 항적 조회"""
    sd = datetime.strptime(start_date, '%Y-%m-%d').date()
    ed = datetime.strptime(end_date, '%Y-%m-%d').date()
    tables = get_tables_for_range(sd, ed)

    lat_min_q = int(lat_min * 10000000)
    lat_max_q = int(lat_max * 10000000)
    lon_min_q = int(lon_min * 10000000)
    lon_max_q = int(lon_max * 10000000)

    all_rows = []
    cur = conn.cursor()
    for table in tables:
        try:
            sql = f"""
                SELECT datetime, mmsi, lat/10000000.0, lon/10000000.0,
                       sog/10.0, cog/10.0, heading, rot, nav_status
                FROM {table}
                WHERE datetime >= %s AND datetime < %s
                  AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
                  AND lon != 0 AND lat != 0
            """
            params = [start_date, end_date, lat_min_q, lat_max_q, lon_min_q, lon_max_q]
            if mmsi:
                sql += " AND mmsi = %s"
                params.append(mmsi)
            sql += f" ORDER BY datetime LIMIT %s"
            params.append(limit - len(all_rows))

            cur.execute(sql, params)
            all_rows.extend(cur.fetchall())
            if len(all_rows) >= limit:
                break
        except Exception:
            conn.rollback()
    cur.close()

    return [
        {
            'datetime': str(r[0]), 'mmsi': r[1],
            'lat': float(r[2]), 'lon': float(r[3]),
            'sog': float(r[4]), 'cog': float(r[5]),
            'heading': r[6], 'rot': r[7], 'nav_status': r[8],
        }
        for r in all_rows
    ]


def query_available_tables(conn):
    """사용 가능한 테이블(기간) 목록"""
    cur = conn.cursor()
    cur.execute("""
        SELECT tablename FROM pg_tables
        WHERE schemaname='public' AND tablename LIKE 'db%'
        ORDER BY tablename
    """)
    tables = [r[0] for r in cur.fetchall()]
    cur.close()
    return tables
