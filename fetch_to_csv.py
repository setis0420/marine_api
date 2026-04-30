"""
21번 aisdb에서 특정 MMSI 항적을 CSV로 저장 (DB 적재 X)
- 기간: 2012-01 ~ 2024-12 (전체)
- 컬럼: datetime, mmsi, lat, lon, sog, cog, heading, nav_status
- lat/lon은 ÷10^7, sog/cog는 ÷10 변환된 실제값으로 저장

사용법:
    python fetch_to_csv.py --mmsi "440181090,440702840"
    python fetch_to_csv.py --mmsi 440181090 --year-from 2020 --year-to 2024
    python fetch_to_csv.py --mmsi 440181090 --out custom_dir
    python fetch_to_csv.py --mmsi 440181090 --by-month   # 년월별 파일 분리
"""
import psycopg2
import os
import csv
import argparse
import time
from datetime import datetime
from multiprocessing import Pool

REMOTE_DB = {'host': '203.253.202.21', 'dbname': 'aisdb',
             'user': 'fishery_readonly_2', 'password': 'readonly', 'port': 5432}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, 'csv_export')


HEADER = ['datetime', 'mmsi', 'lat', 'lon', 'sog_knots', 'cog_deg', 'heading_deg', 'nav_status']


def _convert(r):
    dt, mm, lat, lon, sog, cog, hdg, nav = r
    return [
        dt, mm,
        lat / 10000000.0 if lat is not None else None,
        lon / 10000000.0 if lon is not None else None,
        sog / 10.0 if sog is not None else None,
        cog / 10.0 if cog is not None else None,
        hdg, nav,
    ]


def fetch_one(args):
    mmsi, out_dir, year_from, year_to, by_month = args
    conn = psycopg2.connect(**REMOTE_DB)
    cur = conn.cursor()
    total = 0
    t0 = time.time()
    files_made = 0

    if by_month:
        # 년월별 분리: 폴더/ship_{mmsi}/ship_{mmsi}_YYYYMM.csv
        ship_dir = os.path.join(out_dir, f'ship_{mmsi}')
        os.makedirs(ship_dir, exist_ok=True)
        for year in range(year_from, year_to + 1):
            year_total = 0
            for month in range(1, 13):
                tbl = f'db{year}{month:02d}'
                try:
                    cur.execute(f"""
                        SELECT datetime, mmsi, lat, lon, sog, cog, heading, nav_status
                        FROM {tbl}
                        WHERE mmsi = %s AND lat <> 0 AND lon <> 0
                        ORDER BY datetime
                    """, (int(mmsi),))
                    rows = cur.fetchall()
                    if rows:
                        out_path = os.path.join(ship_dir, f'ship_{mmsi}_{year}{month:02d}.csv')
                        with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
                            w = csv.writer(f)
                            w.writerow(HEADER)
                            for r in rows: w.writerow(_convert(r))
                        files_made += 1
                        total += len(rows)
                        year_total += len(rows)
                except Exception as e:
                    print(f'  [{mmsi}] {tbl}: {str(e)[:60]}')
                    conn.rollback()
            print(f'  [{mmsi}] {year} 완료 ({year_total:,}행)')
        result_path = ship_dir
    else:
        out_path = os.path.join(out_dir, f'ship_{mmsi}_{year_from}-{year_to}.csv')
        os.makedirs(out_dir, exist_ok=True)
        with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            for year in range(year_from, year_to + 1):
                for month in range(1, 13):
                    tbl = f'db{year}{month:02d}'
                    try:
                        cur.execute(f"""
                            SELECT datetime, mmsi, lat, lon, sog, cog, heading, nav_status
                            FROM {tbl}
                            WHERE mmsi = %s AND lat <> 0 AND lon <> 0
                            ORDER BY datetime
                        """, (int(mmsi),))
                        for r in cur.fetchall():
                            w.writerow(_convert(r)); total += 1
                    except Exception as e:
                        print(f'  [{mmsi}] {tbl}: {str(e)[:60]}')
                        conn.rollback()
                print(f'  [{mmsi}] {year} 완료 (누적 {total:,}행)')
        result_path = out_path
        files_made = 1

    cur.close(); conn.close()
    return (mmsi, result_path, total, files_made, time.time() - t0)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--mmsi', required=True, help='MMSI (콤마로 여러 개 가능)')
    p.add_argument('--year-from', type=int, default=2012)
    p.add_argument('--year-to', type=int, default=2024)
    p.add_argument('--out', default=DEFAULT_OUT_DIR, help='저장 폴더')
    p.add_argument('--workers', type=int, default=2)
    p.add_argument('--by-month', action='store_true', help='년월별 파일 분리')
    args = p.parse_args()

    mmsis = [int(m.strip()) for m in args.mmsi.split(',') if m.strip()]
    print(f'대상 MMSI: {mmsis}')
    print(f'기간: {args.year_from} ~ {args.year_to}')
    print(f'저장: {args.out}')
    print(f'분리: {"년월별" if args.by_month else "단일파일"}')
    print(f'워커: {args.workers}')
    print('=' * 60)

    work = [(m, args.out, args.year_from, args.year_to, args.by_month) for m in mmsis]
    t0 = time.time()

    if args.workers <= 1 or len(mmsis) == 1:
        results = [fetch_one(w) for w in work]
    else:
        with Pool(processes=min(args.workers, len(mmsis))) as pool:
            results = pool.map(fetch_one, work)

    print('=' * 60)
    for mmsi, path, n, files, dur in results:
        print(f'  MMSI {mmsi}: {n:,}행, {files}개 파일 → {path} ({dur:.0f}s)')
    print(f'전체 {time.time()-t0:.0f}s 완료')


if __name__ == '__main__':
    main()
