"""
port_entering 재계산 + 항차번호(voyage_num) 계산 + kfw_ebp_voyage 저장
- 수심 -10m 기준으로 port_entering 재판정
- 30분 이상 port_entering=True 구간을 입항으로 판정
- voyage_num = YY * 1000 + 항차순번 (예: 22001)
- 항적을 1분 단위로 보간하여 KFW/EBP 구역 내 체류 시간 계산
- 선박 단위로 처리, 월별로 나눠서 SELECT
"""

import psycopg2
import numpy as np
import netCDF4 as nc
import shutil
import os
from datetime import timedelta, datetime
from multiprocessing import Pool, Value, Lock
from shapely.geometry import Point, Polygon
from shapely.prepared import prep

LOCAL_DB = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

DEPTH_TEMP = r'C:\temp\depth.nc'
DEPTH_FILE = r'K:\coding_project\해양수산 데이터 분석 플랫폼\수심\depth_450m_s27.0_w119.0_e137.0.nc'
LAND_THRESHOLD = -10
PORT_STAY_THRESHOLD = timedelta(minutes=30)
NUM_WORKERS = 10

# 처리 기간 설정
START_DATE = '2022-01-01'
END_DATE = '2025-01-01'

# KFW/EBP 구역 좌표 (lon, lat)
KFW_COORDS = [(130.23,35.44),(130.20,35.48),(130.25,35.56),(130.29,35.58),
              (130.38,35.58),(130.41,35.55),(130.32,35.49),(130.23,35.44)]
EBP_COORDS = [(130.34,35.42),(130.43,35.47),(130.47,35.43),(130.43,35.37),
              (130.40,35.36),(130.34,35.42)]

KFW_POLY = Polygon(KFW_COORDS)
EBP_POLY = Polygon(EBP_COORDS)

counter = None
counter_lock = None


def init_counter(c, l):
    global counter, counter_lock
    counter = c
    counter_lock = l


def load_depth_data():
    os.makedirs(os.path.dirname(DEPTH_TEMP), exist_ok=True)
    if not os.path.exists(DEPTH_TEMP):
        shutil.copy2(DEPTH_FILE, DEPTH_TEMP)
    ds = nc.Dataset(DEPTH_TEMP)
    lats = np.array(ds.variables['lat'][:])
    lons = np.array(ds.variables['lon'][:])
    elev = np.array(ds.variables['elevation'][:])
    ds.close()
    return lats, lons, elev


def interpolate_1min(times, lons, lats):
    """항적을 1분 단위로 선형 보간 + 속력 계산."""
    if len(times) < 2:
        return times, lons, lats, np.zeros(len(times))

    t0 = times[0]
    t_seconds = np.array([(t - t0).total_seconds() for t in times])

    total_sec = t_seconds[-1]
    if total_sec <= 0:
        return times, lons, lats, np.zeros(len(times))

    t_interp = np.arange(0, total_sec, 60)
    if len(t_interp) == 0:
        return times, lons, lats, np.zeros(len(times))

    lon_interp = np.interp(t_interp, t_seconds, lons)
    lat_interp = np.interp(t_interp, t_seconds, lats)

    sog_interp = np.zeros(len(t_interp))
    for i in range(1, len(t_interp)):
        dlat = lat_interp[i] - lat_interp[i - 1]
        dlon = lon_interp[i] - lon_interp[i - 1]
        lat_mid = (lat_interp[i] + lat_interp[i - 1]) / 2.0
        dist_nm = np.sqrt((dlat * 60) ** 2 + (dlon * 60 * np.cos(np.radians(lat_mid))) ** 2)
        dt_hours = (t_interp[i] - t_interp[i - 1]) / 3600.0
        if dt_hours > 0:
            sog_interp[i] = dist_nm / dt_hours
    sog_interp[0] = sog_interp[1] if len(sog_interp) > 1 else 0

    return t_interp, lon_interp, lat_interp, sog_interp


