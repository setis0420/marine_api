"""
159 → 171 서버 DB 마이그레이션 스크립트
- 171에 marine DB 생성
- 테이블 구조 복사
- 데이터 배치 전송
"""

import psycopg2
import logging
import time
from datetime import datetime
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# 로그 설정
log_file = f'migrate_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

SRC_DB = {
    'host': 'localhost',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420!',
    'port': '5432'
}

DST_DB = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

BATCH_SIZE = 10000


def create_database():
    """171에 marine DB 생성"""
    conn = psycopg2.connect(host=DST_DB['host'], dbname='postgres',
                            user=DST_DB['user'], password=DST_DB['password'], port=DST_DB['port'])
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = 'marine'")
    if cur.fetchone():
        logging.info("marine DB 이미 존재")
    else:
        cur.execute("CREATE DATABASE marine ENCODING 'UTF8'")
        logging.info("marine DB 생성 완료")
    cur.close()
    conn.close()


def get_table_ddl(src_cur, table_name):
    """테이블 DDL 생성"""
    src_cur.execute("""
        SELECT column_name, data_type, character_maximum_length,
               is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))
    columns = src_cur.fetchall()

    col_defs = []
    for col_name, data_type, max_len, nullable, default in columns:
        # 타입 매핑
        if data_type == 'integer':
            col_type = 'INTEGER'
        elif data_type == 'bigint':
            col_type = 'BIGINT'
        elif data_type == 'smallint':
            col_type = 'SMALLINT'
        elif data_type == 'real':
            col_type = 'REAL'
        elif data_type == 'double precision':
            col_type = 'DOUBLE PRECISION'
        elif data_type == 'boolean':
            col_type = 'BOOLEAN'
        elif data_type == 'text':
            col_type = 'TEXT'
        elif data_type == 'character varying':
            col_type = f'VARCHAR({max_len})' if max_len else 'TEXT'
        elif data_type == 'timestamp without time zone':
            col_type = 'TIMESTAMP'
        elif data_type == 'numeric':
            col_type = 'NUMERIC'
        else:
            col_type = data_type.upper()

        parts = [f'"{col_name}" {col_type}']
        if nullable == 'NO':
            parts.append('NOT NULL')
        if default and 'nextval' not in str(default):
            parts.append(f"DEFAULT {default}")

        col_defs.append(' '.join(parts))

    return col_defs


def get_indexes(src_cur, table_name):
    """인덱스 DDL 가져오기"""
    src_cur.execute("""
        SELECT indexdef FROM pg_indexes
        WHERE tablename = %s
    """, (table_name,))
    return [row[0] for row in src_cur.fetchall()]


def migrate_table(table_name):
    """한 테이블 마이그레이션"""
    logging.info(f"{'='*50}")
    logging.info(f"테이블: {table_name}")
    logging.info(f"{'='*50}")

    # 소스 연결
    src_conn = psycopg2.connect(**SRC_DB)
    src_cur = src_conn.cursor()

    # 대상 연결
    dst_conn = psycopg2.connect(**DST_DB)
    dst_cur = dst_conn.cursor()

    # 1. 테이블 구조 가져오기
    col_defs = get_table_ddl(src_cur, table_name)
    if not col_defs:
        logging.info(f"  테이블 {table_name} 없음, 스킵")
        src_conn.close()
        dst_conn.close()
        return

    # 컬럼 이름 목록
    src_cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s ORDER BY ordinal_position
    """, (table_name,))
    col_names = [r[0] for r in src_cur.fetchall()]

    # 2. 대상에 테이블 생성
    dst_cur.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
    create_sql = f"CREATE TABLE {table_name} (\n  " + ",\n  ".join(col_defs) + "\n)"
    dst_cur.execute(create_sql)
    dst_conn.commit()
    logging.info(f"  테이블 생성 완료 ({len(col_names)}컬럼)")

    # 3. 데이터 개수 확인
    src_cur.execute(f"SELECT reltuples::bigint FROM pg_class WHERE relname = '{table_name}'")
    row = src_cur.fetchone()
    est_count = row[0] if row else 0
    logging.info(f"  예상 레코드: {est_count:,.0f}건")

    # 4. 데이터 복사 (배치)
    col_list = ', '.join([f'"{c}"' for c in col_names])
    placeholders = ', '.join(['%s'] * len(col_names))

    src_cur2 = src_conn.cursor('server_cursor')  # 서버 사이드 커서
    src_cur2.execute(f"SELECT {col_list} FROM {table_name}")

    total = 0
    start_time = time.time()
    while True:
        rows = src_cur2.fetchmany(BATCH_SIZE)
        if not rows:
            break

        args = ','.join(
            dst_cur.mogrify(f"({placeholders})", row).decode()
            for row in rows
        )
        dst_cur.execute(f"INSERT INTO {table_name} ({col_list}) VALUES {args}")
        dst_conn.commit()

        total += len(rows)
        if total % 100000 == 0 or len(rows) < BATCH_SIZE:
            elapsed = time.time() - start_time
            speed = total / elapsed if elapsed > 0 else 0
            logging.info(f"  전송: {total:,}건 | {elapsed:.0f}초 | {speed:.0f}건/초")

    src_cur2.close()
    elapsed = time.time() - start_time
    logging.info(f"  데이터 전송 완료: {total:,}건 | 소요: {elapsed:.0f}초")

    # 5. 인덱스 생성
    indexes = get_indexes(src_cur, table_name)
    for idx_sql in indexes:
        try:
            dst_cur.execute(idx_sql)
            dst_conn.commit()
        except Exception as e:
            dst_conn.rollback()
            logging.warning(f"  인덱스 오류 (무시): {str(e)[:80]}")

    logging.info(f"  인덱스 생성 완료 ({len(indexes)}개)")

    src_conn.close()
    dst_conn.close()


def main():
    logging.info("=" * 50)
    logging.info("159 → 171 DB 마이그레이션")
    logging.info(f"로그 파일: {log_file}")
    logging.info("=" * 50)

    # 1. marine DB 생성
    create_database()

    # 2. 마이그레이션할 테이블 목록
    tables = [
        'kfw_ebp_shipinfo',    # 선박 정보 (1,006건) - 작음
        'kfw_ebp_voyage',      # 항차 정보 - 작음
        'kfw_ebp_trjdata',     # 항적 데이터 (~5억건) - 큼!
    ]

    for table in tables:
        try:
            migrate_table(table)
        except Exception as e:
            logging.error(f"  [오류] {table}: {e}")
            continue

    logging.info("")
    logging.info("=" * 50)
    logging.info("마이그레이션 완료!")
    logging.info("=" * 50)


if __name__ == '__main__':
    main()
