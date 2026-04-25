"""
전체 선박 항적 데이터 fetch (21번 서버 → 171 ship_{mmsi})
- kfw_ebp_shipinfo 전체 선박 대상
- ship_{mmsi} 테이블 없으면 생성
- 이미 있는 기간은 스킵 (MAX(datetime) 이후부터)
- 멀티프로세스

사용법:
    python fetch_all_ships.py --workers 7
    python fetch_all_ships.py --mmsi 440137010
    python fetch_all_ships.py --start 2019-01-01 --end 2025-01-01
"""

import psycopg2
import numpy as np
import os
import shutil
import argparse
import logging
import time
from datetime import datetime, timedelta
from multiprocessing import Pool, Value, Lock

# === DB 설정 ===
REMOTE_DB = {
    'host': '203.253.202.21',
    'dbname': 'aisdb',
    'user': 'fishery_readonly_2',
    'password': 'readonly',
    'port': '5432'
}

LOCAL_DB = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

# === 수심 설정 ===
DEPTH_TEMP = r'C:\temp\depth.nc'
DEPTH_FILE = r'K:\coding_project\해양수산 데이터 분석 플랫폼\수심\depth_450m_s27.0_w119.0_e137.0.nc'
LAND_THRESHOLD = -10

# === 설정 ===
START_DATE = '2019-01-01'
END_DATE = '2025-01-01'
NUM_WORKERS = 7
BATCH_SIZE = 5000

# 로그
log_file = f'fetch_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

counter = None
counter_lock = None
_depth_data = None


def init_worker(c, l):
    global counter, counter_lock
    counter = c
    counter_lock = l


def load_depth():
    global _depth_data
    if _depth_data is not None:
        return _depth_data
    import netCDF4 as nc
    os.makedirs(os.path.dirname(DEPTH_TEMP), exist_ok=True)
    if not os.path.exists(DEPTH_TEMP):
        shutil.copy2(DEPTH_FILE, DEPTH_TEMP)
    ds = nc.Dataset(DEPTH_TEMP)
    _depth_data = (np.array(ds.variables['lat'][:]), np.array(ds.variables['lon'][:]), np.array(ds.variables['elevation'][:]))
    ds.close()
    return _depth_data


def fetch_ship(args):
    """한 선박의 전체 기간 데이터 fetch"""
    mmsi, start_date, end_date, total_ships = args

    local_conn = None
    remote_conn = None
    try:
        local_conn = psycopg2.connect(**LOCAL_DB)
        local_cur = local_conn.cursor()
        ship_table = f"ship_{mmsi}"

        # 테이블 없으면 생성
        local_cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)", (ship_table,))
        if not local_cur.fetchone()[0]:
            local_cur.execute(f"""
                CREATE TABLE {ship_table} (
                    mmsi INTEGER, datetime TIMESTAMP, rot SMALLINT, sog SMALLINT,
                    lon INTEGER, lat INTEGER, cog SMALLINT, heading SMALLINT,
                    model_fishing_type SMALLINT, model_fishing_status SMALLINT,
                    port_entering BOOLEAN, voyage_num SMALLINT
                )
            """)
            local_cur.execute(f"CREATE INDEX ON {ship_table} (datetime)")
            local_cur.execute(f"CREATE UNIQUE INDEX ON {ship_table} (mmsi, datetime)")
            local_cur.execute(f"CREATE INDEX ON {ship_table} (voyage_num)")
            local_conn.commit()

        # 기간별 이미 있는 데이터 확인
        local_cur.execute(f"SELECT MIN(datetime), MAX(datetime) FROM {ship_table}")
        existing = local_cur.fetchone()
        existing_min = existing[0]
        existing_max = existing[1]

        # 수심 데이터
        depth_lats, depth_lons, depth_elev = load_depth()
        lat_res = float(depth_lats[1] - depth_lats[0])
        lon_res = float(depth_lons[1] - depth_lons[0])

        # 월별로 fetch
        remote_conn = psycopg2.connect(**REMOTE_DB)
        total_inserted = 0

        d = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')

        while d < end:
            month_start = d
            if d.month == 12:
                month_end = datetime(d.year + 1, 1, 1)
            else:
                month_end = datetime(d.year, d.month + 1, 1)
            month_end = min(month_end, end)

            # 이미 데이터가 있는 구간이면 스킵
            if existing_min and existing_max:
                if month_start >= existing_min and month_end <= existing_max + timedelta(days=1):
                    d = month_end
                    continue

            table = f"db{d.year}{d.month:02d}"

            try:
                remote_cur = remote_conn.cursor()
                remote_cur.execute(f"""
                    SELECT DISTINCT mmsi, datetime, rot, sog, lon, lat, cog, heading
                    FROM {table}
                    WHERE mmsi = %s AND datetime >= %s AND datetime < %s
                      AND lon <> 0 AND lat <> 0
                    ORDER BY datetime
                """, (int(mmsi), month_start, month_end))
                rows = remote_cur.fetchall()
                remote_cur.close()

                if rows:
                    # port_entering 판정
                    lat_arr = np.array([float(r[5]) for r in rows]) / 10000000.0
                    lon_arr = np.array([float(r[4]) for r in rows]) / 10000000.0
                    lat_idx = np.clip(((lat_arr - depth_lats[0]) / lat_res).astype(int), 0, len(depth_lats) - 1)
                    lon_idx = np.clip(((lon_arr - depth_lons[0]) / lon_res).astype(int), 0, len(depth_lons) - 1)
                    valid = (lat_arr >= depth_lats[0]) & (lat_arr <= depth_lats[-1]) & \
                            (lon_arr >= depth_lons[0]) & (lon_arr <= depth_lons[-1])
                    port_flags = np.zeros(len(rows), dtype=bool)
                    if valid.any():
                        port_flags[valid] = depth_elev[lat_idx[valid], lon_idx[valid]] >= LAND_THRESHOLD

                    # INSERT
                    for i in range(0, len(rows), BATCH_SIZE):
                        batch = rows[i:i + BATCH_SIZE]
                        batch_ports = port_flags[i:i + BATCH_SIZE]
                        values = [row + (bool(pf),) for row, pf in zip(batch, batch_ports)]
                        args_str = ','.join(
                            local_cur.mogrify("(%s,%s,%s,%s,%s,%s,%s,%s,%s)", v).decode() for v in values
                        )
                        local_cur.execute(f"""
                            INSERT INTO {ship_table} (mmsi, datetime, rot, sog, lon, lat, cog, heading, port_entering)
                            VALUES {args_str}
                            ON CONFLICT (mmsi, datetime) DO NOTHING
                        """)
                    local_conn.commit()
                    total_inserted += len(rows)

            except Exception as e:
                local_conn.rollback()
                try:
                    remote_conn.close()
                except:
                    pass
                remote_conn = psycopg2.connect(**REMOTE_DB)

            d = month_end

        remote_conn.close()
        local_conn.close()

        with counter_lock:
            counter.value += 1
            current = counter.value

        return (mmsi, total_inserted, current, total_ships, None)

    except Exception as e:
        if local_conn:
            try: local_conn.close()
            except: pass
        if remote_conn:
            try: remote_conn.close()
            except: pass
        with counter_lock:
            counter.value += 1
            current = counter.value
        return (mmsi, 0, current, total_ships, str(e))