def calc_zone_duration(lons, lats, sogs, port_flags):
    """보간된 좌표에서 KFW/EBP/total 조업 시간(분) 계산
    - KFW/EBP: 해당 구역 내 + 속력 < 3knots인 점
    - total: 전체 항적 중 속력 < 3knots + 항구 아닌 점
    """
    kfw_prep = prep(KFW_POLY)
    ebp_prep = prep(EBP_POLY)
    kfw_minx, kfw_miny, kfw_maxx, kfw_maxy = KFW_POLY.bounds
    ebp_minx, ebp_miny, ebp_maxx, ebp_maxy = EBP_POLY.bounds

    kfw_minutes = 0
    ebp_minutes = 0
    total_minutes = 0

    for i in range(len(lons)):
        lon, lat, sog = lons[i], lats[i], sogs[i]
        is_port = port_flags[i]

        # total: 속력 < 3knots + 항구 아닌 곳
        if sog < 3 and not is_port:
            total_minutes += 1

        # KFW/EBP: 구역 내 + 속력 < 3knots
        if sog < 3:
            if kfw_minx <= lon <= kfw_maxx and kfw_miny <= lat <= kfw_maxy:
                if kfw_prep.contains(Point(lon, lat)):
                    kfw_minutes += 1
            if ebp_minx <= lon <= ebp_maxx and ebp_miny <= lat <= ebp_maxy:
                if ebp_prep.contains(Point(lon, lat)):
                    ebp_minutes += 1

    return kfw_minutes, ebp_minutes, total_minutes


def generate_months(start_date, end_date):
    """시작~종료 기간의 (year, month) 리스트 생성"""
    months = []
    d = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    while d < end:
        months.append((d.year, d.month))
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 1)
    return months


