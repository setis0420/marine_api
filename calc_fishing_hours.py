"""
fishing_voyage에 조업시간 컬럼 추가 (시간 단위) + 대형트롤 34척 채우기
- Phase 1: 신규 컬럼 추가, 기존 분 → 시간 변환, 분 컬럼 DROP
- Phase 2: 대형트롤 34척 fishing_hours/nav_hours/stby_hours 계산 (SOG 기반)
  - SOG 2~5 노트 (sog 20~50) → fishing
  - SOG >5 노트 (sog 51~1022) → nav
  - SOG <2 노트 (sog 0~19) → stby
- Phase 3: 검증
"""
import psycopg2
import sys, time

sys.stdout.reconfigure(encoding='utf-8')

from db_config import LOCAL_DB as _LOCAL
LOCAL_DB = {'host': _LOCAL['host'], 'dbname': _LOCAL['database'],
            'user': _LOCAL['user'], 'password': _LOCAL['password'], 'port': _LOCAL['port']}


def phase1_alter(conn):
    cur = conn.cursor()
    print('[Phase 1] 컬럼 변경...')

    # 신규 컬럼 추가
    print('  신규 컬럼 추가...')
    for stmt in [
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS total_hours REAL",
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS fishing_hours REAL",
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS nav_hours REAL",
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS stby_hours REAL",
        "ALTER TABLE fishing_voyage ADD COLUMN IF NOT EXISTS effort_source TEXT",
    ]:
        cur.execute(stmt)
    conn.commit()

    # 기존 데이터 분 → 시간 변환
    print('  기존 duration/nav/stby (분) → 시간 변환...')
    t0 = time.time()
    cur.execute("""UPDATE fishing_voyage SET
                     total_hours = duration / 60.0,
                     nav_hours   = nav_duration / 60.0,
                     stby_hours  = stby_duration / 60.0
                   WHERE duration IS NOT NULL OR nav_duration IS NOT NULL OR stby_duration IS NOT NULL""")
    print(f'  변환된 행: {cur.rowcount:,} ({time.time()-t0:.0f}s)')
    conn.commit()

    # 기존 분 컬럼 DROP
    print('  기존 분 컬럼 DROP...')
    for col in ['duration', 'nav_duration', 'stby_duration']:
        try:
            cur.execute(f"ALTER TABLE fishing_voyage DROP COLUMN IF EXISTS {col}")
        except Exception as e:
            print(f'    {col}: {e}')
            conn.rollback()
    conn.commit()
    cur.close()
    print('  Phase 1 완료\n')


