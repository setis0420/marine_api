"""
port_entering 재계산 스크립트
- kfw_ebp_trjdata의 모든 데이터에 대해 수심 기준 -10m으로 재판정
- 배치 단위로 UPDATE
"""

import psycopg2
import numpy as np
import netCDF4 as nc
import shutil
import os

LOCAL_DB = {
    'host': 'localhost',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420!',
    'port': '5432'
}

DEPTH_TEMP = r'C:\temp\depth.nc'
DEPTH_FILE = r'K:\coding_project\해양수산 데이터 분석 플랫폼\수심\depth_450m_s27.0_w119.0_e137.0.nc'
LAND_THRESHOLD = -10
BATCH_SIZE = 50000  # 한번에 처리할 레코드 수


def main():
    # 수심 데이터 로드
    os.makedirs(os.path.dirname(DEPTH_TEMP), exist_ok=True)
    if not os.path.exists(DEPTH_TEMP):
        shutil.copy2(DEPTH_FILE, DEPTH_TEMP)

    ds = nc.Dataset(DEPTH_TEMP)
    lats = np.array(ds.variables['lat'][:])
    lons = np.array(ds.variables['lon'][:])
    elev = np.array(ds.variables['elevation'][:])
    ds.close()

    lat_res = float(lats[1] - lats[0])
    lon_res = float(lons[1] - lons[0])
    print(f"수심 데이터 로드 완료 (LAND_THRESHOLD = {LAND_THRESHOLD}m)")

    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()

    # 총 건수 확인 (추정치)
    cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname='kfw_ebp_trjdata'")
    total_est = cur.fetchone()[0]
    print(f"총 레코드 (추정): {total_est:,}건")

    # MMSI 목록
    cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_shipinfo ORDER BY mmsi")
    mmsi_list = [row[0] for row in cur.fetchall()]
    print(f"대상 선박: {len(mmsi_list)}척")
    print()

    total_updated = 0

    for idx, mmsi in enumerate(mmsi_list):
        # 해당 선박의 lon, lat 조회
        cur.execute("""
            SELECT ctid, lon, lat, port_entering
            FROM kfw_ebp_trjdata
            WHERE mmsi = %s
        """, (int(mmsi),))
        rows = cur.fetchall()

        if not rows:
            continue

        # 수심 판정 (벡터화)
        lat_arr = np.array([float(r[2]) for r in rows]) / 10000000.0
        lon_arr = np.array([float(r[1]) for r in rows]) / 10000000.0

        lat_idx = ((lat_arr - lats[0]) / lat_res).astype(int)
        lon_idx = ((lon_arr - lons[0]) / lon_res).astype(int)
        lat_idx = np.clip(lat_idx, 0, len(lats) - 1)
        lon_idx = np.clip(lon_idx, 0, len(lons) - 1)

        valid = (lat_arr >= lats[0]) & (lat_arr <= lats[-1]) & \
                (lon_arr >= lons[0]) & (lon_arr <= lons[-1])

        new_port = np.zeros(len(rows), dtype=bool)
        if valid.any():
            new_port[valid] = elev[lat_idx[valid], lon_idx[valid]] >= LAND_THRESHOLD

        # 변경된 것만 UPDATE
        changed = 0
        for i, row in enumerate(rows):
            old_port = row[3] if row[3] is not None else False
            if bool(new_port[i]) != bool(old_port):
                cur.execute("""
                    UPDATE kfw_ebp_trjdata
                    SET port_entering = %s
                    WHERE ctid = %s
                """, (bool(new_port[i]), row[0]))
                changed += 1

        if changed > 0:
            conn.commit()
            total_updated += changed

        print(f"  [{idx+1}/{len(mmsi_list)}] MMSI {mmsi}: {len(rows):,}건 중 {changed:,}건 변경 | 누적: {total_updated:,}건")

    conn.commit()
    cur.close()
    conn.close()

    print()
    print(f"완료! 총 {total_updated:,}건 port_entering 업데이트")


if __name__ == "__main__":
    main()
