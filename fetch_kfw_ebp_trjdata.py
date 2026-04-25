"""
KFW/EBP 선박 항적 데이터 추출 스크립트 (멀티프로세스)
- kfw_ebp_shipinfo에서 MMSI 목록 조회
- 21번 서버 aisdb에서 항적 데이터 추출
- 로컬 marine DB kfw_ebp_trjdata에 저장
"""

import psycopg2
import sys
import shutil
import os
import numpy as np
import netCDF4 as nc
from datetime import datetime
from multiprocessing import Pool, Value, Lock, current_process

# === 설정 ===
REMOTE_DB = {
    'host': '203.253.202.21',
    'dbname': 'aisdb',
    'user': 'fishery_readonly_2',
    'password': 'readonly',
    'port': '5432'
}

LOCAL_DB = {
    'host': 'localhost',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420!',
    'port': '5432'
}

# 추출 대상 기간 (년, 월) - 필요시 수정
TARGET_PERIODS = [
    (2022, 1),
    (2022, 2),
    (2022, 3),
    (2022, 4),
    (2022, 5),
    (2022, 6),
    (2022, 7),
    (2022, 8),
    (2022, 9),
    (2022, 10),
    (2022, 11),
    (2022, 12),
    (2023, 1),
    (2023, 2),
    (2023, 3),
    (2023, 4),
    (2023, 5),
    (2023, 6),
    (2023, 7),
    (2023, 8),
    (2023, 9),
    (2023, 10),
    (2023, 11),
    (2023, 12),
    (2024, 1),
    (2024, 2),
    (2024, 3),
    (2024, 4),
    (2024, 5),
    (2024, 6),
    (2024, 7),
    (2024, 8),
    (2024, 9),
    (2024, 10),
    (2024, 11),
    (2024, 12),
]

BATCH_SIZE = 5000   # DB 삽입 배치 크기
NUM_WORKERS = 10     # 동시 프로세스 수 (조절 가능)

# 수심 파일 경로 (한글 경로 문제로 임시 복사하여 사용)
DEPTH_FILE = r'K:\coding_project\해양수산 데이터 분석 플랫폼\수심\depth_450m_s27.0_w119.0_e137.0.nc'
DEPTH_TEMP = r'C:\temp\depth.nc'
LAND_THRESHOLD = -10  # 수심 -10m 이상이면 육지(항구) 판정

# 프로세스 간 공유 카운터
counter = None
counter_lock = None


def init_counter(c, l):
    """멀티프로세스 초기화: 공유 카운터 설정"""
    global counter, counter_lock
    counter = c
    counter_lock = l


def load_depth_data():
    """수심 데이터를 로드하여 (lats, lons, elevation) 반환"""
    os.makedirs(os.path.dirname(DEPTH_TEMP), exist_ok=True)
    if not os.path.exists(DEPTH_TEMP):
        shutil.copy2(DEPTH_FILE, DEPTH_TEMP)

    ds = nc.Dataset(DEPTH_TEMP)
    lats = np.array(ds.variables['lat'][:])
    lons = np.array(ds.variables['lon'][:])
    elev = np.array(ds.variables['elevation'][:])
    ds.close()
    return lats, lons, elev


def is_port_batch(lat_arr, lon_arr, depth_lats, depth_lons, depth_elev):
    """배열 단위로 port_entering 판정 (벡터화)"""
    lat = lat_arr / 10000000.0
    lon = lon_arr / 10000000.0

    lat_res = depth_lats[1] - depth_lats[0]
    lon_res = depth_lons[1] - depth_lons[0]

    lat_idx = ((lat - depth_lats[0]) / lat_res).astype(int)
    lon_idx = ((lon - depth_lons[0]) / lon_res).astype(int)

    lat_idx = np.clip(lat_idx, 0, len(depth_lats) - 1)
    lon_idx = np.clip(lon_idx, 0, len(depth_lons) - 1)

    # 범위 밖은 False
    valid = (lat >= depth_lats[0]) & (lat <= depth_lats[-1]) & \
            (lon >= depth_lons[0]) & (lon <= depth_lons[-1])

    result = np.zeros(len(lat), dtype=bool)
    if valid.any():
        result[valid] = depth_elev[lat_idx[valid], lon_idx[valid]] >= LAND_THRESHOLD

    return result