def process_ship(args):
    """한 선박의 전체 기간 처리 (월별로 나눠서 SELECT, 상태는 메모리 유지)"""
    mmsi, months, total_ships = args

    conn = None
    try:
        depth_lats, depth_lons, depth_elev = load_depth_data()
        lat_res = float(depth_lats[1] - depth_lats[0])
        lon_res = float(depth_lons[1] - depth_lons[0])

        conn = psycopg2.connect(**LOCAL_DB)
        cur = conn.cursor()

        # 선박 전체 상태 (메모리에서 이어감)
        year_count = {}  # {yy: seq}
        current_vnum = None
        prev_in_port = None
        total_records = 0
        total_port_changed = 0
        all_voyage_data = {}  # {vnum: {'times':[], 'lons':[], 'lats':[], 'start':dt, 'end':dt}}

        for year, month in months:
            start_dt = f"{year}-{month:02d}-01"
            if month == 12:
                end_dt = f"{year + 1}-01-01"
            else:
                end_dt = f"{year}-{month + 1:02d}-01"

            # 월별 SELECT (ship_mmsi 테이블)
            ship_table = f"ship_{mmsi}"
            cur.execute(f"""
                SELECT datetime, lon, lat, port_entering, voyage_num
                FROM {ship_table}
                WHERE datetime >= %s AND datetime < %s
                ORDER BY datetime
            """, (start_dt, end_dt))
            records = cur.fetchall()

            if not records:
                continue

            n = len(records)
            total_records += n
            lat_arr = np.array([float(r[2]) for r in records]) / 10000000.0
            lon_arr = np.array([float(r[1]) for r in records]) / 10000000.0
            dt_arr = [r[0] for r in records]

            # port_entering 재계산
            lat_idx = ((lat_arr - depth_lats[0]) / lat_res).astype(int)
            lon_idx = ((lon_arr - depth_lons[0]) / lon_res).astype(int)
            lat_idx = np.clip(lat_idx, 0, len(depth_lats) - 1)
            lon_idx = np.clip(lon_idx, 0, len(depth_lons) - 1)

            valid = (lat_arr >= depth_lats[0]) & (lat_arr <= depth_lats[-1]) & \
                    (lon_arr >= depth_lons[0]) & (lon_arr <= depth_lons[-1])

            new_port = np.zeros(n, dtype=bool)
            if valid.any():
                new_port[valid] = depth_elev[lat_idx[valid], lon_idx[valid]] >= LAND_THRESHOLD

            # 실제 입항 구간 판별 (30분 이상)
            real_port = np.zeros(n, dtype=bool)
            i = 0
            while i < n:
                if new_port[i]:
                    seg_start = i
                    while i < n and new_port[i]:
                        i += 1
                    seg_end = i - 1
                    duration = records[seg_end][0] - records[seg_start][0]
                    if duration >= PORT_STAY_THRESHOLD:
                        real_port[seg_start:seg_end + 1] = True
                else:
                    i += 1

            # 항차번호 부여 (이전 월 상태 이어감)
            voyage_nums = [None] * n

            if prev_in_port is None:
                # 첫 월 첫 레코드
                prev_in_port = bool(real_port[0])
                if not prev_in_port:
                    yy = records[0][0].year % 100
                    year_count[yy] = year_count.get(yy, 0) + 1
                    current_vnum = yy * 1000 + year_count[yy]

            for i in range(n):
                in_port = bool(real_port[i])
                if prev_in_port and not in_port:
                    yy = records[i][0].year % 100
                    year_count[yy] = year_count.get(yy, 0) + 1
                    current_vnum = yy * 1000 + year_count[yy]
                if current_vnum is not None:
                    voyage_nums[i] = current_vnum
                prev_in_port = in_port

            # 임시 테이블 JOIN UPDATE
            updates = []
            for i in range(n):
                p_val = bool(new_port[i])
                v_val = voyage_nums[i]
                old_p = records[i][3] if records[i][3] is not None else False
                old_v = records[i][4]
                if p_val != old_p or v_val != old_v:
                    updates.append((dt_arr[i], p_val, v_val))
                    if p_val != old_p:
                        total_port_changed += 1

            if updates:
                cur.execute("""
                    CREATE TEMP TABLE tmp_vup (
                        dt TIMESTAMP, port BOOLEAN, vnum INTEGER
                    ) ON COMMIT DROP
                """)
                batch_size = 5000
                for bi in range(0, len(updates), batch_size):
                    batch = updates[bi:bi + batch_size]
                    args_str = ','.join(
                        cur.mogrify("(%s,%s,%s)", row).decode() for row in batch
                    )
                    cur.execute("INSERT INTO tmp_vup (dt, port, vnum) VALUES " + args_str)

                cur.execute(f"""
                    UPDATE {ship_table} t
                    SET port_entering = tmp.port, voyage_num = tmp.vnum
                    FROM tmp_vup tmp
                    WHERE t.datetime = tmp.dt
                """)
                conn.commit()
            else:
                conn.commit()

            # voyage 데이터 수집 (나중에 한번에 저장)
            for i in range(n):
                vnum = voyage_nums[i]
                if vnum is None:
                    continue
                if vnum not in all_voyage_data:
                    all_voyage_data[vnum] = {'times': [], 'lons': [], 'lats': [], 'ports': []}
                all_voyage_data[vnum]['times'].append(dt_arr[i])
                all_voyage_data[vnum]['lons'].append(lon_arr[i])
                all_voyage_data[vnum]['lats'].append(lat_arr[i])
                all_voyage_data[vnum]['ports'].append(bool(new_port[i]))

        # 항차별 kfw_ebp_voyage 저장
        voyage_count = len(all_voyage_data)
        for vnum, vdata in all_voyage_data.items():
            v_times = vdata['times']
            v_lons = np.array(vdata['lons'])
            v_lats = np.array(vdata['lats'])
            v_ports = vdata['ports']
            v_start = v_times[0]
            v_end = v_times[-1]

            t_interp, lon_interp, lat_interp, sog_interp = interpolate_1min(v_times, v_lons, v_lats)

            # port_entering도 보간 (가장 가까운 원본 값 사용)
            t0 = v_times[0]
            t_sec_orig = np.array([(t - t0).total_seconds() for t in v_times])
            t_sec_interp = t_interp if isinstance(t_interp, np.ndarray) else np.array(t_interp)
            port_idx = np.searchsorted(t_sec_orig, t_sec_interp, side='right') - 1
            port_idx = np.clip(port_idx, 0, len(v_ports) - 1)
            port_interp = np.array([v_ports[j] for j in port_idx])

            kfw_min, ebp_min, total_min = calc_zone_duration(lon_interp, lat_interp, sog_interp, port_interp)

            for area_name, area_min in [('KFW', kfw_min), ('EBP', ebp_min), ('total', total_min)]:
                cur.execute("""
                    INSERT INTO kfw_ebp_voyage (mmsi, voyage_num, start_time, end_time, target_area, duration)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (mmsi, voyage_num, target_area) DO UPDATE
                    SET start_time = EXCLUDED.start_time,
                        end_time = EXCLUDED.end_time,
                        duration = EXCLUDED.duration
                """, (int(mmsi), vnum, v_start, v_end, area_name, area_min / 60.0))

        conn.commit()
        cur.close()

        with counter_lock:
            counter.value += 1
            current = counter.value

        return (mmsi, total_records, total_port_changed, voyage_count, current, total_ships)

    except Exception as e:
        if conn:
            try: conn.rollback()
            except: pass
        with counter_lock:
            counter.value += 1
            current = counter.value
        return (mmsi, -1, 0, 0, current, total_ships, str(e))

    finally:
        if conn:
            try: conn.close()
            except: pass


