"""
Rule-based 조업 판별 프로그램
- 대상: 근해자망어업, 연안자망어업, 기타통발어업
- 규칙: SOG < 0.5 knots → 조업(1), 4시간 이상 지속 → 대기(3)
- 항구(port_entering=True) 제외

model_fishing_status:
  0: 비조업 (항해 중)
  1: 조업 (SOG < 0.5, 4시간 미만)
  3: 대기 (SOG < 0.5, 4시간 이상 지속)

model_fishing_type:
  선박의 실제 업종 코드 사용

사용법:
    python rule_based_classify.py --all
    python rule_based_classify.py --mmsi 440137010
    python rule_based_classify.py --all --workers 5
"""

import os
import sys
import argparse
import logging
import time
import numpy as np
import psycopg2
from datetime import datetime, timedelta
from multiprocessing import Pool

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

# === DB 설정 ===
DB_CONFIG = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

# === 설정 ===
SOG_THRESHOLD = 5       # 0.5 knots (DB에 10배로 저장)
STANDBY_HOURS = 4       # 대기 판정 시간
MODEL_NAME = 'spd_lmt_0_5knot_4hour'
NUM_WORKERS = 5

# 업종 → model_fishing_type 매핑
BIZ_TYPE_MAP = {
    '근해자망어업': 4,      # 자망
    '근해자망': 4,
    '연안자망어업': 4,      # 자망
    '연안자망': 4,
    '기타통발어업': 6,      # 통발
    '기타통발': 6,
}

# 대상 업종 키워드
TARGET_BIZ = ['근해자망', '연안자망', '기타통발']

# 로그
log_file = f'rule_based_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)


