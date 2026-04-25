"""
항차번호(voyage_num) 계산 스크립트
- kfw_ebp_trjdata에서 선박별로 시간순 정렬
- port_entering=True 구간이 3시간 이상이면 입항으로 판정
- 출항~입항 = 1개 항차
- voyage_num = YY * 1000 + 항차순번 (예: 22001 = 2022년 1번째 항차)
- smallint 범위: 최대 32767 (2032년까지 가능)
"""

import psycopg2
from datetime import timedelta
from multiprocessing import Pool, Value, Lock

# === 설정 ===
LOCAL_DB = {
    'host': 'localhost',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420!',
    'port': '5432'
}

NUM_WORKERS = 6         # 동시 프로세스 수
PORT_STAY_THRESHOLD = timedelta(hours=3)  # 입항 판정 기준: 3시간
UPDATE_BATCH = 10000    # DB 업데이트 배치 크기

# 처리 기간 설정 (None이면 전체)
START_DATE = '2022-01-01'   # 시작일
END_DATE = '2025-01-01'     # 종료일 (미만)

# 프로세스 간 공유 카운터
counter = None
counter_lock = None


def init_counter(c, l):
    global counter, counter_lock
    counter = c
    counter_lock = l


def get_mmsi_list():
    """처리할 MMSI 목록 조회 (shipinfo에서 가져옴 - 빠름)"""
    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_shipinfo ORDER BY mmsi")
    mmsi_list = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return mmsi_list


def identify_port_segments(records):
    """
    연속된 port_entering=True 구간을 찾아서 3시간 이상인지 판별
    records: [(datetime, port_entering), ...]  시간순 정렬
    returns: set of indexes where port_entering=True AND 해당 구간이 3시간 이상
    """
    real_port_indices = set()
    n = len(records)
    i = 0

    while i < n:
        if records[i][1]:  # port_entering = True
            # 연속 True 구간의 시작
            seg_start = i
            while i < n and records[i][1]:
                i += 1
            seg_end = i - 1  # 마지막 True 인덱스

            # 구간 지속 시간 계산
            duration = records[seg_end][0] - records[seg_start][0]
            if duration >= PORT_STAY_THRESHOLD:
                # 3시간 이상 → 실제 입항
                for j in range(seg_start, seg_end + 1):
                    real_port_indices.add(j)
        else:
            i += 1

    return real_port_indices


