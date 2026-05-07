"""
fetch_offshore.py가 duration 컬럼 오류로 fishing_voyage INSERT만 실패한
ship_{mmsi} 들에 대해 voyage 재계산 + 적재 복구.
ship_{mmsi} 데이터는 이미 정상이므로 voyage_num 분리 + fishing_voyage INSERT만 수행.
"""
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timedelta
import numpy as np
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from db_config import LOCAL_DB as LOCAL
PORT_STAY_THRESHOLD = timedelta(minutes=30)


def find_missing_mmsis(cur):
    cur.execute("""
        SELECT REPLACE(t.table_name, 'ship_', '')::bigint AS mmsi
        FROM information_schema.tables t
        LEFT JOIN (SELECT DISTINCT mmsi FROM fishing_voyage) v
          ON v.mmsi = REPLACE(t.table_name, 'ship_', '')::bigint
        WHERE t.table_schema='public'
          AND t.table_name ~ '^ship_[0-9]+$'
          AND v.mmsi IS NULL
        ORDER BY 1
    """)
    return [r[0] for r in cur.fetchall()]


def repair_one(cur, mmsi):
    table = f'ship_{mmsi}'
    cur.execute(f"SELECT datetime, port_entering, voyage_num FROM {table} ORDER BY datetime")
    records = cur.fetchall()
    n = len(records)
    if n < 2:
        return 0, 0

    dt_arr = [r[0] for r in records]
    port_raw = np.array([bool(r[1]) if r[1] is not None else False for r in records])

    real_port = np.zeros(n, dtype=bool)
    i = 0
    while i < n:
        if port_raw[i]:
            seg_s = i
            while i < n and port_raw[i]:
                i += 1
            if dt_arr[i-1] - dt_arr[seg_s] >= PORT_STAY_THRESHOLD:
                real_port[seg_s:i] = True
        else:
            i += 1

    voyage_nums = [None] * n
    year_count = {}
    current_vnum = None
    prev_in_port = bool(real_port[0])
    if not prev_in_port:
        yy = dt_arr[0].year % 100
        year_count[yy] = year_count.get(yy, 0) + 1
        current_vnum = yy * 1000 + year_count[yy]
        voyage_nums[0] = current_vnum

    for i in range(1, n):
        in_port = bool(real_port[i])
        if prev_in_port and not in_port:
            yy = dt_arr[i].year % 100
            year_count[yy] = year_count.get(yy, 0) + 1
            current_vnum = yy * 1000 + year_count[yy]
        if current_vnum is not None and not in_port:
            voyage_nums[i] = current_vnum
        prev_in_port = in_port

    updates = [(dt_arr[i], voyage_nums[i]) for i in range(n) if voyage_nums[i] != records[i][2]]
    if updates:
        cur.execute("CREATE TEMP TABLE tmp_v (dt TIMESTAMP, vnum INTEGER) ON COMMIT DROP")
        execute_values(cur, "INSERT INTO tmp_v VALUES %s", updates, page_size=5000)
        cur.execute(f"UPDATE {table} t SET voyage_num=tmp.vnum FROM tmp_v tmp WHERE t.datetime=tmp.dt")

    voyage_meta = {}
    for i in range(n):
        v = voyage_nums[i]
        if v is None: continue
        if v not in voyage_meta:
            voyage_meta[v] = {'start': dt_arr[i], 'end': dt_arr[i]}
        voyage_meta[v]['end'] = dt_arr[i]

    if voyage_meta:
        rows = [(int(mmsi), v, m['start'], m['end'], 'total',
                 (m['end'] - m['start']).total_seconds() / 3600.0, 'none')
                for v, m in voyage_meta.items()]
        execute_values(cur,
            "INSERT INTO fishing_voyage (mmsi, voyage_num, start_time, end_time, target_area, total_hours, model) "
            "VALUES %s ON CONFLICT (mmsi, voyage_num, target_area) DO UPDATE "
            "SET start_time=EXCLUDED.start_time, end_time=EXCLUDED.end_time, total_hours=EXCLUDED.total_hours",
            rows, page_size=1000)

    return n, len(voyage_meta)


def main():
    conn = psycopg2.connect(**LOCAL)
    conn.autocommit = False
    cur = conn.cursor()

    mmsis = find_missing_mmsis(cur)
    print(f'복구 대상 MMSI: {len(mmsis):,}척')
    if not mmsis:
        return

    t0 = datetime.now()
    total_voy = 0
    fail = []
    for idx, mmsi in enumerate(mmsis, 1):
        try:
            rows, voy = repair_one(cur, mmsi)
            conn.commit()
            total_voy += voy
            elapsed = (datetime.now() - t0).total_seconds()
            eta = elapsed / idx * (len(mmsis) - idx)
            print(f'  [{idx}/{len(mmsis)}] mmsi={mmsi}: {rows:,}행 → voyage {voy}개 '
                  f'(elapsed {int(elapsed)}s, ETA {int(eta)}s)', flush=True)
        except Exception as e:
            conn.rollback()
            fail.append((mmsi, str(e)[:120]))
            print(f'  [{idx}/{len(mmsis)}] mmsi={mmsi}: 실패 - {str(e)[:120]}', flush=True)

    print()
    print(f'완료: {len(mmsis) - len(fail)}/{len(mmsis)}척, 총 voyage {total_voy:,}개 입력')
    if fail:
        print(f'실패 {len(fail)}건:')
        for m, e in fail[:20]:
            print(f'  {m}: {e}')

    cur.close(); conn.close()


if __name__ == '__main__':
    main()
