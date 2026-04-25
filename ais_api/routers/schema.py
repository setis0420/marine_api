"""
DB 스키마 탐색 라우터 (공개)
- /schema/databases : 3개 DB 개요
- /schema/tables/{db} : 테이블 목록
- /schema/tables/{db}/{table} : 테이블 상세 (컬럼, 샘플, 행수)
"""
from fastapi import APIRouter, HTTPException
import psycopg2
from config import AIS_DB, FISHERY_DB, LOCAL_DB, FISHERY_TABLES

router = APIRouter(prefix="/api/schema", tags=["스키마 탐색"], include_in_schema=False)


# === DB 접속 헬퍼 ===
def _conn(db_key: str):
    if db_key == 'fishery':
        return psycopg2.connect(**FISHERY_DB)
    if db_key == 'ais':
        return psycopg2.connect(**AIS_DB)
    if db_key == 'marine':
        return psycopg2.connect(host=LOCAL_DB['host'], dbname=LOCAL_DB['database'],
                                user=LOCAL_DB['user'], password=LOCAL_DB['password'],
                                port=LOCAL_DB['port'])
    raise HTTPException(status_code=404, detail=f"알 수 없는 DB: {db_key}")


DB_INFO = {
    'fishery': {
        'label': '수산 데이터 (Fishery)',
        'host': FISHERY_DB['host'],
        'database': FISHERY_DB['database'],
        'description': '위판, CPUE, 어획노력량, TAC 등 수산 통계 · 어선 · 센서 · 환경 데이터',
        'color': '#2ecc71',
        'icon': 'F',
    },
    'ais': {
        'label': 'AIS 항적 (AIS)',
        'host': AIS_DB['host'],
        'database': AIS_DB['database'],
        'description': '2012년~현재 월별 AIS 항적 데이터 (선박 위치·속력·침로)',
        'color': '#3498db',
        'icon': 'A',
    },
    'marine': {
        'label': 'KFW/EBP 분석 (Marine)',
        'host': LOCAL_DB['host'],
        'database': LOCAL_DB['database'],
        'description': 'KFW/EBP 대상 선박 개별 테이블 · 항차 분리 · 조업 분류 결과',
        'color': '#e67e22',
        'icon': 'M',
    },
}


@router.get("/databases")
def databases():
    """3개 DB 개요 및 테이블 수"""
    result = []
    for key, info in DB_INFO.items():
        table_count = None
        status = 'ok'
        try:
            conn = _conn(key)
            cur = conn.cursor()
            if key == 'fishery':
                table_count = len(FISHERY_TABLES)
            elif key == 'ais':
                cur.execute("""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema='public' AND table_name LIKE 'ais_%'
                """)
                table_count = cur.fetchone()[0]
            elif key == 'marine':
                cur.execute("""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema='public'
                """)
                table_count = cur.fetchone()[0]
            cur.close()
            conn.close()
        except Exception as e:
            status = f'error: {str(e)[:60]}'
        result.append({
            'key': key, **info, 'table_count': table_count, 'status': status,
        })
    return {'databases': result}


@router.get("/tables/{db}")
def tables(db: str):
    """특정 DB의 테이블 목록"""
    if db not in DB_INFO:
        raise HTTPException(status_code=404, detail=f"알 수 없는 DB: {db}")

    conn = _conn(db)
    cur = conn.cursor()

    if db == 'fishery':
        # 허용된 테이블만 공개
        items = []
        for name, info in FISHERY_TABLES.items():
            try:
                cur.execute(f"SELECT COUNT(*) FROM {name}")
                cnt = cur.fetchone()[0]
            except Exception:
                conn.rollback()
                cnt = None
            items.append({
                'name': name,
                'description': info['description'],
                'row_count': cnt,
                'category': _fishery_category(name),
            })
    elif db == 'ais':
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_name LIKE 'ais_%'
            ORDER BY table_name
        """)
        items = []
        for (name,) in cur.fetchall():
            items.append({
                'name': name,
                'description': _ais_description(name),
                'row_count': None,
                'category': 'AIS 월별',
            })
    elif db == 'marine':
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public'
            ORDER BY table_name
        """)
        items = []
        for (name,) in cur.fetchall():
            items.append({
                'name': name,
                'description': _marine_description(name),
                'row_count': None,
                'category': _marine_category(name),
            })

    cur.close()
    conn.close()
    return {'db': db, 'count': len(items), 'tables': items}


