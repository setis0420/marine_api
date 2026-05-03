"""
fishing_voyage에 입항 좌표 + 수협 매칭
- Phase 1: 컬럼 추가
- Phase 2: ship_{mmsi}에서 end_time 시점 lat/lon 추출
- Phase 3: 가장 가까운 수협 매칭 (5km 이내)
- Phase 4: 인덱스
"""
import psycopg2
from psycopg2.extras import execute_values
import numpy as np
import sys, time
from math import radians

sys.stdout.reconfigure(encoding='utf-8')

LOCAL_DB = {'host': '203.253.202.171', 'dbname': 'marine',
            'user': 'postgres', 'password': 'prhkddlf0420', 'port': 5432}
FISHERY_DB = {'host': '203.253.202.21', 'dbname': 'fishery',
              'user': 'fishery_readonly_2', 'password': 'readonly', 'port': 5432}

DIST_THRESHOLD_KM = 5.0


def phase1_add_columns(conn):
    cur = conn.cursor()
    print('[Phase 1] 컬럼 추가...')
    statements = [
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS id BIGSERIAL",
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS entry_lat DOUBLE PRECISION",
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS entry_lon DOUBLE PRECISION",
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS johab_code BIGINT",
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS johab_name TEXT",
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS entry_distance_km REAL",
    ]
    for s in statements:
        cur.execute(s)
    # id에 UNIQUE 인덱스 (이미 있으면 skip)
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fishing_voyage_id ON fishing_voyage(id)")
    except Exception as e:
        conn.rollback()
        print(f'  id 인덱스: {e}')
    conn.commit()
    cur.close()
    print('  완료')


