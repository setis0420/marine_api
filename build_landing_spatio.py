"""
fishing_landing_info + fishing_spatio_temporal 구축 (대형트롤 우선)

Phase A: fishing_landing_info 채움
  A1. 171에서 대형트롤 입항 키 (date, johab) + ship_count + total_fishing_hours
  A2. 21에서 fish_landing_suhyub의 트롤 위판 fetch (매칭된 키만)
  A3. JOIN → 171 INSERT

Phase B: fishing_spatio_temporal 채움
  B1. 항차별 그리드 effort + median datetime
  B2. 위판 매칭 (입항일 D, D+1 평균)
  B3. effort 비율 분배 → INSERT
"""
import psycopg2
from psycopg2.extras import execute_values
import sys, time
from datetime import timedelta
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

from db_config import LOCAL_DB as _LOCAL, FISHERY_DB as _FISH
LOCAL_DB = {'host': _LOCAL['host'], 'dbname': _LOCAL['database'],
            'user': _LOCAL['user'], 'password': _LOCAL['password'], 'port': _LOCAL['port']}
FISHERY_DB = {'host': _FISH['host'], 'dbname': _FISH['database'],
              'user': _FISH['user'], 'password': _FISH['password'], 'port': _FISH['port']}

FISHING_TYPE = '대형트롤어업'
SERVICE_TYPE = '트롤'


# ============================================================
# Phase A: fishing_landing_info
# ============================================================

def phase_a_create_table(conn):
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS fishing_landing_info")
    cur.execute("""
        CREATE TABLE fishing_landing_info (
            id BIGSERIAL PRIMARY KEY,
            landing_date DATE NOT NULL,
            fishing_type TEXT NOT NULL,
            johab_code BIGINT, johab_name TEXT,
            wepan_code TEXT, wepan_name TEXT,
            fish_code TEXT, fish_name TEXT,
            weight_kg REAL,
            price_total BIGINT,
            sale_count INTEGER,
            ship_count INTEGER,
            total_fishing_hours REAL,
            UNIQUE (landing_date, fishing_type, johab_code, wepan_code, fish_code)
        )
    """)
    cur.execute("CREATE INDEX idx_fli_date ON fishing_landing_info(landing_date)")
    cur.execute("CREATE INDEX idx_fli_johab ON fishing_landing_info(johab_code)")
    cur.execute("CREATE INDEX idx_fli_fishing_type ON fishing_landing_info(fishing_type)")
    cur.execute("CREATE INDEX idx_fli_fish_code ON fishing_landing_info(fish_code)")
    conn.commit()
    cur.close()
    print('  fishing_landing_info 테이블 생성')


