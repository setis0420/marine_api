"""
KFW/EBP 영역 nav_duration/stby_duration 계산
- kfw_ebp_voyage의 target_area='KFW' 및 'EBP' 항차 대상
- ship_{mmsi}에서 해당 항차 시간범위의 항적 조회 → 1분 보간
- KFW/EBP 폴리곤 내부의 점만 계산
  * nav_duration: 폴리곤 내부 + model_fishing_status=0 + 항구제외
  * stby_duration: 폴리곤 내부 + model_fishing_status=3 + 항구제외
- duration 컬럼은 건드리지 않음 (step2가 이미 계산)
- 멱등성: nav_duration IS NULL 행만 처리

사용법:
    python calc_kfw_ebp_duration.py                  # 전체 선박 (미처리만)
    python calc_kfw_ebp_duration.py --mmsi 440137010 # 특정 선박
    python calc_kfw_ebp_duration.py --workers 5      # 멀티프로세스
    python calc_kfw_ebp_duration.py --force          # nav_duration 상관없이 전체 재계산
"""

import psycopg2
import numpy as np
import argparse
import logging
import time
from datetime import datetime
from multiprocessing import Pool
from shapely.geometry import Point, Polygon
from shapely.prepared import prep

DB_CONFIG = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

NUM_WORKERS = 5

# process_voyage_model.py 와 동일한 좌표
KFW_COORDS = [(130.23,35.44),(130.20,35.48),(130.25,35.56),(130.29,35.58),
              (130.38,35.58),(130.41,35.55),(130.32,35.49),(130.23,35.44)]
EBP_COORDS = [(130.34,35.42),(130.43,35.47),(130.47,35.43),(130.43,35.37),
              (130.40,35.36),(130.34,35.42)]
KFW_POLY = Polygon(KFW_COORDS)
EBP_POLY = Polygon(EBP_COORDS)
KFW_PREP = prep(KFW_POLY)
EBP_PREP = prep(EBP_POLY)
KFW_BOUNDS = KFW_POLY.bounds  # (minx, miny, maxx, maxy)
EBP_BOUNDS = EBP_POLY.bounds

# 원본 관측점 간 간격이 이 값(초) 이상이면 "AIS 송신 끊김"으로 보고 해당 구간 제외
GAP_THRESHOLD_SEC = 10 * 60  # 10분

log_file = f'calc_kfw_ebp_duration_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)


def in_bounds(lo, la, bounds):
    return bounds[0] <= lo <= bounds[2] and bounds[1] <= la <= bounds[3]


def calc_voyage(rows):
    """한 항차의 KFW/EBP nav/stby 계산 (분 단위)
    rows: list of (datetime, lon, lat, status, port)  — DB 스케일
    반환: (kfw_nav, kfw_stby, ebp_nav, ebp_stby) 모두 분
    """
    if len(rows) < 2:
        return 0, 0, 0, 0

    datetimes = [r[0] for r in rows]
    lons = np.array([r[1] / 10000000.0 for r in rows])  # DB → 실좌표
    lats = np.array([r[2] / 10000000.0 for r in rows])
    statuses = np.array([r[3] if r[3] is not None else 0 for r in rows])
    ports = np.array([r[4] for r in rows])

    t0 = datetimes[0]
    t_sec = np.array([(t - t0).total_seconds() for t in datetimes])
    total_sec = t_sec[-1]
    if total_sec <= 60:
        return 0, 0, 0, 0

    # 좌표는 선형 보간, status/port는 최근접(직전값)
    t_interp = np.arange(0, total_sec, 60)
    if len(t_interp) == 0:
        return 0, 0, 0, 0

    lon_i = np.interp(t_interp, t_sec, lons)
    lat_i = np.interp(t_interp, t_sec, lats)
    idx_nn = np.clip(np.searchsorted(t_sec, t_interp, side='right') - 1, 0, len(rows) - 1)
    status_i = statuses[idx_nn]
    port_i = ports[idx_nn]

    # 갭 마스크: 각 보간점의 "직전 관측점 → 다음 관측점" 간격이
    # GAP_THRESHOLD_SEC 초과면 해당 보간점은 신뢰할 수 없다고 판단하여 제외
    # (AIS 송신이 끊긴 구간)
    gap_valid = np.ones(len(t_interp), dtype=bool)
    for j in range(len(t_interp)):
        i_prev = idx_nn[j]
        if i_prev + 1 < len(rows):
            gap = t_sec[i_prev + 1] - t_sec[i_prev]
            if gap > GAP_THRESHOLD_SEC:
                gap_valid[j] = False

    kfw_nav = kfw_stby = 0
    ebp_nav = ebp_stby = 0

    for j in range(len(t_interp)):
        if not gap_valid[j]:
            continue  # AIS 갭 구간 제외
        lo, la, st, pt = lon_i[j], lat_i[j], status_i[j], port_i[j]
        if pt:
            continue  # 항구 제외

        in_kfw = in_bounds(lo, la, KFW_BOUNDS) and KFW_PREP.contains(Point(lo, la))
        in_ebp = False
        if not in_kfw:
            in_ebp = in_bounds(lo, la, EBP_BOUNDS) and EBP_PREP.contains(Point(lo, la))

        if in_kfw:
            if st == 0:
                kfw_nav += 1
            elif st == 3:
                kfw_stby += 1
        elif in_ebp:
            if st == 0:
                ebp_nav += 1
            elif st == 3:
                ebp_stby += 1

    return kfw_nav, kfw_stby, ebp_nav, ebp_stby