def process_task(args):
    """워커 프로세스: (mmsi, year, month) 하나를 처리"""
    mmsi, year, month, total_tasks = args

    table = f"db{year}{month:02d}"
    start_date = f"{year}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1}-01-01"
    else:
        end_date = f"{year}-{month + 1:02d}-01"

    remote_conn = None
    local_conn = None
    try:
        # 수심 데이터 로드 (프로세스별 1회)
        depth_lats, depth_lons, depth_elev = load_depth_data()

        # DB 연결
        remote_conn = psycopg2.connect(**REMOTE_DB)
        local_conn = psycopg2.connect(**LOCAL_DB)

        # 21번 서버에서 조회
        remote_cur = remote_conn.cursor()
        remote_cur.execute(f"""
            SELECT DISTINCT mmsi, datetime, rot, sog, lon, lat, cog, heading
            FROM {table}
            WHERE mmsi = %s
              AND datetime >= %s AND datetime < %s
              AND lon <> 0 AND lat <> 0
            ORDER BY datetime
        """, (int(mmsi), start_date, end_date))

        rows = remote_cur.fetchall()
        remote_cur.close()
        remote_conn.close()
        remote_conn = None

        if not rows:
            with counter_lock:
                counter.value += 1
                current = counter.value
            return (mmsi, year, month, 0, current, total_tasks)

        # port_entering 판정 (벡터화)
        lat_arr = np.array([r[5] for r in rows])
        lon_arr = np.array([r[4] for r in rows])
        port_flags = is_port_batch(lat_arr, lon_arr, depth_lats, depth_lons, depth_elev)

        # 로컬 DB에 삽입
        local_cur = local_conn.cursor()
        for i in range(0, len(rows), BATCH_SIZE):
            batch_rows = rows[i:i + BATCH_SIZE]
            batch_flags = port_flags[i:i + BATCH_SIZE]

            values = []
            for row, flag in zip(batch_rows, batch_flags):
                values.append(row + (bool(flag),))

            args_str = ','.join(
                local_cur.mogrify("(%s,%s,%s,%s,%s,%s,%s,%s,%s)", v).decode()
                for v in values
            )
            local_cur.execute(
                "INSERT INTO kfw_ebp_trjdata (mmsi, datetime, rot, sog, lon, lat, cog, heading, port_entering) VALUES "
                + args_str + " ON CONFLICT (mmsi, datetime) DO NOTHING"
            )

        local_conn.commit()
        local_cur.close()
        local_conn.close()
        local_conn = None

        with counter_lock:
            counter.value += 1
            current = counter.value

        return (mmsi, year, month, len(rows), current, total_tasks)

    except Exception as e:
        with counter_lock:
            counter.value += 1
            current = counter.value
        return (mmsi, year, month, -1, current, total_tasks, str(e))

    finally:
        if remote_conn:
            try: remote_conn.close()
            except: pass
        if local_conn:
            try: local_conn.close()
            except: pass


def get_mmsi_list():
    """로컬 DB에서 KFW/EBP 선박 MMSI 목록 조회"""
    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_shipinfo ORDER BY mmsi")
    mmsi_list = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return mmsi_list


def get_already_done(mmsi_list):
    """이미 데이터가 있는 (mmsi, year-month) 조합 확인"""
    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT mmsi, TO_CHAR(datetime, 'YYYYMM') as ym
        FROM kfw_ebp_trjdata
        WHERE mmsi = ANY(%s)
        GROUP BY mmsi, TO_CHAR(datetime, 'YYYYMM')
    """, ([int(m) for m in mmsi_list],))
    done = set()
    for row in cur.fetchall():
        done.add((str(row[0]), row[1]))
    cur.close()
    conn.close()
    return done


def main():
    print("=" * 60)
    print(f"KFW/EBP 선박 항적 데이터 추출 (멀티프로세스 x{NUM_WORKERS})")
    print("=" * 60)

    # 1. 수심 파일 사전 복사
    os.makedirs(os.path.dirname(DEPTH_TEMP), exist_ok=True)
    if not os.path.exists(DEPTH_TEMP):
        shutil.copy2(DEPTH_FILE, DEPTH_TEMP)
    print("수심 데이터 준비 완료")

    # 2. MMSI 목록 조회
    mmsi_list = get_mmsi_list()
    print(f"대상 선박: {len(mmsi_list)}척")
    print(f"대상 기간: {len(TARGET_PERIODS)}개월")

    # 3. 이미 완료된 작업 확인
    already_done = get_already_done(mmsi_list)
    print(f"이미 완료된 작업: {len(already_done)}건 (스킵)")

    # 4. 작업 목록 생성 (스킵 제외)
    tasks = []
    for year, month in TARGET_PERIODS:
        ym = f"{year}{month:02d}"
        for mmsi in mmsi_list:
            if (mmsi, ym) not in already_done:
                tasks.append((mmsi, year, month))

    total_tasks = len(tasks)
    tasks = [(mmsi, year, month, total_tasks) for mmsi, year, month in tasks]
    print(f"실제 처리할 작업: {total_tasks}건")
    print()

    if total_tasks == 0:
        print("모든 작업이 완료되었습니다.")
        return

    # 5. 멀티프로세스 실행
    c = Value('i', 0)
    l = Lock()

    total_records = 0
    error_count = 0
    ship_count = 0

    try:
        with Pool(processes=NUM_WORKERS, initializer=init_counter, initargs=(c, l)) as pool:
            for result in pool.imap_unordered(process_task, tasks):
                if len(result) == 7:
                    # 오류
                    mmsi, year, month, count, current, total, err = result
                    print(f"  [오류 {current}/{total}] MMSI {mmsi}, {year}-{month:02d}: {err}")
                    error_count += 1
                else:
                    mmsi, year, month, count, current, total = result
                    if count > 0:
                        total_records += count
                        ship_count += 1
                        print(f"  [{current}/{total}] MMSI {mmsi} {year}-{month:02d} → {count:,}건 "
                              f"| 완료: {ship_count}척 {total_records:,}건")

    except KeyboardInterrupt:
        print("\n\n중단됨! 지금까지 저장된 데이터는 유지됩니다.")

    print()
    print("=" * 60)
    print(f"완료! 총 {total_records:,}건 저장 | {ship_count}건 성공 | {error_count}건 오류")
    print("=" * 60)


if __name__ == "__main__":
    main()
