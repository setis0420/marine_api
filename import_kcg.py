"""
해양경찰청 보유 선박제원 엑셀 → 171 marine.fishing_shipinfo_kcg
"""
import pandas as pd
import psycopg2
import sys, time
from psycopg2.extras import execute_values

sys.stdout.reconfigure(encoding='utf-8')

XLSX = r'k:\coding_project\해양수산 데이터 분석 플랫폼\해양경찰청 보유 선박제원(3.31.)_정리본.xlsx'
from db_config import LOCAL_DB as _LOCAL
LOCAL_DB = {'host': _LOCAL['host'], 'dbname': _LOCAL['database'],
            'user': _LOCAL['user'], 'password': _LOCAL['password'], 'port': _LOCAL['port']}

# (한글, 영어, 타입, 설명)
COLS = [
    ('선명', 'shipname', 'TEXT', '선명'),
    ('톤수', 'tonnage', 'REAL', '톤수'),
    ('길이', 'length_m', 'REAL', '선박 길이(m)'),
    ('엔진종류', 'engine_type', 'TEXT', '엔진 종류'),
    ('엔진갯수', 'engine_count', 'INTEGER', '엔진 갯수'),
    ('엔진출력PS', 'engine_ps', 'REAL', '엔진 출력(PS)'),
    ('엔진출력KW', 'engine_kw', 'REAL', '엔진 출력(KW)'),
    ('선질', 'hull_material', 'TEXT', '선질'),
    ('등록번호', 'registration_no', 'BIGINT', '어선 등록번호'),
    ('건조일시', 'build_date', 'TEXT', '건조일시 (YYYY.MM.DD)'),
    ('선적지', 'port', 'TEXT', '선적지'),
    ('업종', 'fishing_type', 'TEXT', '업종'),
    ('호출부호', 'callsign', 'TEXT', '호출부호'),
    ('선원수', 'crew_count', 'INTEGER', '선원수'),
    ('RFID_CHA', 'rfid_cha', 'TEXT', 'RFID 차수'),
    ('출입번호', 'entry_no', 'TEXT', '출입번호'),
    ('RFID', 'rfid', 'TEXT', 'RFID 번호'),
    ('신고방법', 'report_method', 'TEXT', '신고방법'),
    ('수협MMSI', 'mmsi', 'BIGINT', '수협 MMSI (메인)'),
    ('수협AIS', 'suhyub_ais', 'TEXT', '수협 AIS 보유 여부'),
    ('수협GPSVHF', 'suhyub_gps_vhf', 'TEXT', '수협 GPS-VHF 보유 여부'),
    ('수협D-MF/HF', 'suhyub_d_mf_hf', 'TEXT', '수협 D-MF/HF 보유 여부'),
    ('수협INMARSAT-D', 'suhyub_inmarsat_d', 'TEXT', '수협 INMARSAT-D 보유 여부'),
    ('무선국VHF', 'radio_vhf', 'TEXT', '무선국 VHF 보유 여부'),
    ('무선국VHF-MMSI', 'radio_vhf_mmsi', 'BIGINT', '무선국 VHF MMSI'),
    ('무선국AIS-MMSI', 'radio_ais_mmsi', 'BIGINT', '무선국 AIS MMSI'),
    ('무선국AIS', 'radio_ais', 'TEXT', '무선국 AIS 보유 여부'),
    ('어업인허가(시군구)', 'fishing_license_local', 'TEXT', '어업인 허가 (시군구)'),
    ('허가시작일(시군구)', 'license_start_local', 'DATE', '허가 시작일 (시군구)'),
    ('허가종료일(시군구)', 'license_end_local', 'DATE', '허가 종료일 (시군구)'),
    ('어업인허가(시도)', 'fishing_license_state', 'TEXT', '어업인 허가 (시도)'),
    ('허가시작일(시도)', 'license_start_state', 'DATE', '허가 시작일 (시도)'),
    ('허가종료일(시도)', 'license_end_state', 'DATE', '허가 종료일 (시도)'),
    ('관리어선승인번호', 'mgmt_approval_no', 'TEXT', '관리어선 승인번호'),
    ('관리어선승인시작일자', 'mgmt_approval_start', 'DATE', '관리어선 승인 시작일'),
    ('관리어선승인종료일자', 'mgmt_approval_end', 'DATE', '관리어선 승인 종료일'),
    ('바다내비', 'sea_navi', 'TEXT', '바다내비 보유 여부'),
    ('MAX_CREW', 'max_crew', 'INTEGER', '최대 승선 인원'),
]