@router.get("/tables/{db}/{table}")
def table_detail(db: str, table: str, sample: int = 5):
    """테이블 컬럼 + 행수 + 샘플"""
    if db not in DB_INFO:
        raise HTTPException(status_code=404, detail=f"알 수 없는 DB: {db}")

    # 보안: fishery는 허용 목록만
    if db == 'fishery' and table not in FISHERY_TABLES:
        raise HTTPException(status_code=403, detail="허용되지 않은 테이블")
    # 이름 유효성
    if not table.replace('_', '').replace('-', '').isalnum():
        raise HTTPException(status_code=400, detail="잘못된 테이블명")

    conn = _conn(db)
    cur = conn.cursor()

    # 존재 확인
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='public' AND table_name=%s
    """, (table,))
    if not cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail=f"테이블 없음: {table}")

    # 컬럼
    cur.execute("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
    """, (table,))
    columns = [{'name': r[0], 'type': r[1], 'nullable': r[2] == 'YES',
                'default': r[3], 'comment': _column_comment(cur, table, r[0])}
               for r in cur.fetchall()]

    # 행수 (대용량 대비: marine ship_* 테이블은 근사치)
    row_count = None
    try:
        if db == 'marine' and table.startswith('ship_'):
            cur.execute("""
                SELECT reltuples::bigint FROM pg_class WHERE relname=%s
            """, (table,))
            row_count = cur.fetchone()[0]
        else:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            row_count = cur.fetchone()[0]
    except Exception:
        conn.rollback()

    # 샘플
    sample_rows = []
    try:
        sample = max(1, min(sample, 20))
        cur.execute(f"SELECT * FROM {table} LIMIT %s", (sample,))
        col_names = [c[0] for c in cur.description]
        for row in cur.fetchall():
            sample_rows.append({col_names[i]: _to_json_safe(v) for i, v in enumerate(row)})
    except Exception:
        conn.rollback()

    # 설명
    desc = ''
    if db == 'fishery':
        desc = FISHERY_TABLES.get(table, {}).get('description', '')
    elif db == 'marine':
        desc = _marine_description(table)
    elif db == 'ais':
        desc = _ais_description(table)

    cur.close()
    conn.close()
    return {
        'db': db,
        'table': table,
        'description': desc,
        'row_count': row_count,
        'columns': columns,
        'sample': sample_rows,
    }


# === 유틸 ===
def _to_json_safe(v):
    from datetime import datetime, date
    import math
    if v is None: return None
    if isinstance(v, (datetime, date)): return v.isoformat()
    if isinstance(v, (bytes, bytearray)): return f"<binary {len(v)} bytes>"
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, (int, bool)):
        return v
    return str(v)


def _column_comment(cur, table, column):
    try:
        cur.execute("""
            SELECT pgd.description
            FROM pg_catalog.pg_statio_all_tables AS st
            JOIN pg_catalog.pg_description AS pgd ON pgd.objoid = st.relid
            JOIN information_schema.columns AS c ON c.ordinal_position = pgd.objsubid
              AND c.table_schema = st.schemaname AND c.table_name = st.relname
            WHERE c.table_schema='public' AND c.table_name=%s AND c.column_name=%s
        """, (table, column))
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None


def _fishery_category(name):
    if name.startswith('fish_spatio_temp') or name == 'fish_spatio_temporal_db': return '시공간 어업'
    if name.startswith('fish_landing') or name.startswith('suhyub'): return '위판/수협'
    if name.startswith('fish_code') or name.startswith('fish_group') or name.startswith('fish_type'): return '참조 코드'
    if name.startswith('fishship_sensor'): return '선박 센서'
    if name.startswith('fishing_shipinfo') or name.startswith('fishing_shiptype'): return '선박 정보'
    if name.startswith('fishing_tac'): return 'TAC'
    if name.startswith('fishing_effort') or name.startswith('fishing_activity') or name.startswith('fishing_labels'): return '조업/어획'
    if name.startswith('purse_'): return '선망 전용'
    if name.startswith('port_') or name == 'transit_time' or name == 'suhyub_entrance_count': return '항구/통항'
    if name == 'fish_cpue' or name == 'fish_suhyub_location' or name == 'haegu_location': return '위치/CPUE'
    if name == 'marine_env_ship_observation' or name == 'zooplankton' or name == 'seabed_info': return '해양환경'
    if name == 'fishing_trj_category_month': return '항적 카테고리'
    if name == 'shipinfo_ner': return '선박 정보'
    return '기타'


def _marine_category(name):
    if name.startswith('ship_'): return '선박별 항적'
    if name.startswith('kfw_ebp'): return 'KFW/EBP 메타'
    return '기타'


def _marine_description(name):
    MAP = {
        'shipinfo': '모든 선박 메타정보 55,225척 (여객선/화물선/어선, 기본 조회용)',
        'shipinfo_ner': '음성인식(NER) 전용 발음 변형 53,909척 - 일반 조회에 사용 안 함',
        'fishing_shipinfo': '한국 어선 부속 정보 3,264척 (업종/톤수)',
        'kfw_ebp_shipinfo': '내부 분석용 어선 1,006척',
        'kfw_ebp_voyage': '항차별 구역 체류 시간',
        'kfw_ebp_trjdata': '원본 항적 데이터 (마이그레이션 완료)',
    }
    if name in MAP: return MAP[name]
    if name.startswith('ship_'):
        mmsi = name.replace('ship_', '')
        return f'MMSI {mmsi} 선박의 개별 항적 테이블 (조업 분류 포함)'
    return ''


def _ais_description(name):
    # ais_YYYYMM 형태
    import re
    m = re.match(r'ais_(\d{4})(\d{2})', name)
    if m:
        return f'{m.group(1)}년 {int(m.group(2))}월 AIS 항적'
    return ''
