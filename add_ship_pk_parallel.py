"""
모든 ship_{mmsi} 테이블에 PRIMARY KEY (datetime) 추가 - 병렬 7워커
"""
import psycopg2
import time
import sys
from multiprocessing import Pool

LOCAL_DB = {
    'host': '203.253.202.171', 'dbname': 'marine',
    'user': 'postgres', 'password': 'prhkddlf0420', 'port': 5432
}


def add_pk(table):
    try:
        conn = psycopg2.connect(**LOCAL_DB)
        cur = conn.cursor()
        # 이미 PK 있으면 skip
        cur.execute("""SELECT 1 FROM pg_constraint c JOIN pg_class cl ON c.conrelid=cl.oid
                       WHERE cl.relname=%s AND c.contype='p'""", (table,))
        if cur.fetchone():
            return (table, 'skip', None)
        # 일반 datetime 인덱스 삭제 (PK가 대신함)
        cur.execute(f"DROP INDEX IF EXISTS {table}_datetime_idx")
        cur.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (datetime)")
        conn.commit()
        cur.close(); conn.close()
        return (table, 'ok', None)
    except Exception as e:
        return (table, 'err', str(e)[:100])


def main():
    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()
    cur.execute("""SELECT cl.relname FROM pg_class cl
                   WHERE cl.relname ~ '^ship_[0-9]+$' AND cl.relkind='r'
                   AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conrelid=cl.oid AND c.contype='p')
                   ORDER BY cl.relname""")
    tables = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    print(f'PK 추가 대상: {len(tables)}개')

    t0 = time.time()
    ok = err = 0
    errors = []
    with Pool(processes=7) as pool:
        for i, (table, status, msg) in enumerate(pool.imap_unordered(add_pk, tables), 1):
            if status == 'ok':
                ok += 1
            elif status == 'err':
                err += 1
                errors.append((table, msg))
            if i % 30 == 0:
                elapsed = time.time() - t0
                eta = elapsed / i * (len(tables) - i)
                print(f'  {i}/{len(tables)} ok={ok} err={err} ({elapsed:.0f}s elapsed, ETA {eta:.0f}s)')

    print(f'\n완료: ok={ok} err={err} 총 {time.time()-t0:.0f}s')
    for t, m in errors[:10]:
        print(f'  ERROR {t}: {m}')


if __name__ == '__main__':
    main()