def phase_a1_keys(conn):
    """입항 키 + 통계 (date, johab) → ship_count + total_fishing_hours"""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT v.end_time::date AS landing_date,
               v.johab_code, v.johab_name,
               COUNT(DISTINCT v.mmsi) AS ship_count,
               SUM(v.fishing_hours) AS total_fishing_hours
        FROM fishing_voyage v
        JOIN shipinfo s ON s.mmsi = v.mmsi
        WHERE s.fishing_type = %s
          AND v.johab_code IS NOT NULL
          AND v.fishing_hours IS NOT NULL AND v.fishing_hours > 0
        GROUP BY 1, 2, 3
    """, (FISHING_TYPE,))
    rows = cur.fetchall()
    cur.close()
    print(f'  A1: 입항 키 {len(rows):,}개')
    return rows


def phase_a2_landings(keys):
    """21번에서 위판 데이터 fetch (트롤 + 매칭 키만)"""
    if not keys:
        return []
    fc = psycopg2.connect(**FISHERY_DB)
    fcur = fc.cursor()

    # 키 튜플 (date, johab_code)
    key_tuples = [(r[0], r[1]) for r in keys]

    # 너무 많으면 chunked로 나눠서 fetch (PostgreSQL IN 제한 회피)
    CHUNK = 1000
    all_rows = []
    t0 = time.time()
    for i in range(0, len(key_tuples), CHUNK):
        chunk = key_tuples[i:i+CHUNK]
        fcur.execute("""
            SELECT datetime::date AS landing_date,
                   johab_code, johab_name,
                   wepan_code, wepan_name,
                   fish_code, fish_name,
                   SUM(saleweight) AS weight_kg,
                   SUM(saletotalprice) AS price_total,
                   COUNT(*) AS sale_count
            FROM fish_landing_suhyub
            WHERE (datetime::date, johab_code) IN %s
              AND fishshiptype LIKE '%%트롤%%'
            GROUP BY 1,2,3,4,5,6,7
        """, (tuple(chunk),))
        rows = fcur.fetchall()
        all_rows.extend(rows)
        elapsed = time.time() - t0
        eta = elapsed / (i+CHUNK) * (len(key_tuples)-i-CHUNK) if i+CHUNK < len(key_tuples) else 0
        print(f'  A2: {min(i+CHUNK, len(key_tuples)):,}/{len(key_tuples):,} 키 처리 (위판 {len(all_rows):,}행, ETA {eta:.0f}s)')
    fcur.close(); fc.close()
    print(f'  A2 완료: {len(all_rows):,} 위판 행 fetch ({time.time()-t0:.0f}s)')
    return all_rows


def phase_a3_insert(conn, keys, landings):
    """JOIN → 171 INSERT"""
    cur = conn.cursor()

    # 키별 통계 dict
    entry_info = {(r[0], r[1]): {'ship_count': r[3], 'total_fishing_hours': r[4]} for r in keys}

    rows_to_insert = []
    for r in landings:
        ldate, johab_code, johab_name, wepan_code, wepan_name, fish_code, fish_name, weight, price, sale_cnt = r
        info = entry_info.get((ldate, johab_code))
        if not info:
            continue
        rows_to_insert.append((
            ldate, FISHING_TYPE,
            johab_code, johab_name, wepan_code, wepan_name,
            fish_code, fish_name,
            float(weight) if weight else 0,
            int(price) if price else 0,
            int(sale_cnt) if sale_cnt else 0,
            info['ship_count'], float(info['total_fishing_hours']),
        ))

    if rows_to_insert:
        execute_values(cur, """
            INSERT INTO fishing_landing_info
              (landing_date, fishing_type, johab_code, johab_name, wepan_code, wepan_name,
               fish_code, fish_name, weight_kg, price_total, sale_count,
               ship_count, total_fishing_hours)
            VALUES %s
            ON CONFLICT (landing_date, fishing_type, johab_code, wepan_code, fish_code) DO UPDATE
              SET weight_kg = EXCLUDED.weight_kg,
                  price_total = EXCLUDED.price_total,
                  sale_count = EXCLUDED.sale_count,
                  ship_count = EXCLUDED.ship_count,
                  total_fishing_hours = EXCLUDED.total_fishing_hours
        """, rows_to_insert, page_size=2000)
        conn.commit()
    cur.close()
    print(f'  A3: {len(rows_to_insert):,} 행 INSERT')


# ============================================================
# Phase B: fishing_spatio_temporal
# ============================================================

def phase_b_create_table(conn):
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS fishing_spatio_temporal")
    cur.execute("""
        CREATE TABLE fishing_spatio_temporal (
            id BIGSERIAL PRIMARY KEY,
            datetime DATE NOT NULL,
            mmsi BIGINT NOT NULL,
            fishing_ship_type TEXT,
            service_type TEXT,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            fishing_hours REAL,
            fish_code TEXT,
            fish_name TEXT,
            weight_kg REAL,
            sell_location TEXT,
            landing_date DATE,
            effort_source TEXT,
            UNIQUE (datetime, mmsi, lat, lon, fish_code)
        )
    """)
    cur.execute("CREATE INDEX idx_fst_mmsi ON fishing_spatio_temporal(mmsi)")
    cur.execute("CREATE INDEX idx_fst_dt ON fishing_spatio_temporal(datetime)")
    cur.execute("CREATE INDEX idx_fst_grid ON fishing_spatio_temporal(lat, lon)")
    cur.execute("CREATE INDEX idx_fst_fish ON fishing_spatio_temporal(fish_code)")
    conn.commit()
    cur.close()
    print('  fishing_spatio_temporal 테이블 생성')


def phase_b_process(conn):
    """항차별로 그리드 effort + 위판 매칭 + 분배 INSERT"""
    cur = conn.cursor()

    # 대형트롤 항차 + johab 매칭된 것만
    cur.execute(f"""
        SELECT v.id, v.mmsi, v.start_time, v.end_time, v.fishing_hours,
               v.johab_code, v.johab_name
        FROM fishing_voyage v
        JOIN shipinfo s ON s.mmsi = v.mmsi
        WHERE s.fishing_type = %s
          AND v.johab_code IS NOT NULL
          AND v.fishing_hours IS NOT NULL AND v.fishing_hours > 0
        ORDER BY v.mmsi, v.voyage_num
    """, (FISHING_TYPE,))
    voyages = cur.fetchall()
    print(f'  처리 대상 항차: {len(voyages):,}')
    cur.close()

    t0 = time.time()
    total_inserted = 0
    no_landing = 0
    no_grid = 0

    cur = conn.cursor()
    for vi, voy in enumerate(voyages, 1):
        voyage_id, mmsi, start_time, end_time, voy_fh, johab_code, johab_name = voy
        landing_date = end_time.date()

        # B1. 그리드 effort + median datetime 계산
        try:
            cur.execute(f"""
                WITH fishing_pts AS (
                  SELECT datetime,
                         ROUND((lat / 1e7)::numeric, 2)::real AS lat_g,
                         ROUND((lon / 1e7)::numeric, 2)::real AS lon_g
                  FROM ship_{mmsi}
                  WHERE datetime BETWEEN %s AND %s
                    AND sog BETWEEN 20 AND 50
                    AND lat BETWEEN -900000000 AND 900000000
                    AND lon BETWEEN -1800000000 AND 1800000000
                )
                SELECT lat_g, lon_g,
                       COUNT(*) / 60.0 AS hours,
                       (PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY datetime))::date AS median_date
                FROM fishing_pts
                GROUP BY lat_g, lon_g
            """, (start_time, end_time))
            grids = cur.fetchall()
        except Exception as e:
            conn.rollback()
            print(f'  voyage {voyage_id} grid 오류: {str(e)[:80]}')
            continue

        if not grids:
            no_grid += 1
            continue

        total_grid_hr = sum(float(g[2]) for g in grids)
        if total_grid_hr <= 0:
            no_grid += 1
            continue

        # B2. 위판 매칭 (D, D+1, D+2 윈도우; 데이터 있는 날만 평균)
        D = landing_date
        D1 = D + timedelta(days=1)
        D2 = D + timedelta(days=2)
        cur.execute("""
            SELECT fish_code, fish_name, landing_date, weight_kg, total_fishing_hours
            FROM fishing_landing_info
            WHERE landing_date IN (%s, %s, %s)
              AND johab_code = %s
              AND fishing_type = %s
        """, (D, D1, D2, johab_code, FISHING_TYPE))
        landings = cur.fetchall()
        if not landings:
            no_landing += 1
            continue

        # 어종별로 데이터 있는 날만 모음
        fish_dict = {}  # fish_code → {weights:[], efforts:[], name}
        for fc, fn, ldate, w, eff in landings:
            if fc not in fish_dict:
                fish_dict[fc] = {'name': fn, 'weights': [], 'efforts': []}
            if w and float(w) > 0:
                fish_dict[fc]['weights'].append(float(w))
                fish_dict[fc]['efforts'].append(float(eff or 0))

        # B3. 분배 → INSERT
        rows = []
        for fc, info in fish_dict.items():
            n = len(info['weights'])
            if n == 0:
                continue
            w_avg = sum(info['weights']) / n            # 데이터 있는 날만 평균
            e_avg = sum(info['efforts']) / n            # 효율 분모도 같은 방식
            if e_avg <= 0 or w_avg <= 0:
                continue
            ship_share = float(voy_fh) / e_avg
            ship_kg = w_avg * ship_share

            for lat_g, lon_g, hr, mdate in grids:
                grid_share = float(hr) / total_grid_hr
                grid_kg = ship_kg * grid_share
                rows.append((
                    mdate, mmsi, FISHING_TYPE, SERVICE_TYPE,
                    float(lat_g), float(lon_g), float(hr),
                    fc, info['name'], grid_kg,
                    johab_name, D, 'sog'
                ))

        if rows:
            try:
                execute_values(cur, """
                    INSERT INTO fishing_spatio_temporal
                      (datetime, mmsi, fishing_ship_type, service_type, lat, lon, fishing_hours,
                       fish_code, fish_name, weight_kg, sell_location, landing_date, effort_source)
                    VALUES %s
                    ON CONFLICT (datetime, mmsi, lat, lon, fish_code) DO UPDATE
                      SET weight_kg = fishing_spatio_temporal.weight_kg + EXCLUDED.weight_kg,
                          fishing_hours = GREATEST(fishing_spatio_temporal.fishing_hours, EXCLUDED.fishing_hours)
                """, rows, page_size=2000)
                conn.commit()
                total_inserted += len(rows)
            except Exception as e:
                conn.rollback()
                print(f'  voyage {voyage_id} INSERT 오류: {str(e)[:80]}')

        if vi % 200 == 0:
            elapsed = time.time() - t0
            eta = elapsed / vi * (len(voyages) - vi)
            print(f'  진행 {vi:,}/{len(voyages):,} 항차 (insert {total_inserted:,}, no_grid {no_grid}, no_landing {no_landing}, ETA {eta:.0f}s)')

    cur.close()
    print(f'\n[B] 완료: voyage {len(voyages):,}, INSERT {total_inserted:,}행')
    print(f'   매칭 실패: 그리드 없음 {no_grid}, 위판 없음 {no_landing}')


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--skip-a', action='store_true', help='Phase A 건너뛰기')
    p.add_argument('--skip-b', action='store_true', help='Phase B 건너뛰기')
    args = p.parse_args()

    conn = psycopg2.connect(**LOCAL_DB)
    t0 = time.time()

    if not args.skip_a:
        print('='*60); print('[Phase A] fishing_landing_info 구축')
        phase_a_create_table(conn)
        keys = phase_a1_keys(conn)
        landings = phase_a2_landings(keys)
        phase_a3_insert(conn, keys, landings)
        print(f'  Phase A 소요: {time.time()-t0:.0f}s')

    if not args.skip_b:
        print(); print('='*60); print('[Phase B] fishing_spatio_temporal 구축')
        tb = time.time()
        phase_b_create_table(conn)
        phase_b_process(conn)
        print(f'  Phase B 소요: {time.time()-tb:.0f}s')

    print(); print('='*60)
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM fishing_landing_info')
    print(f'fishing_landing_info: {cur.fetchone()[0]:,}행')
    cur.execute('SELECT COUNT(*), COUNT(DISTINCT mmsi), COUNT(DISTINCT (lat, lon)) FROM fishing_spatio_temporal')
    r = cur.fetchone()
    print(f'fishing_spatio_temporal: {r[0]:,}행, {r[1]} 어선, {r[2]:,} 그리드')
    cur.close()

    print(f'\n전체 소요: {time.time()-t0:.0f}s')
    conn.close()


if __name__ == '__main__':
    main()