def phase2_extract_coords(initial_conn):
    """ship_{mmsi}에서 voyage end_time 좌표 추출 (재연결 지원)"""
    print('[Phase 2] 입항 좌표 추출...')
    conn = initial_conn

    # 처리 대상 mmsi
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT v.mmsi FROM fishing_voyage v WHERE v.entry_lat IS NULL""")
    mmsis = [r[0] for r in cur.fetchall()]
    print(f'  처리 대상: {len(mmsis)} 척')
    if not mmsis:
        print('  이미 모두 처리됨')
        cur.close()
        return conn

    cur.execute("""SELECT REPLACE(table_name, 'ship_', '')::bigint FROM information_schema.tables
                   WHERE table_schema='public' AND table_name ~ '^ship_[0-9]+$'""")
    ship_set = set(r[0] for r in cur.fetchall())
    cur.close()

    t0 = time.time()
    total_updated = 0
    no_table = 0

    def get_cur():
        nonlocal conn
        try:
            c = conn.cursor()
            c.execute('SELECT 1')
            c.close()
        except Exception:
            try: conn.close()
            except: pass
            print('    재연결...')
            conn = psycopg2.connect(**LOCAL_DB)
        return conn.cursor()

    for i, mmsi in enumerate(mmsis, 1):
        if mmsi not in ship_set:
            no_table += 1
            continue
        retries = 0
        while retries < 3:
            try:
                cur = get_cur()
                cur.execute(f"""
                    UPDATE fishing_voyage v
                    SET entry_lat = s.lat / 10000000.0,
                        entry_lon = s.lon / 10000000.0
                    FROM ship_{mmsi} s
                    WHERE v.mmsi = {mmsi}
                      AND v.end_time = s.datetime
                      AND v.entry_lat IS NULL
                """)
                total_updated += cur.rowcount
                conn.commit()
                cur.close()
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                print(f'    mmsi {mmsi} 연결 끊김 ({retries+1}/3): {str(e)[:80]}')
                try: conn.close()
                except: pass
                time.sleep(5)
                conn = psycopg2.connect(**LOCAL_DB)
                retries += 1
            except Exception as e:
                try: conn.rollback()
                except: pass
                print(f'  mmsi {mmsi}: {str(e)[:80]}')
                break

        if i % 100 == 0:
            elapsed = time.time() - t0
            eta = elapsed / i * (len(mmsis) - i)
            print(f'  {i}/{len(mmsis)} 진행 (업데이트 {total_updated:,}, ETA {eta:.0f}s)')

    print(f'  완료: 업데이트 {total_updated:,}, ship_{{mmsi}} 없음 {no_table}')
    return conn


def haversine_km(lat1, lon1, lat2, lon2):
    """벡터화 haversine 거리 (km)"""
    R = 6371.0088
    lat1_r = np.radians(lat1)
    lat2_r = np.radians(lat2)
    dlat = lat2_r - lat1_r
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat/2)**2 + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon/2)**2
    return 2 * R * np.arcsin(np.sqrt(a))


def phase3_match_johab(conn):
    """가장 가까운 수협 매칭"""
    cur = conn.cursor()
    print('[Phase 3] 수협 매칭 (5km 이내)...')

    # 수협 좌표 로드 (구역 사각형 → 중심점)
    fc = psycopg2.connect(**FISHERY_DB)
    fcur = fc.cursor()
    fcur.execute("""SELECT johab_code, johab_name,
                     (lat_start+lat_end)/2 AS lat,
                     (lon_start+lon_end)/2 AS lon
                   FROM fish_suhyub_location
                   WHERE lat_start IS NOT NULL AND lon_start IS NOT NULL""")
    suhyubs = fcur.fetchall()
    fc.close()
    print(f'  수협 구역 {len(suhyubs)}개 로드 ({len(set(s[0] for s in suhyubs))} 고유 수협)')

    sh_codes = np.array([s[0] for s in suhyubs])
    sh_names = [s[1] for s in suhyubs]
    sh_lats = np.array([float(s[2]) for s in suhyubs])
    sh_lons = np.array([float(s[3]) for s in suhyubs])

    # 처리 대상 voyage (좌표 있고 johab 미할당)
    cur.execute("""SELECT id, entry_lat, entry_lon FROM fishing_voyage
                   WHERE entry_lat IS NOT NULL AND johab_code IS NULL""")
    rows = cur.fetchall()
    print(f'  매칭 대상: {len(rows):,}개 voyage')
    if not rows:
        cur.close()
        return

    t0 = time.time()
    BATCH = 10000
    matched = 0
    unmatched = 0
    updates = []

    for batch_i in range(0, len(rows), BATCH):
        batch = rows[batch_i:batch_i+BATCH]
        ids = np.array([r[0] for r in batch])
        v_lats = np.array([float(r[1]) for r in batch])
        v_lons = np.array([float(r[2]) for r in batch])

        # (n, m) 거리 행렬 — 메모리 부담 줄이기 위해 row 단위 broadcasting
        # n_voys × m_suhyub
        # Step 1: lat 차로 1차 필터 (5km 이내는 약 0.05도)
        for j in range(len(batch)):
            d = haversine_km(v_lats[j], v_lons[j], sh_lats, sh_lons)
            min_idx = int(np.argmin(d))
            min_d = float(d[min_idx])
            if min_d <= DIST_THRESHOLD_KM:
                updates.append((int(sh_codes[min_idx]), sh_names[min_idx], round(min_d, 3), int(ids[j])))
                matched += 1
            else:
                unmatched += 1

        if batch_i % (BATCH * 10) == 0 and batch_i > 0:
            elapsed = time.time() - t0
            eta = elapsed / (batch_i + BATCH) * (len(rows) - batch_i - BATCH)
            print(f'  {batch_i+BATCH:,}/{len(rows):,} (매칭 {matched:,}, ETA {eta:.0f}s)')

    print(f'  계산 완료. 매칭 {matched:,} / 미매칭 {unmatched:,}')

    # 일괄 UPDATE
    print('  DB UPDATE...')
    t0 = time.time()
    if updates:
        cur.execute("""CREATE TEMP TABLE tmp_johab (
            johab_code BIGINT, johab_name TEXT, dist_km REAL, voy_id BIGINT
        ) ON COMMIT DROP""")
        execute_values(cur,
            "INSERT INTO tmp_johab (johab_code, johab_name, dist_km, voy_id) VALUES %s",
            updates, page_size=5000)
        cur.execute("""UPDATE fishing_voyage v SET
                         johab_code = t.johab_code,
                         johab_name = t.johab_name,
                         entry_distance_km = t.dist_km
                       FROM tmp_johab t WHERE v.id = t.voy_id""")
        conn.commit()
    print(f'  UPDATE 완료 ({time.time()-t0:.1f}s)')
    cur.close()


def phase4_indexes(conn):
    cur = conn.cursor()
    print('[Phase 4] 인덱스...')
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_fv_johab ON fishing_voyage(johab_code)",
        "CREATE INDEX IF NOT EXISTS idx_fv_entry ON fishing_voyage(entry_lat, entry_lon)",
    ]:
        cur.execute(sql)
    conn.commit()
    cur.close()
    print('  완료')


def summary(conn):
    cur = conn.cursor()
    cur.execute("""SELECT
                     COUNT(*) total,
                     COUNT(entry_lat) with_coord,
                     COUNT(johab_code) with_johab,
                     ROUND(AVG(entry_distance_km)::numeric, 2) avg_dist
                   FROM fishing_voyage""")
    r = cur.fetchone()
    print('\n=== 최종 결과 ===')
    print(f'전체 voyage: {r[0]:,}')
    print(f'좌표 보유: {r[1]:,} ({100*r[1]/r[0]:.1f}%)')
    print(f'수협 매칭: {r[2]:,} ({100*r[2]/r[0]:.1f}%)')
    print(f'평균 거리: {r[3]} km')
    cur.execute("""SELECT johab_name, COUNT(*) FROM fishing_voyage
                   WHERE johab_code IS NOT NULL GROUP BY johab_name ORDER BY 2 DESC LIMIT 10""")
    print('\n매칭 상위 10 수협:')
    for r in cur.fetchall(): print(f'  {r[0]}: {r[1]:,}')
    cur.close()


def main():
    conn = psycopg2.connect(**LOCAL_DB)
    t0 = time.time()
    phase1_add_columns(conn)
    conn = phase2_extract_coords(conn)
    phase3_match_johab(conn)
    phase4_indexes(conn)
    summary(conn)
    print(f'\n전체 소요: {time.time()-t0:.0f}s')
    try: conn.close()
    except: pass


if __name__ == '__main__':
    main()