def phase2_trawl_hours(conn):
    """대형트롤 34척 fishing/nav/stby_hours 계산 (SOG 기반)"""
    cur = conn.cursor()
    print('[Phase 2] 대형트롤 34척 hours 계산 (SOG 기반)...')

    # 대형트롤 MMSI
    cur.execute("""SELECT v.mmsi, COUNT(*) FROM fishing_voyage v
                   JOIN shipinfo s ON s.mmsi = v.mmsi
                   WHERE s.fishing_type = '대형트롤어업'
                   GROUP BY v.mmsi ORDER BY v.mmsi""")
    mmsi_voys = cur.fetchall()
    print(f'  대상: {len(mmsi_voys)}척, {sum(v[1] for v in mmsi_voys):,} 항차')

    # ship_{mmsi} 존재 확인
    cur.execute("""SELECT REPLACE(table_name, 'ship_', '')::bigint FROM information_schema.tables
                   WHERE table_schema='public' AND table_name ~ '^ship_[0-9]+$'""")
    ship_set = set(r[0] for r in cur.fetchall())

    t0 = time.time()
    total_updated = 0
    for i, (mmsi, voy_n) in enumerate(mmsi_voys, 1):
        if mmsi not in ship_set:
            print(f'  ({i}/{len(mmsi_voys)}) ship_{mmsi}: 테이블 없음')
            continue
        try:
            # SOG 기반 분류 (sog ×10 저장)
            # 2~5 노트 = sog 20~50 (조업)
            # >5 노트 = sog 51~1022 (항해)
            # <2 노트 = sog 0~19 (정박)
            # 1023 = N/A 제외
            cur.execute(f"""
                WITH per_voyage AS (
                  SELECT v.id,
                    COUNT(*) FILTER (WHERE s.sog BETWEEN 20 AND 50) / 60.0 AS fishing_h,
                    COUNT(*) FILTER (WHERE s.sog BETWEEN 51 AND 1022) / 60.0 AS nav_h,
                    COUNT(*) FILTER (WHERE s.sog BETWEEN 0 AND 19) / 60.0 AS stby_h
                  FROM fishing_voyage v
                  JOIN ship_{mmsi} s ON s.datetime BETWEEN v.start_time AND v.end_time
                  WHERE v.mmsi = {mmsi}
                  GROUP BY v.id
                )
                UPDATE fishing_voyage v SET
                  fishing_hours = pv.fishing_h,
                  nav_hours = pv.nav_h,
                  stby_hours = pv.stby_h,
                  effort_source = 'sog'
                FROM per_voyage pv WHERE v.id = pv.id
            """)
            total_updated += cur.rowcount
            conn.commit()
            elapsed = time.time() - t0
            eta = elapsed / i * (len(mmsi_voys) - i)
            print(f'  ({i}/{len(mmsi_voys)}) ship_{mmsi}: +{cur.rowcount} (누적 {total_updated:,}, ETA {eta:.0f}s)')
        except Exception as e:
            conn.rollback()
            print(f'  ship_{mmsi} 오류: {str(e)[:100]}')

    print(f'  완료: {total_updated:,} 항차 업데이트 ({time.time()-t0:.0f}s)\n')
    cur.close()


def phase3_verify(conn):
    cur = conn.cursor()
    print('[Phase 3] 검증...')
    cur.execute("""SELECT
                     effort_source, COUNT(*),
                     ROUND(AVG(total_hours)::numeric, 2) avg_total,
                     ROUND(AVG(fishing_hours)::numeric, 2) avg_fishing,
                     ROUND(AVG(nav_hours)::numeric, 2) avg_nav,
                     ROUND(AVG(stby_hours)::numeric, 2) avg_stby
                   FROM fishing_voyage v
                   JOIN shipinfo s ON s.mmsi = v.mmsi
                   WHERE s.fishing_type = '대형트롤어업' AND v.target_area = 'total'
                   GROUP BY 1""")
    print(f'  {"source":<10} {"count":>8} {"avg_total":>10} {"avg_fishing":>12} {"avg_nav":>10} {"avg_stby":>10}')
    for r in cur.fetchall():
        print(f'  {str(r[0]):<10} {r[1]:>8,} {r[2]:>10} {r[3]:>12} {r[4]:>10} {r[5]:>10}')

    # 합 = total 검증
    cur.execute("""SELECT COUNT(*),
                     ROUND(AVG(ABS(total_hours - (fishing_hours + nav_hours + stby_hours)))::numeric, 2) AS diff
                   FROM fishing_voyage v JOIN shipinfo s ON s.mmsi=v.mmsi
                   WHERE s.fishing_type='대형트롤어업' AND v.target_area='total'
                     AND fishing_hours IS NOT NULL AND total_hours IS NOT NULL""")
    r = cur.fetchone()
    print(f'\n  fishing+nav+stby vs total 평균 차이: {r[1]} 시간 (n={r[0]:,})')
    print('  (1023=N/A 제외 + 보간 차이로 약간 차이 정상)')

    # 인덱스
    cur.execute("CREATE INDEX IF NOT EXISTS idx_fv_fishing_hours ON fishing_voyage(fishing_hours) WHERE fishing_hours IS NOT NULL")
    conn.commit()
    print('  인덱스 생성')
    cur.close()


def main():
    conn = psycopg2.connect(**LOCAL_DB)
    t0 = time.time()
    phase1_alter(conn)
    phase2_trawl_hours(conn)
    phase3_verify(conn)
    print(f'\n전체 소요: {time.time()-t0:.0f}s')
    conn.close()


if __name__ == '__main__':
    main()