def yyyymmdd_to_date(v):
    if pd.isna(v): return None
    try:
        s = str(int(v))
        if len(s) == 8: return f'{s[0:4]}-{s[4:6]}-{s[6:8]}'
    except: pass
    return None


def to_int(v):
    if pd.isna(v): return None
    try: return int(v)
    except: return None


def to_str(v):
    if pd.isna(v): return None
    s = str(v).strip()
    if s in ('', '-', 'nan', 'NaN', 'None'): return None
    return s


def to_real(v):
    if pd.isna(v): return None
    try: return float(v)
    except: return None


CONVERT = {
    'TEXT': to_str, 'REAL': to_real, 'INTEGER': to_int,
    'BIGINT': to_int, 'DATE': yyyymmdd_to_date,
}


def main():
    print('엑셀 로드...')
    df = pd.read_excel(XLSX, sheet_name='선박대장')
    print(f'  {df.shape[0]:,}행 × {df.shape[1]}컬럼')

    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()

    # 테이블 생성
    print('테이블 CREATE...')
    cur.execute('DROP TABLE IF EXISTS fishing_shipinfo_kcg CASCADE')
    col_defs = ['id BIGSERIAL PRIMARY KEY']
    for kr, en, tp, _ in COLS:
        col_defs.append(f'{en} {tp}')
    cur.execute(f'CREATE TABLE fishing_shipinfo_kcg ({", ".join(col_defs)})')

    # 컬럼 코멘트
    for kr, en, _, desc in COLS:
        cur.execute(f"COMMENT ON COLUMN fishing_shipinfo_kcg.{en} IS %s", (desc,))
    cur.execute("COMMENT ON TABLE fishing_shipinfo_kcg IS %s",
                ('해양경찰청 보유 선박제원 (2024년 3월 31일 기준, 60,905척)',))
    conn.commit()
    print('  생성 + 코멘트 완료')

    # 데이터 변환
    print('데이터 변환...')
    t0 = time.time()
    rows = []
    for _, row in df.iterrows():
        out = []
        for kr, en, tp, _ in COLS:
            out.append(CONVERT[tp](row[kr]))
        rows.append(tuple(out))
    print(f'  {len(rows):,}행 변환 ({time.time()-t0:.1f}s)')

    # INSERT
    print('INSERT...')
    t0 = time.time()
    placeholders = ', '.join(en for _, en, _, _ in COLS)
    execute_values(cur,
        f'INSERT INTO fishing_shipinfo_kcg ({placeholders}) VALUES %s',
        rows, page_size=1000)
    conn.commit()
    print(f'  완료 ({time.time()-t0:.1f}s)')

    # 인덱스
    print('인덱스 생성...')
    for col in ['mmsi', 'registration_no', 'radio_ais_mmsi', 'radio_vhf_mmsi',
                'fishing_type', 'shipname']:
        cur.execute(f'CREATE INDEX idx_kcg_{col} ON fishing_shipinfo_kcg({col})')
    conn.commit()
    print('  완료')

    # 검증
    cur.execute('SELECT COUNT(*) FROM fishing_shipinfo_kcg')
    print(f'\n총 행수: {cur.fetchone()[0]:,}')
    cur.execute("SELECT pg_size_pretty(pg_total_relation_size('fishing_shipinfo_kcg'))")
    print(f'테이블 크기: {cur.fetchone()[0]}')

    # 사용자가 찾던 어선번호
    target = 16060026501309
    cur.execute("""SELECT registration_no, shipname, mmsi, radio_ais_mmsi, fishing_type
                   FROM fishing_shipinfo_kcg
                   WHERE registration_no::text LIKE %s OR registration_no = %s LIMIT 5""",
                (f'%{target}%', target))
    rows = cur.fetchall()
    if rows:
        print(f'\n어선번호 {target} 매칭:')
        for r in rows: print(f'  {r}')
    else:
        # 부분 매칭
        cur.execute("""SELECT registration_no, shipname, mmsi, radio_ais_mmsi
                       FROM fishing_shipinfo_kcg
                       WHERE registration_no::text LIKE '%26501309%' LIMIT 5""")
        rows = cur.fetchall()
        if rows:
            print(f'\n부분 매칭 (~26501309):')
            for r in rows: print(f'  {r}')

    conn.close()


if __name__ == '__main__':
    main()