def main():
    print("=" * 60)
    print(f"port_entering + 항차 + 구역체류시간 계산 (x{NUM_WORKERS} 프로세스)")
    print(f"기간: {START_DATE} ~ {END_DATE}")
    print(f"LAND_THRESHOLD: {LAND_THRESHOLD}m | PORT_STAY: {PORT_STAY_THRESHOLD}")
    print("=" * 60)

    # MMSI 목록 (이미 처리된 선박 제외)
    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT s.mmsi FROM kfw_ebp_shipinfo s
        WHERE s.mmsi::integer NOT IN (SELECT DISTINCT mmsi FROM kfw_ebp_voyage)
        ORDER BY s.mmsi
    """)
    mmsi_list = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()

    months = generate_months(START_DATE, END_DATE)
    total_ships = len(mmsi_list)

    print(f"대상 선박: {total_ships}척 × {len(months)}개월")
    print()

    # 선박 단위 작업 (월 목록은 공통 전달)
    tasks = [(mmsi, months, total_ships) for mmsi in mmsi_list]

    c = Value('i', 0)
    l = Lock()

    total_records = 0
    total_port_changed = 0
    total_voyages = 0
    error_count = 0

    try:
        with Pool(processes=NUM_WORKERS, initializer=init_counter, initargs=(c, l)) as pool:
            for result in pool.imap_unordered(process_ship, tasks):
                if len(result) == 7:
                    mmsi, cnt, pc, vc, cur_n, total, err = result
                    print(f"  [오류 {cur_n}/{total}] MMSI {mmsi}: {err}")
                    error_count += 1
                else:
                    mmsi, cnt, pc, vc, cur_n, total = result
                    if cnt > 0:
                        total_records += cnt
                        total_port_changed += pc
                        total_voyages += vc
                        print(f"  [{cur_n}/{total}] MMSI {mmsi} → {cnt:,}건, "
                              f"{vc}항차 | 누적: {total_records:,}건 {total_voyages}항차")

    except KeyboardInterrupt:
        print("\n\n중단됨! 지금까지 업데이트된 데이터는 유지됩니다.")

    print()
    print("=" * 60)
    print(f"완료! {total_records:,}건 처리 | port변경: {total_port_changed:,} | "
          f"{total_voyages}항차 | {error_count}건 오류")
    print("=" * 60)


if __name__ == "__main__":
    main()