def process_ship(args):
    mmsi, force = args
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        ship_table = f"ship_{mmsi}"

        cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name=%s)", (ship_table,))
        if not cur.fetchone()[0]:
            return (mmsi, 0, 0, None)

        # KFW 행 중 대상 voyage_num 조회 (KFW/EBP는 한 항차에 쌍으로 존재)
        if force:
            cur.execute("""
                SELECT DISTINCT voyage_num, start_time, end_time FROM kfw_ebp_voyage
                WHERE mmsi=%s AND target_area='KFW'
                ORDER BY voyage_num
            """, (int(mmsi),))
        else:
            cur.execute("""
                SELECT DISTINCT voyage_num, start_time, end_time FROM kfw_ebp_voyage
                WHERE mmsi=%s AND target_area='KFW' AND nav_duration IS NULL
                ORDER BY voyage_num
            """, (int(mmsi),))
        voyages = cur.fetchall()
        if not voyages:
            return (mmsi, 0, 0, None)

        updated = 0
        for vnum, v_start, v_end in voyages:
            cur.execute(f"""
                SELECT datetime, lon, lat, model_fishing_status, port_entering
                FROM {ship_table}
                WHERE datetime >= %s AND datetime <= %s
                ORDER BY datetime
            """, (v_start, v_end))
            rows = cur.fetchall()

            kfw_n, kfw_s, ebp_n, ebp_s = calc_voyage(rows)

            cur.execute("""
                UPDATE kfw_ebp_voyage
                SET nav_duration=%s, stby_duration=%s
                WHERE mmsi=%s AND voyage_num=%s AND target_area='KFW'
            """, (kfw_n / 60.0, kfw_s / 60.0, int(mmsi), vnum))

            cur.execute("""
                UPDATE kfw_ebp_voyage
                SET nav_duration=%s, stby_duration=%s
                WHERE mmsi=%s AND voyage_num=%s AND target_area='EBP'
            """, (ebp_n / 60.0, ebp_s / 60.0, int(mmsi), vnum))

            conn.commit()
            updated += 1

        conn.close()
        return (mmsi, len(voyages), updated, None)

    except Exception as e:
        if conn:
            try: conn.close()
            except: pass
        return (mmsi, 0, 0, str(e))


def main():
    parser = argparse.ArgumentParser(description='KFW/EBP 영역 duration/nav/stby 계산')
    parser.add_argument('--mmsi', type=int, default=None)
    parser.add_argument('--workers', type=int, default=NUM_WORKERS)
    parser.add_argument('--force', action='store_true', help='이미 계산된 값도 다시 계산')
    args = parser.parse_args()

    logging.info("=" * 60)
    logging.info(f"KFW/EBP duration/nav/stby 계산 (x{args.workers} 프로세스)")
    logging.info(f"force={args.force}")
    logging.info("=" * 60)

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    if args.mmsi:
        mmsi_list = [str(args.mmsi)]
    else:
        if args.force:
            cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_voyage WHERE target_area='KFW' ORDER BY mmsi")
        else:
            cur.execute("""
                SELECT DISTINCT mmsi FROM kfw_ebp_voyage
                WHERE target_area='KFW' AND nav_duration IS NULL
                ORDER BY mmsi
            """)
        mmsi_list = [r[0] for r in cur.fetchall()]

    cur.close()
    conn.close()

    logging.info(f"대상: {len(mmsi_list)}척")
    logging.info("")

    total_updated = 0
    completed = 0
    work = [(m, args.force) for m in mmsi_list]

    try:
        with Pool(processes=args.workers) as pool:
            for result in pool.imap_unordered(process_ship, work):
                mmsi, voyages, updated, err = result
                completed += 1
                if err:
                    logging.error(f"  [{completed}/{len(mmsi_list)}] MMSI {mmsi} 오류: {err}")
                elif updated > 0:
                    total_updated += updated
                    logging.info(f"  [{completed}/{len(mmsi_list)}] MMSI {mmsi}: {updated}항차 | 누적: {total_updated}")
    except KeyboardInterrupt:
        logging.info("\n중단됨!")

    logging.info("")
    logging.info(f"완료! {total_updated}개 항차 처리")


if __name__ == '__main__':
    main()