def main():
    parser = argparse.ArgumentParser(description='전체 선박 항적 fetch')
    parser.add_argument('--mmsi', type=int, default=None)
    parser.add_argument('--start', default=START_DATE)
    parser.add_argument('--end', default=END_DATE)
    parser.add_argument('--workers', type=int, default=NUM_WORKERS)
    parser.add_argument('--reverse', action='store_true', help='MMSI 역순으로 실행')
    args = parser.parse_args()

    logging.info("=" * 60)
    logging.info(f"전체 선박 항적 fetch (x{args.workers} 프로세스)")
    logging.info(f"21번 서버 → 171 ship_{{mmsi}}")
    logging.info(f"기간: {args.start} ~ {args.end}")
    logging.info("=" * 60)

    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()

    if args.mmsi:
        mmsi_list = [str(args.mmsi)]
    else:
        cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_shipinfo ORDER BY mmsi")
        mmsi_list = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()

    if args.reverse:
        mmsi_list = mmsi_list[::-1]
        logging.info("역순 실행")

    total = len(mmsi_list)
    logging.info(f"대상: {total}척")
    logging.info("")

    tasks = [(mmsi, args.start, args.end, total) for mmsi in mmsi_list]

    c = Value('i', 0)
    l = Lock()

    total_inserted = 0

    try:
        with Pool(processes=args.workers, initializer=init_worker, initargs=(c, l)) as pool:
            for result in pool.imap_unordered(fetch_ship, tasks):
                mmsi, inserted, current, tot, err = result
                if err:
                    logging.error(f"  [{current}/{tot}] MMSI {mmsi} 오류: {err}")
                else:
                    total_inserted += inserted
                    if inserted > 0:
                        logging.info(f"  [{current}/{tot}] MMSI {mmsi}: {inserted:,}건 | 누적: {total_inserted:,}")
                    else:
                        logging.info(f"  [{current}/{tot}] MMSI {mmsi}: 이미 완료")
    except KeyboardInterrupt:
        logging.info("\n중단됨!")

    logging.info("")
    logging.info("=" * 60)
    logging.info(f"완료! 총 {total_inserted:,}건 fetch")
    logging.info("=" * 60)


if __name__ == '__main__':
    main()