def calc_voyage_for_ship(args):
    """한 선박의 항차번호 계산 및 DB 업데이트"""
    mmsi, total_ships = args

    conn = None
    try:
        conn = psycopg2.connect(**LOCAL_DB)
        cur = conn.cursor()

        # 1. 해당 선박 데이터 시간순 조회 (기간 필터)
        cur.execute("""
            SELECT datetime, port_entering
            FROM kfw_ebp_trjdata
            WHERE mmsi = %s AND datetime >= %s AND datetime < %s
            ORDER BY datetime
        """, (mmsi, START_DATE, END_DATE))
        records = cur.fetchall()

        if not records:
            with counter_lock:
                counter.value += 1
            return (mmsi, 0, 0, counter.value, total_ships)

        # 2. 실제 입항 구간 판별 (3시간 이상 port_entering=True)
        real_port_indices = identify_port_segments(records)

        # 3. 항차번호 부여
        # 상태: in_port(실제 입항 중) / at_sea(항해 중)
        # 항차는 입항 구간이 끝나고 출항하는 시점에 새로 시작
        voyage_assignments = []  # [(start_dt, end_dt, voyage_num), ...]

        year_count = {}  # {YY: 횟수} - 연도별 항차 순번

        prev_in_real_port = (0 in real_port_indices)

        # 첫 레코드가 항구가 아니면 → 이미 항해 중 (항차 시작)
        if not prev_in_real_port:
            dt = records[0][0]
            yy = dt.year % 100  # 2자리 연도
            year_count[yy] = year_count.get(yy, 0) + 1
            current_voyage_num = yy * 1000 + year_count[yy]
        else:
            current_voyage_num = None

        # 각 레코드에 voyage_num 매핑
        voyage_nums = [None] * len(records)

        for i in range(len(records)):
            in_real_port = (i in real_port_indices)

            if prev_in_real_port and not in_real_port:
                # 입항 → 출항 전환 = 새 항차 시작
                dt = records[i][0]
                yy = dt.year % 100
                year_count[yy] = year_count.get(yy, 0) + 1
                current_voyage_num = yy * 1000 + year_count[yy]

            if not in_real_port and current_voyage_num is not None:
                voyage_nums[i] = current_voyage_num
            elif in_real_port and current_voyage_num is not None:
                # 입항 구간에도 직전 항차번호 유지
                voyage_nums[i] = current_voyage_num

            prev_in_real_port = in_real_port

        # 4. DB 업데이트 (배치)
        # voyage_num 별로 시간 범위를 묶어서 업데이트
        update_count = 0

        # (voyage_num, start_dt, end_dt) 묶기
        segments = []
        seg_start = 0
        for i in range(1, len(records)):
            if voyage_nums[i] != voyage_nums[seg_start]:
                if voyage_nums[seg_start] is not None:
                    segments.append((voyage_nums[seg_start], records[seg_start][0], records[i - 1][0]))
                seg_start = i
        # 마지막 세그먼트
        if voyage_nums[seg_start] is not None:
            segments.append((voyage_nums[seg_start], records[seg_start][0], records[-1][0]))

        for vnum, start_dt, end_dt in segments:
            cur.execute("""
                UPDATE kfw_ebp_trjdata
                SET voyage_num = %s
                WHERE mmsi = %s AND datetime >= %s AND datetime <= %s
            """, (vnum, mmsi, start_dt, end_dt))
            update_count += cur.rowcount

        conn.commit()
        cur.close()

        voyage_count = len(set(v for v in voyage_nums if v is not None))

        with counter_lock:
            counter.value += 1
            current = counter.value

        return (mmsi, len(records), voyage_count, current, total_ships)

    except Exception as e:
        if conn:
            try: conn.rollback()
            except: pass
        with counter_lock:
            counter.value += 1
            current = counter.value
        return (mmsi, -1, 0, current, total_ships, str(e))

    finally:
        if conn:
            try: conn.close()
            except: pass


def main():
    print("=" * 60)
    print(f"항차번호(voyage_num) 계산 (멀티프로세스 x{NUM_WORKERS})")
    print("=" * 60)

    # 1. MMSI 목록 조회
    mmsi_list = get_mmsi_list()
    total_ships = len(mmsi_list)
    print(f"대상 선박: {total_ships}척")
    print()

    tasks = [(mmsi, total_ships) for mmsi in mmsi_list]

    # 2. 멀티프로세스 실행
    c = Value('i', 0)
    l = Lock()

    total_voyages = 0
    total_records = 0
    error_count = 0

    try:
        with Pool(processes=NUM_WORKERS, initializer=init_counter, initargs=(c, l)) as pool:
            for result in pool.imap_unordered(calc_voyage_for_ship, tasks):
                if len(result) == 6:
                    mmsi, rec_count, voy_count, current, total, err = result
                    print(f"  [오류 {current}/{total}] MMSI {mmsi}: {err}")
                    error_count += 1
                else:
                    mmsi, rec_count, voy_count, current, total = result
                    if rec_count > 0:
                        total_records += rec_count
                        total_voyages += voy_count
                        print(f"  [{current}/{total}] MMSI {mmsi} → {rec_count:,}건, "
                              f"{voy_count}개 항차 | 누적: {total_voyages}항차")

    except KeyboardInterrupt:
        print("\n\n중단됨! 지금까지 업데이트된 데이터는 유지됩니다.")

    print()
    print("=" * 60)
    print(f"완료! {total_records:,}건 처리 | {total_voyages}개 항차 | {error_count}건 오류")
    print("=" * 60)


if __name__ == "__main__":
    main()
