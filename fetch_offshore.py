"""
근해 어선 항적 fetch + voyage_num + fishing_voyage
- 21번 db{YYYYMM} → 171 ship_{mmsi} (2012-01 ~ 2024-12)
- C+A 중복 방지: 기존 보유 기간 스킵 + ON CONFLICT (datetime) DO NOTHING
- port_entering 계산 (수심 -10m 기준, 30분 입항 지속 시 진짜 입항)
- voyage_num 부여 (YY*1000 + 순번)
- fishing_voyage 테이블에 voyage 메타 INSERT

사용법:
    python fetch_offshore.py --target-csv offshore_target.csv --workers 7
    python fetch_offshore.py --fishing-type 대형트롤어업 --workers 7
    python fetch_offshore.py --mmsi 440137010
"""

import psycopg2
from psycopg2.extras import execute_values
import numpy as np
import os
import csv
import argparse
import logging
import time
from datetime import datetime, timedelta
from multiprocessing import Pool, Value, Lock

# === DB 설정 ===
REMOTE_DB = {'host': '203.253.202.21', 'dbname': 'aisdb',
             'user': 'fishery_readonly_2', 'password': 'readonly', 'port': '5432'}
LOCAL_DB = {'host': '203.253.202.171', 'dbname': 'marine',
            'user': 'postgres', 'password': 'prhkddlf0420', 'port': '5432'}

# === 경로 (스크립트 위치 기준 자동 해석) ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR  # 이 스크립트가 프로젝트 루트에 있음
DEPTH_FILE = os.path.join(PROJECT_ROOT, '수심', 'depth_450m_s27.0_w119.0_e137.0.nc')
DEPTH_TEMP = r'C:\temp\depth.nc'  # netCDF4가 한글 경로 못 열어서 ASCII 경로로 복사
DEFAULT_TARGET_CSV = os.path.join(PROJECT_ROOT, 'offshore_target.csv')
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

LAND_THRESHOLD = -10
PORT_STAY_THRESHOLD = timedelta(minutes=30)

START_DATE = datetime(2012, 1, 1)
END_DATE = datetime(2025, 1, 1)
BATCH_SIZE = 5000

