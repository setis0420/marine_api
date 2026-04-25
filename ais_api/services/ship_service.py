def query_fishing_ships(conn, mmsi=None, fishing_type=None, limit=1000, offset=0):
    """어선 정보 조회"""
    cur = conn.cursor()
    sql = "SELECT mmsi, shipname, fish_ship_name, fish_ton, fishing_type, length, service FROM fishing_shipinfo WHERE 1=1"
    params = []

    if mmsi:
        sql += " AND mmsi = %s"
        params.append(mmsi)
    if fishing_type:
        sql += " AND fishing_type LIKE %s"
        params.append(f'%{fishing_type}%')

    sql += " ORDER BY mmsi LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()

    return [
        {
            'mmsi': r[0], 'shipname': r[1], 'fish_ship_name': r[2],
            'tonnage': r[3], 'fishing_type': r[4], 'length': r[5], 'service': r[6],
        }
        for r in rows
    ]


def query_fishing_ship_detail(conn, mmsi):
    """개별 어선 상세 정보"""
    cur = conn.cursor()
    cur.execute("""
        SELECT mmsi, shipname, fish_ship_name, fish_ton, fishing_type,
               fishing_type_sub1, fishing_type_sub2, length, service, datetime_update
        FROM fishing_shipinfo WHERE mmsi = %s
    """, (mmsi,))
    r = cur.fetchone()
    cur.close()

    if not r:
        return None
    return {
        'mmsi': r[0], 'shipname': r[1], 'fish_ship_name': r[2],
        'tonnage': r[3], 'fishing_type': r[4],
        'fishing_type_sub1': r[5], 'fishing_type_sub2': r[6],
        'length': r[7], 'service': r[8], 'datetime_update': str(r[9]) if r[9] else None,
    }


def query_cooperatives(conn, name=None):
    """수협 관할구역 조회"""
    cur = conn.cursor()
    if name:
        cur.execute("""
            SELECT johab_code, johab_name, lat_start, lat_end, lon_start, lon_end
            FROM fish_suhyub_location WHERE johab_name LIKE %s ORDER BY johab_code
        """, (f'%{name}%',))
    else:
        cur.execute("""
            SELECT johab_code, johab_name, lat_start, lat_end, lon_start, lon_end
            FROM fish_suhyub_location ORDER BY johab_code
        """)
    rows = cur.fetchall()
    cur.close()

    return [
        {
            'johab_code': r[0], 'johab_name': r[1],
            'lat_start': float(r[2]) if r[2] else None,
            'lat_end': float(r[3]) if r[3] else None,
            'lon_start': float(r[4]) if r[4] else None,
            'lon_end': float(r[5]) if r[5] else None,
        }
        for r in rows
    ]
