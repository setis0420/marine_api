"""
선박별 항차 정보 CSV 추출
- kfw_ebp_voyage 테이블에서 total/KFW/EBP 정보를 합쳐서
  선박별 CSV 파일로 저장

출력 경로: H:\\Dropbox\\파일전송\\어업피해조사\\결과이미지\\{mmsi}_{선박명}\\{mmsi}_{선박명}_항차정보.csv

사용법:
    python export_voyage_csv.py                  # 전체
    python export_voyage_csv.py --mmsi 440137010 # 특정 선박
"""

import os
import psycopg2
import pandas as pd
import argparse
import logging
from datetime import datetime

DB_CONFIG = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

SAVE_ROOT = r'H:\Dropbox\파일전송\어업피해조사\결과이미지'

log_file = f'export_voyage_csv_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)


def export_ship(mmsi, vessel_name, conn):
    cur = conn.cursor()

    cur.execute("""
        SELECT voyage_num, target_area, start_time, end_time,
               duration, nav_duration, stby_duration
        FROM kfw_ebp_voyage
        WHERE mmsi = %s
        ORDER BY voyage_num, target_area
    """, (int(mmsi),))
    rows = cur.fetchall()

    if not rows:
        return 0

    # target_area별로 분리
    total_map = {}  # voyage_num -> row
    kfw_map = {}
    ebp_map = {}

    for vnum, area, start, end, dur, nav, stby in rows:
        d = {
            'start_time': start,
            'end_time': end,
            'duration': dur or 0,
            'nav_duration': nav or 0,
            'stby_duration': stby or 0,
        }
        if area == 'total':
            total_map[vnum] = d
        elif area == 'KFW':
            kfw_map[vnum] = d
        elif area == 'EBP':
            ebp_map[vnum] = d

    # voyage_num 기준으로 병합
    result = []
    for vnum in sorted(total_map.keys()):
        t = total_map[vnum]
        k = kfw_map.get(vnum, {})
        e = ebp_map.get(vnum, {})

        result.append({
            'voyage_num': vnum,
            'start_time': t['start_time'],
            'end_time': t['end_time'],
            'total_duration': round(t['duration'], 2),
            'total_nav': round(t['nav_duration'], 2),
            'total_stby': round(t['stby_duration'], 2),
            'kfw_duration': round(k.get('duration', 0), 2),
            'kfw_nav': round(k.get('nav_duration', 0), 2),
            'kfw_stby': round(k.get('stby_duration', 0), 2),
            'ebp_duration': round(e.get('duration', 0), 2),
            'ebp_nav': round(e.get('nav_duration', 0), 2),
            'ebp_stby': round(e.get('stby_duration', 0), 2),
        })

    df = pd.DataFrame(result)

    # 저장
    name = vessel_name or str(mmsi)
    folder = os.path.join(SAVE_ROOT, f"{mmsi}_{name}")
    os.makedirs(folder, exist_ok=True)
    csv_path = os.path.join(folder, f"{mmsi}_{name}_항차정보.csv")
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')

    return len(df)


def main():
    parser = argparse.ArgumentParser(description='선박별 항차 정보 CSV 추출')
    parser.add_argument('--mmsi', type=int, default=None)
    args = parser.parse_args()

    logging.info("=" * 60)
    logging.info("선박별 항차 정보 CSV 추출")
    logging.info(f"저장: {SAVE_ROOT}")
    logging.info("=" * 60)

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    if args.mmsi:
        cur.execute("SELECT mmsi, vessel_name FROM kfw_ebp_shipinfo WHERE mmsi = %s", (str(args.mmsi),))
        ships = cur.fetchall()
    else:
        cur.execute("SELECT mmsi, vessel_name FROM kfw_ebp_shipinfo ORDER BY mmsi")
        ships = cur.fetchall()

    logging.info(f"대상: {len(ships)}척")
    logging.info("")

    total_exported = 0
    for i, (mmsi, name) in enumerate(ships, 1):
        try:
            count = export_ship(int(mmsi), name, conn)
            if count > 0:
                total_exported += 1
                logging.info(f"  [{i}/{len(ships)}] {mmsi} {name}: {count}항차")
        except Exception as e:
            logging.error(f"  [{i}/{len(ships)}] {mmsi} {name} 오류: {e}")

    conn.close()

    logging.info("")
    logging.info(f"완료! {total_exported}척 CSV 저장")


if __name__ == '__main__':
    main()
