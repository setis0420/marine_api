"""
kfw_ebp_trjdata를 MMSI별 개별 테이블로 분리 (멀티프로세스)
- ship_440000640, ship_440004950, ... 형태

사용법:
    python split_by_ship.py                    # 전체
    python split_by_ship.py --mmsi 440000640   # 특정 선박
"""

import psycopg2
import argparse
import logging
import time
from datetime import datetime
from multiprocessing import Pool, Value, Lock

DB_CONFIG = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

NUM_WORKERS = 10

log_file = f'split_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
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

def init_counter(c, l):
    global counter, counter_lock
    counter = c
    counter_lock = l


def split_ship(args):
    mmsi, total = args
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        table_name = f"ship_{mmsi}"

        # 이미 존재하면 스킵
        cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)", (table_name,))
        if cur.fetchone()[0]:
            cur.execute(f"SELECT COUNT(*) FROM {table_name}")
            cnt = cur.fetchone()[0]
            if cnt > 0:
                with counter_lock:
                    counter.value += 1
                    c = counter.value
                return (mmsi, cnt, True, c, total)

        # 테이블 생성 + 데이터 복사
        cur.execute(f"DROP TABLE IF EXISTS {table_name}")
        cur.execute(f"CREATE TABLE {table_name} AS SELECT * FROM kfw_ebp_trjdata WHERE mmsi = %s", (int(mmsi),))
        cur.execute(f"CREATE INDEX ON {table_name} (datetime)")
        cur.execute(f"CREATE UNIQUE INDEX ON {table_name} (mmsi, datetime)")
        cur.execute(f"CREATE INDEX ON {table_name} (voyage_num)")
        conn.commit()

        cur.execute(f"SELECT COUNT(*) FROM {table_name}")
        cnt = cur.fetchone()[0]

        with counter_lock:
            counter.value += 1
            c = counter.value
        return (mmsi, cnt, False, c, total)

    except Exception as e:
        if conn:
            conn.rollback()
        with counter_lock:
            counter.value += 1
            c = counter.value
        return (mmsi, -1, False, c, total, str(e))
    finally:
        if conn:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description='MMSI별 테이블 분리')
    parser.add_argument('--mmsi', type=int, default=None)
    args = parser.parse_args()

    logging.info("=" * 60)
    logging.info(f"MMSI별 테이블 분리 (x{NUM_WORKERS} 프로세스)")
    logging.info(f"DB: {DB_CONFIG['host']}")
    logging.info("=" * 60)

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    if args.mmsi:
        mmsi_list = [str(args.mmsi)]
    else:
        cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_shipinfo ORDER BY mmsi")
        mmsi_list = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()

    total = len(mmsi_list)
    logging.info(f"대상: {total}척")
    logging.info("")

    tasks = [(mmsi, total) for mmsi in mmsi_list]
    c = Value('i', 0)
    l = Lock()

    total_records = 0
    total_created = 0

    try:
        with Pool(processes=NUM_WORKERS, initializer=init_counter, initargs=(c, l)) as pool:
            for result in pool.imap_unordered(split_ship, tasks):
                if len(result) == 6:
                    mmsi, cnt, skip, cur_n, tot, err = result
                    logging.error(f"  [{cur_n}/{tot}] ship_{mmsi} 오류: {err}")
                else:
                    mmsi, cnt, skip, cur_n, tot = result
                    total_records += cnt
                    if skip:
                        logging.info(f"  [{cur_n}/{tot}] ship_{mmsi}: {cnt:,}건 (스킵)")
                    else:
                        total_created += 1
                        logging.info(f"  [{cur_n}/{tot}] ship_{mmsi}: {cnt:,}건 생성 | 누적: {total_records:,}건")
    except KeyboardInterrupt:
        logging.info("\n중단됨!")

    logging.info("")
    logging.info("=" * 60)
    logging.info(f"완료! {total_created}개 생성 | 총 {total_records:,}건")
    logging.info("=" * 60)


if __name__ == '__main__':
    main()
