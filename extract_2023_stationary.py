import psycopg2
import csv
import os
from datetime import datetime, timedelta

OUTPUT_DIR = r"k:\coding_project\해양수산 데이터 분석 플랫폼\mmsi_results"
GRID_FILE = r"k:\coding_project\해양수산 데이터 분석 플랫폼\grid_statistics_2023.csv"

# 1. 로컬 marine DB에서 어선 MMSI 목록
local_conn = psycopg2.connect(
    host='localhost', dbname='marine', user='postgres',
    password='prhkddlf0420!', port='5432'
)
cur = local_conn.cursor()
cur.execute("SELECT mmsi FROM fishing_shipinfo WHERE mmsi IS NOT NULL")
fishing_mmsi = [row[0] for row in cur.fetchall()]
cur.close()
local_conn.close()
print(f"어선 MMSI 수: {len(fishing_mmsi)}")

# 2. 2023년 1주일 단위 기간 생성
weeks = []
start = datetime(2023, 1, 1)
end = datetime(2024, 1, 1)
while start < end:
    week_end = min(start + timedelta(days=7), end)
    # 해당 월 테이블명 (db202301 ~ db202312)
    table = f"db{start.strftime('%Y%m')}"
    weeks.append((start, week_end, table))
    start = week_end

print(f"처리할 주간 수: {len(weeks)}")

# 3. 원격 서버 연결
remote_conn = psycopg2.connect(
    host='203.253.202.21', dbname='aisdb', user='fishery_readonly_2',
    password='readonly', port='5432'
)

# MMSI별 파일 핸들러
mmsi_files = {}
# 격자 통계: (grid_lat, grid_lon) -> {count, mmsi_set}
grid = {}

batch_size = 300
total_records = 0

for week_idx, (w_start, w_end, table) in enumerate(weeks):
    # 월이 바뀌면 테이블도 바뀜 - 주간이 월 경계를 넘을 수 있음
    tables_needed = set()
    d = w_start
    while d < w_end:
        tables_needed.add(f"db{d.strftime('%Y%m')}")
        d += timedelta(days=1)

    for tbl in sorted(tables_needed):
        # 해당 테이블의 시작/끝 범위 계산
        tbl_start = max(w_start, datetime(int(tbl[2:6]), int(tbl[6:8]), 1))
        if int(tbl[6:8]) == 12:
            tbl_end_month = datetime(int(tbl[2:6]) + 1, 1, 1)
        else:
            tbl_end_month = datetime(int(tbl[2:6]), int(tbl[6:8]) + 1, 1)
        tbl_end = min(w_end, tbl_end_month)

        print(f"[{week_idx+1}/{len(weeks)}] {tbl} ({tbl_start.strftime('%m/%d')}~{tbl_end.strftime('%m/%d')}) ...", end="", flush=True)

        week_count = 0
        for i in range(0, len(fishing_mmsi), batch_size):
            batch = fishing_mmsi[i:i+batch_size]
            placeholders = ','.join(['%s'] * len(batch))

            query = f"""
                SELECT mmsi,
                       lon / 10000000.0 as lon,
                       lat / 10000000.0 as lat,
                       sog,
                       datetime
                FROM {tbl}
                WHERE mmsi IN ({placeholders})
                  AND datetime >= %s AND datetime < %s
                  AND sog <= 1
                  AND lon <> 0 AND lat <> 0
            """
            cur = remote_conn.cursor()
            cur.execute(query, batch + [tbl_start, tbl_end])
            rows = cur.fetchall()
            cur.close()

            for mmsi, lon, lat, sog, dt in rows:
                # MMSI별 파일 저장
                if mmsi not in mmsi_files:
                    fpath = os.path.join(OUTPUT_DIR, f"{mmsi}.csv")
                    f = open(fpath, 'w', newline='', encoding='utf-8')
                    writer = csv.writer(f)
                    writer.writerow(['mmsi', 'datetime', 'lat', 'lon', 'sog'])
                    mmsi_files[mmsi] = (f, writer)

                mmsi_files[mmsi][1].writerow([mmsi, dt, f"{lat:.6f}", f"{lon:.6f}", sog])

                # 격자 통계
                grid_key = (round(lat, 3), round(lon, 3))
                if grid_key not in grid:
                    grid[grid_key] = {'count': 0, 'mmsi_set': set()}
                grid[grid_key]['count'] += 1
                grid[grid_key]['mmsi_set'].add(mmsi)

            week_count += len(rows)

        total_records += week_count
        print(f" {week_count}건 (누적: {total_records})", flush=True)

remote_conn.close()

# 파일 닫기
for f, writer in mmsi_files.values():
    f.close()
print(f"\nMMSI별 파일 저장 완료: {len(mmsi_files)}개 선박 -> {OUTPUT_DIR}")

# 격자 통계 저장
with open(GRID_FILE, 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(['grid_lat', 'grid_lon', 'record_count', 'ship_count'])
    for (glat, glon), info in sorted(grid.items(), key=lambda x: len(x[1]['mmsi_set']), reverse=True):
        writer.writerow([f"{glat:.3f}", f"{glon:.3f}", info['count'], len(info['mmsi_set'])])

print(f"격자 통계 저장 완료: {GRID_FILE}")
print(f"총 격자 수: {len(grid)}")
print(f"총 레코드: {total_records}")
print(f"총 선박 수: {len(mmsi_files)}")
