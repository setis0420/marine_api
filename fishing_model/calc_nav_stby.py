"""
항해시간(nav_duration) + 대기시간(stby_duration) 계산
- kfw_ebp_voyage의 target_area='total' 항차 대상
- ship_{mmsi}에서 항적 조회 → 1분 보간 → status별 카운트
- nav: model_fishing_status=0 + 항구 아닌 곳
- stby: model_fishing_status=3 + 항구 아닌 곳

사용법:
    python calc_nav_stby.py                    # 전체
    python calc_nav_stby.py --mmsi 440137010   # 특정 선박
    python calc_nav_stby.py --workers 5        # 멀티프로세스
"""

import psycopg2
import numpy as np
import argparse
import logging
import time
from datetime import datetime
from multiprocessing import Pool

DB_CONFIG = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

NUM_WORKERS = 5

log_file = f'calc_nav_stby_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)


def calc_ship(mmsi):
    """한 선박의 모든 항차 nav/stby 계산"""
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        ship_table = f"ship_{mmsi}"

        # 테이블 존재 확인
        cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name=%s)", (ship_table,))
        if not cur.fetchone()[0]:
            return (mmsi, 0, 0, None)

        # total 항차 중 nav_duration이 NULL인 것만
        cur.execute("""
            SELECT voyage_num, start_time, end_time
            FROM kfw_ebp_voyage
            WHERE mmsi = %s AND target_area = 'total' AND nav_duration IS NULL
            ORDER BY voyage_num
        """, (int(mmsi),))
        voyages = cur.fetchall()

        if not voyages:
            return (mmsi, 0, 0, None)

        updated = 0
        for vnum, start_time, end_time in voyages:
            # 항적 조회 (항구 제외)
            cur.execute(f"""
                SELECT datetime, sog, model_fishing_status, port_entering
                FROM {ship_table}
                WHERE datetime >= %s AND datetime <= %s
                ORDER BY datetime
            """, (start_time, end_time))
            rows = cur.fetchall()

            if len(rows) < 2:
                cur.execute("""
                    UPDATE kfw_ebp_voyage SET nav_duration=0, stby_duration=0
                    WHERE mmsi=%s AND voyage_num=%s AND target_area='total'
                """, (int(mmsi), vnum))
                conn.commit()
                updated += 1
                continue

            # 1분 보간
            datetimes = [r[0] for r in rows]
            statuses = [r[2] for r in rows]
            ports = [r[3] for r in rows]

            t0 = datetimes[0]
            t_sec = np.array([(t - t0).total_seconds() for t in datetimes])
            total_sec = t_sec[-1]

            if total_sec <= 60:
                cur.execute("""
                    UPDATE kfw_ebp_voyage SET nav_duration=0, stby_duration=0
                    WHERE mmsi=%s AND voyage_num=%s AND target_area='total'
                """, (int(mmsi), vnum))
                conn.commit()
                updated += 1
                continue

            t_interp = np.arange(0, total_sec, 60)
            if len(t_interp) == 0:
                continue

            # status와 port는 가장 가까운 원본 값 사용
            indices = np.clip(np.searchsorted(t_sec, t_interp, side='right') - 1, 0, len(rows) - 1)

            nav_min = 0
            stby_min = 0

            for idx in indices:
                status = statuses[idx]
                is_port = ports[idx]

                if is_port:
                    continue  # 항구 제외

                if status == 0:
                    nav_min += 1
                elif status == 3:
                    stby_min += 1

            # UPDATE
            cur.execute("""
                UPDATE kfw_ebp_voyage SET nav_duration=%s, stby_duration=%s
                WHERE mmsi=%s AND voyage_num=%s AND target_area='total'
            """, (nav_min / 60.0, stby_min / 60.0, int(mmsi), vnum))
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
    parser = argparse.ArgumentParser(description='항해/대기 시간 계산')
    parser.add_argument('--mmsi', type=int, default=None)
    parser.add_argument('--workers', type=int, default=NUM_WORKERS)
    args = parser.parse_args()

    logging.info("=" * 60)
    logging.info(f"항해/대기 시간 계산 (x{args.workers} 프로세스)")
    logging.info("nav: status=0 + 항구제외 | stby: status=3 + 항구제외")
    logging.info("=" * 60)

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    if args.mmsi:
        mmsi_list = [str(args.mmsi)]
    else:
        cur.execute("""
            SELECT DISTINCT mmsi FROM kfw_ebp_voyage
            WHERE target_area='total' AND nav_duration IS NULL
            ORDER BY mmsi
        """)
        mmsi_list = [r[0] for r in cur.fetchall()]

    cur.close()
    conn.close()

    logging.info(f"대상: {len(mmsi_list)}척")
    logging.info("")

    total_updated = 0
    completed = 0

    try:
        with Pool(processes=args.workers) as pool:
            for result in pool.imap_unordered(calc_ship, mmsi_list):
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