log_file = os.path.join(LOG_DIR, f'fetch_offshore_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
logging.basicConfig(level=logging.INFO,
    format='%(asctime)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler()])

counter = None
counter_lock = None
_depth_cache = None


def init_worker(c, l):
    global counter, counter_lock
    counter = c
    counter_lock = l


def load_depth():
    global _depth_cache
    if _depth_cache is not None:
        return _depth_cache
    import netCDF4 as nc
    import shutil
    if not os.path.exists(DEPTH_TEMP):
        os.makedirs(os.path.dirname(DEPTH_TEMP), exist_ok=True)
        shutil.copy(DEPTH_FILE, DEPTH_TEMP)
    ds = nc.Dataset(DEPTH_TEMP)
    lats = ds.variables['lat'][:]
    lons = ds.variables['lon'][:]
    elev = ds.variables['elevation'][:]
    ds.close()
    _depth_cache = (np.array(lats), np.array(lons), np.array(elev))
    return _depth_cache


def ensure_table(local_cur, mmsi):
    """ship_{mmsi} 테이블 보장 (스키마 통일, datetime PK)"""
    table = f"ship_{mmsi}"
    local_cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name=%s AND table_schema='public')", (table,))
    if local_cur.fetchone()[0]:
        return table
    local_cur.execute(f"""
        CREATE TABLE {table} (
            datetime TIMESTAMP PRIMARY KEY,
            sog SMALLINT, lon INTEGER, lat INTEGER, cog SMALLINT,
            model_fishing_type SMALLINT, model_fishing_status SMALLINT,
            port_entering BOOLEAN, voyage_num INTEGER
        )
    """)
    local_cur.execute(f"CREATE INDEX ON {table} (voyage_num)")
    return table


def fetch_one_ship(args):
    """한 척 fetch + voyage 처리"""
    mmsi, total_ships = args
    local_conn = remote_conn = None
    try:
        local_conn = psycopg2.connect(**LOCAL_DB)
        local_cur = local_conn.cursor()
        table = ensure_table(local_cur, mmsi)
        local_conn.commit()

        # 기존 보유 기간 (월 단위로)
        local_cur.execute(f"""SELECT EXTRACT(YEAR FROM datetime)::int, EXTRACT(MONTH FROM datetime)::int, COUNT(*)
                              FROM {table} GROUP BY 1, 2""")
        existing_months = {(y, m) for y, m, _ in local_cur.fetchall()}

        depth_lats, depth_lons, depth_elev = load_depth()
        lat_res = float(depth_lats[1] - depth_lats[0])
        lon_res = float(depth_lons[1] - depth_lons[0])

        remote_conn = psycopg2.connect(**REMOTE_DB)
        total_inserted = 0

        d = START_DATE
        while d < END_DATE:
            yr, mo = d.year, d.month
            if mo == 12:
                next_d = datetime(yr + 1, 1, 1)
            else:
                next_d = datetime(yr, mo + 1, 1)

            # 이미 데이터 있는 월은 스킵 (C: existing range check)
            if (yr, mo) in existing_months:
                d = next_d
                continue

            db_table = f"db{yr}{mo:02d}"
            try:
                remote_cur = remote_conn.cursor()
                remote_cur.execute(f"""
                    SELECT datetime, sog, lon, lat, cog
                    FROM {db_table}
                    WHERE mmsi = %s AND datetime >= %s AND datetime < %s
                      AND lat <> 0 AND lon <> 0
                """, (int(mmsi), d, next_d))
                rows = remote_cur.fetchall()
                remote_cur.close()

                if rows:
                    # port_entering 계산
                    lat_arr = np.array([float(r[3]) for r in rows]) / 10000000.0
                    lon_arr = np.array([float(r[2]) for r in rows]) / 10000000.0
                    lat_idx = np.clip(((lat_arr - depth_lats[0]) / lat_res).astype(int), 0, len(depth_lats) - 1)
                    lon_idx = np.clip(((lon_arr - depth_lons[0]) / lon_res).astype(int), 0, len(depth_lons) - 1)
                    valid = (lat_arr >= depth_lats[0]) & (lat_arr <= depth_lats[-1]) & \
                            (lon_arr >= depth_lons[0]) & (lon_arr <= depth_lons[-1])
                    port_flags = np.zeros(len(rows), dtype=bool)
                    if valid.any():
                        port_flags[valid] = depth_elev[lat_idx[valid], lon_idx[valid]] >= LAND_THRESHOLD

                    # INSERT (A: ON CONFLICT)
                    for i in range(0, len(rows), BATCH_SIZE):
                        batch = rows[i:i+BATCH_SIZE]
                        bp = port_flags[i:i+BATCH_SIZE]
                        # datetime, sog, lon, lat, cog, port_entering
                        values = [(r[0], r[1], r[2], r[3], r[4], bool(p)) for r, p in zip(batch, bp)]
                        execute_values(local_cur,
                            f"INSERT INTO {table} (datetime, sog, lon, lat, cog, port_entering) "
                            f"VALUES %s ON CONFLICT (datetime) DO NOTHING",
                            values, page_size=1000)
                    local_conn.commit()
                    total_inserted += len(rows)
            except Exception as e:
                local_conn.rollback()
                try: remote_conn.close()
                except: pass
                remote_conn = psycopg2.connect(**REMOTE_DB)
                logging.warning(f'{mmsi} {db_table}: {str(e)[:80]}')

            d = next_d

        # voyage_num 계산 + fishing_voyage 적재
        v_count = compute_voyage_and_save(local_cur, mmsi, table)
        local_conn.commit()

        with counter_lock:
            counter.value += 1
            cur_n = counter.value

        return (mmsi, total_inserted, v_count, cur_n, total_ships, None)
    except Exception as e:
        with counter_lock:
            counter.value += 1
            cur_n = counter.value
        return (mmsi, 0, 0, cur_n, total_ships, str(e)[:200])
    finally:
        try: local_conn.close()
        except: pass
        try: remote_conn.close()
        except: pass


def compute_voyage_and_save(cur, mmsi, table):
    """전체 데이터에 voyage_num 부여 + fishing_voyage 적재"""
    # 미할당 데이터 가져오기 (voyage_num IS NULL이거나 전체 재계산)
    cur.execute(f"""SELECT datetime, port_entering, voyage_num FROM {table} ORDER BY datetime""")
    records = cur.fetchall()
    if len(records) < 2:
        return 0

    n = len(records)
    dt_arr = [r[0] for r in records]
    port_raw = np.array([bool(r[1]) if r[1] is not None else False for r in records])

    # 입항 30분 지속 = 실제 입항
    real_port = np.zeros(n, dtype=bool)
    i = 0
    while i < n:
        if port_raw[i]:
            seg_s = i
            while i < n and port_raw[i]:
                i += 1
            if dt_arr[i-1] - dt_arr[seg_s] >= PORT_STAY_THRESHOLD:
                real_port[seg_s:i] = True
        else:
            i += 1

    # voyage_num 부여 (연도별 순번)
    voyage_nums = [None] * n
    year_count = {}
    current_vnum = None
    prev_in_port = bool(real_port[0])
    if not prev_in_port:
        yy = dt_arr[0].year % 100
        year_count[yy] = year_count.get(yy, 0) + 1
        current_vnum = yy * 1000 + year_count[yy]
        voyage_nums[0] = current_vnum

    for i in range(1, n):
        in_port = bool(real_port[i])
        if prev_in_port and not in_port:
            yy = dt_arr[i].year % 100
            year_count[yy] = year_count.get(yy, 0) + 1
            current_vnum = yy * 1000 + year_count[yy]
        if current_vnum is not None and not in_port:
            voyage_nums[i] = current_vnum
        prev_in_port = in_port

    # ship_{mmsi}.voyage_num UPDATE (변경된 것만)
    updates = [(dt_arr[i], voyage_nums[i]) for i in range(n) if voyage_nums[i] != records[i][2]]
    if updates:
        cur.execute("CREATE TEMP TABLE tmp_v (dt TIMESTAMP, vnum INTEGER) ON COMMIT DROP")
        execute_values(cur, "INSERT INTO tmp_v VALUES %s", updates, page_size=5000)
        cur.execute(f"UPDATE {table} t SET voyage_num=tmp.vnum FROM tmp_v tmp WHERE t.datetime=tmp.dt")

    # voyage 단위 메타 (start, end, duration) 계산
    voyage_meta = {}
    for i in range(n):
        v = voyage_nums[i]
        if v is None: continue
        if v not in voyage_meta:
            voyage_meta[v] = {'start': dt_arr[i], 'end': dt_arr[i], 'count': 0}
        voyage_meta[v]['end'] = dt_arr[i]
        voyage_meta[v]['count'] += 1

    # fishing_voyage INSERT (target_area='total')
    if voyage_meta:
        rows_v = [(int(mmsi), v, m['start'], m['end'], 'total',
                   (m['end'] - m['start']).total_seconds() / 60.0, 'none', None, None)
                  for v, m in voyage_meta.items()]
        execute_values(cur,
            "INSERT INTO fishing_voyage (mmsi, voyage_num, start_time, end_time, target_area, duration, model, nav_duration, stby_duration) "
            "VALUES %s ON CONFLICT (mmsi, voyage_num, target_area) DO UPDATE "
            "SET start_time=EXCLUDED.start_time, end_time=EXCLUDED.end_time, duration=EXCLUDED.duration",
            rows_v, page_size=1000)

    return len(voyage_meta)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--target-csv', default=DEFAULT_TARGET_CSV)
    p.add_argument('--fishing-type', default=None,
                   help='업종 필터 (콤마로 여러 개 지정 가능, 예: "대형선망어업,근해채낚기어업")')
    p.add_argument('--mmsi', type=int, default=None, help='단일 MMSI 테스트')
    p.add_argument('--workers', type=int, default=7)
    args = p.parse_args()

    fishing_types = None
    if args.fishing_type:
        fishing_types = {t.strip() for t in args.fishing_type.split(',') if t.strip()}

    targets = []
    if args.mmsi:
        targets = [args.mmsi]
    else:
        with open(args.target_csv, 'r', encoding='utf-8-sig') as f:
            r = csv.DictReader(f)
            for row in r:
                if fishing_types and row['fishing_type'] not in fishing_types:
                    continue
                targets.append(int(row['mmsi']))

    logging.info('=' * 60)
    logging.info(f'fetch_offshore 시작 | 대상 {len(targets)}척 | 워커 {args.workers}')
    if fishing_types:
        logging.info(f'업종 필터: {", ".join(sorted(fishing_types))}')
    logging.info(f'기간: {START_DATE.date()} ~ {END_DATE.date()}')
    logging.info('=' * 60)

    cnt = Value('i', 0)
    lk = Lock()
    work_args = [(m, len(targets)) for m in targets]
    t0 = time.time()
    succ = fail = 0
    total_ins = 0
    total_voy = 0
    with Pool(processes=args.workers, initializer=init_worker, initargs=(cnt, lk)) as pool:
        for mmsi, ins, vc, n, total, err in pool.imap_unordered(fetch_one_ship, work_args):
            elapsed = time.time() - t0
            eta = elapsed / n * (total - n) if n > 0 else 0
            if err:
                fail += 1
                logging.error(f'[{n}/{total}] {mmsi}: {err}')
            else:
                succ += 1
                total_ins += ins
                total_voy += vc
                logging.info(f'[{n}/{total}] {mmsi}: +{ins:,}행, voyage {vc}개 ({elapsed:.0f}s, ETA {eta:.0f}s)')

    logging.info('=' * 60)
    logging.info(f'완료: 성공 {succ} 실패 {fail} | 행 {total_ins:,} | 항차 {total_voy:,} | {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