def get_target_ships():
    """대상 선박 목록 조회 (근해자망, 연안자망, 기타통발)"""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        SELECT mmsi, vessel_name, business_type
        FROM kfw_ebp_shipinfo
        WHERE business_type LIKE '%자망%' OR business_type LIKE '%통발%'
        ORDER BY mmsi
    """)
    ships = []
    for mmsi, name, biz in cur.fetchall():
        # 업종에서 fishing_type 코드 결정
        fishing_type = None
        for keyword, code in BIZ_TYPE_MAP.items():
            if keyword in (biz or ''):
                fishing_type = code
                break
        if fishing_type is not None:
            ships.append((mmsi, name, biz, fishing_type))

    cur.close()
    conn.close()
    return ships


def classify_ship(args):
    """한 선박의 rule-based 조업 판별"""
    mmsi, vessel_name, biz_type, fishing_type_code = args

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        ship_table = f"ship_{mmsi}"

        # 테이블 존재 확인
        cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)", (ship_table,))
        if not cur.fetchone()[0]:
            return (mmsi, 0, 0, 0, None)

        # 전체 항차 조회 (기존 것도 포함)
        cur.execute("""
            SELECT voyage_num, start_time, end_time
            FROM kfw_ebp_voyage
            WHERE mmsi = %s AND target_area = 'total'
            ORDER BY voyage_num
        """, (int(mmsi),))
        voyage_list = cur.fetchall()

        if not voyage_list:
            return (mmsi, 0, 0, 0, None)

        total_updated = 0
        total_voyages = 0

        for vnum, v_start, v_end in voyage_list:
            # 항적 데이터 조회 (항구 제외)
            cur.execute(f"""
                SELECT datetime, sog, port_entering
                FROM {ship_table}
                WHERE datetime >= %s AND datetime <= %s
                ORDER BY datetime
            """, (v_start, v_end))
            rows = cur.fetchall()

            if not rows:
                cur.execute("""
                    UPDATE kfw_ebp_voyage SET model=%s
                    WHERE mmsi=%s AND voyage_num=%s AND target_area='total'
                """, (MODEL_NAME, int(mmsi), vnum))
                conn.commit()
                continue

            # 조업 판별
            datetimes = [r[0] for r in rows]
            sogs = [r[1] for r in rows]  # DB 원본 (10배)
            ports = [r[2] for r in rows]

            # 초기값: 비조업(0)
            statuses = [0] * len(rows)

            # 1단계: 항구 아닌 곳에서 SOG < 0.5 (DB값 < 5) → 조업(1)
            for i in range(len(rows)):
                if not ports[i] and sogs[i] < SOG_THRESHOLD:
                    statuses[i] = 1  # 조업

            # 2단계: 연속 조업 구간 중 4시간 이상 → 대기(3)
            i = 0
            while i < len(rows):
                if statuses[i] == 1:
                    seg_start = i
                    while i < len(rows) and statuses[i] == 1:
                        i += 1
                    seg_end = i - 1

                    # 구간 지속 시간
                    duration = datetimes[seg_end] - datetimes[seg_start]
                    if duration >= timedelta(hours=STANDBY_HOURS):
                        # 4시간 이상 → 전체 구간을 대기(3)로
                        for j in range(seg_start, seg_end + 1):
                            statuses[j] = 3
                else:
                    i += 1

            # DB UPDATE (임시 테이블)
            updates = []
            for i in range(len(rows)):
                updates.append((datetimes[i], fishing_type_code, statuses[i]))

            if updates:
                cur.execute("CREATE TEMP TABLE tmp_rb (dt TIMESTAMP, ft SMALLINT, fs SMALLINT) ON COMMIT DROP")
                for bi in range(0, len(updates), 5000):
                    batch = updates[bi:bi + 5000]
                    args_str = ','.join(cur.mogrify("(%s,%s,%s)", r).decode() for r in batch)
                    cur.execute("INSERT INTO tmp_rb VALUES " + args_str)
                cur.execute(f"""
                    UPDATE {ship_table} t
                    SET model_fishing_type = tmp.ft, model_fishing_status = tmp.fs
                    FROM tmp_rb tmp WHERE t.datetime = tmp.dt
                """)
                conn.commit()
                total_updated += len(updates)

            # voyage model 마킹
            cur.execute("""
                UPDATE kfw_ebp_voyage SET model = %s
                WHERE mmsi = %s AND voyage_num = %s AND target_area = 'total'
            """, (MODEL_NAME, int(mmsi), vnum))
            conn.commit()
            total_voyages += 1

        conn.close()
        return (mmsi, total_voyages, total_updated, fishing_type_code, None)

    except Exception as e:
        if conn:
            try:
                conn.close()
            except:
                pass
        return (mmsi, 0, 0, 0, str(e))


def main():
    parser = argparse.ArgumentParser(description='Rule-based 조업 판별')
    parser.add_argument('--mmsi', type=int, default=None)
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--workers', type=int, default=NUM_WORKERS)
    args = parser.parse_args()

    type_names = {4: '자망', 6: '통발'}

    logging.info("=" * 60)
    logging.info(f"Rule-based 조업 판별 (x{args.workers} 프로세스)")
    logging.info(f"규칙: SOG < 0.5knot → 조업, 4시간 이상 → 대기")
    logging.info(f"model: {MODEL_NAME}")
    logging.info("=" * 60)

    # 대상 선박
    all_ships = get_target_ships()
    logging.info(f"대상 업종 선박: {len(all_ships)}척")

    # 업종별 통계
    from collections import Counter
    biz_counts = Counter(s[2] for s in all_ships)
    for biz, cnt in biz_counts.most_common():
        logging.info(f"  {biz}: {cnt}척")
    logging.info("")

    if args.mmsi:
        tasks = [s for s in all_ships if str(s[0]) == str(args.mmsi)]
        if not tasks:
            logging.error(f"MMSI {args.mmsi}는 대상 업종이 아닙니다")
            return
    else:
        tasks = all_ships

    # 멀티프로세스 실행
    total_updated = 0
    total_voyages = 0
    completed = 0

    with Pool(processes=args.workers) as pool:
        for result in pool.imap_unordered(classify_ship, tasks):
            mmsi, voyages, updated, ft_code, err = result
            completed += 1
            if err:
                logging.error(f"  [{completed}/{len(tasks)}] MMSI {mmsi} 오류: {err}")
            elif updated > 0:
                total_updated += updated
                total_voyages += voyages
                logging.info(f"  [{completed}/{len(tasks)}] MMSI {mmsi}: "
                             f"{voyages}항차, {updated:,}건 → 유형={ft_code}({type_names.get(ft_code, '?')}) | "
                             f"누적: {total_updated:,}건")

    logging.info("")
    logging.info("=" * 60)
    logging.info(f"완료! {total_voyages}항차 | {total_updated:,}건 분류")
    logging.info("=" * 60)


if __name__ == '__main__':
    main()
