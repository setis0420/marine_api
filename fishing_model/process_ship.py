"""
선박 종합 처리 스크립트
1) 21번 서버에서 항적 데이터 fetch → ship_{mmsi} INSERT
2) port_entering + voyage_num 계산 → ship_{mmsi} UPDATE
3) kfw_ebp_voyage 저장 (KFW/EBP/total 체류시간)
4) 딥러닝 조업 분류 → ship_{mmsi} UPDATE + voyage model 마킹

사용법:
    python process_ship.py 440137010 2022-01-01 2024-12-31
    python process_ship.py 440137010                          # 전체 기간
    python process_ship.py --all 2022-01-01 2024-12-31        # 전체 선박
"""

import os
import sys
import re
import argparse
import logging
import time
import shutil
import numpy as np
import pandas as pd
import psycopg2
from datetime import timedelta, datetime
from shapely.geometry import Point, Polygon
from shapely.prepared import prep
from haversine import haversine_vector, Unit

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['CUDA_VISIBLE_DEVICES'] = '-1'  # CPU 모드 강제
import tensorflow as tf
tf.get_logger().setLevel('ERROR')
from keras.models import load_model

# === DB 설정 ===
REMOTE_DB = {
    'host': '203.253.202.21',
    'dbname': 'aisdb',
    'user': 'fishery_readonly_2',
    'password': 'readonly',
    'port': '5432'
}

LOCAL_DB = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

# === 수심 설정 ===
DEPTH_TEMP = r'C:\temp\depth.nc'
DEPTH_FILE = r'K:\coding_project\해양수산 데이터 분석 플랫폼\수심\depth_450m_s27.0_w119.0_e137.0.nc'
LAND_THRESHOLD = -10
PORT_STAY_THRESHOLD = timedelta(minutes=30)

# === 모델 설정 ===
MODEL_DIR = r'C:\fishing_model'
TYPE_MODEL_DIR = os.path.join(MODEL_DIR, 'models', 'type_model')
BEHAVIOR_MODEL_DIR = os.path.join(MODEL_DIR, 'models', 'behavior_model')
BEHAVIOR_STRIDE = 10

FISHERY_TYPES = ['권현망', '선망', '안강망', '연승', '자망', '채낚기', '통발', '트롤']
BEHAVIOR_WINDOWS = {
    '자망': 360, '연승': 360, '통발': 360,
    '안강망': 180, '권현망': 180,
    '트롤': 60, '채낚기': 60, '선망': 30,
}
BEHAVIOR_FILES = {
    '선망': 'sunmang', '자망': 'jamang', '안강망': 'angangmang',
    '채낚기': 'chaenakgi', '권현망': 'kwonhyunmang',
    '통발': 'tongbal', '트롤': 'troll', '연승': 'yeonseung',
}

KFW_COORDS = [(130.23,35.44),(130.20,35.48),(130.25,35.56),(130.29,35.58),
              (130.38,35.58),(130.41,35.55),(130.32,35.49),(130.23,35.44)]
EBP_COORDS = [(130.34,35.42),(130.43,35.47),(130.47,35.43),(130.43,35.37),
              (130.40,35.36),(130.34,35.42)]
KFW_POLY = Polygon(KFW_COORDS)
EBP_POLY = Polygon(EBP_COORDS)

def _get_window_sizes():
    sizes = []
    for f in os.listdir(TYPE_MODEL_DIR):
        m = re.search(r'model_(\d+)\.h5', f)
        if m:
            sizes.append(int(m.group(1)))
    return sorted(sizes, reverse=True)

WINDOW_SIZES = _get_window_sizes()
MIN_WINDOW = WINDOW_SIZES[-1] if WINDOW_SIZES else 360

# 로그
log_file = os.path.join(MODEL_DIR, f'ship_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

_model_cache = {}
_depth_data = None

def _load_model(path):
    if path not in _model_cache:
        _model_cache[path] = load_model(path, compile=False)
    return _model_cache[path]

def load_depth():
    global _depth_data
    if _depth_data is not None:
        return _depth_data
    import netCDF4 as nc
    os.makedirs(os.path.dirname(DEPTH_TEMP), exist_ok=True)
    if not os.path.exists(DEPTH_TEMP):
        shutil.copy2(DEPTH_FILE, DEPTH_TEMP)
    ds = nc.Dataset(DEPTH_TEMP)
    _depth_data = (np.array(ds.variables['lat'][:]), np.array(ds.variables['lon'][:]), np.array(ds.variables['elevation'][:]))
    ds.close()
    return _depth_data


# ============================================================
# Phase 1: 21번 서버에서 데이터 fetch
# ============================================================

def phase1_fetch(mmsi, start_date, end_date, conn_local):
    """21번 서버에서 데이터 가져와서 ship_{mmsi}에 INSERT"""
    cur = conn_local.cursor()
    ship_table = f"ship_{mmsi}"

    # 테이블 존재 확인, 없으면 생성
    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)", (ship_table,))
    if not cur.fetchone()[0]:
        cur.execute(f"""
            CREATE TABLE {ship_table} (
                mmsi INTEGER, datetime TIMESTAMP, rot SMALLINT, sog SMALLINT,
                lon INTEGER, lat INTEGER, cog SMALLINT, heading SMALLINT,
                model_fishing_type SMALLINT, model_fishing_status SMALLINT,
                port_entering BOOLEAN, voyage_num SMALLINT
            )
        """)
        cur.execute(f"CREATE INDEX ON {ship_table} (datetime)")
        cur.execute(f"CREATE UNIQUE INDEX ON {ship_table} (mmsi, datetime)")
        cur.execute(f"CREATE INDEX ON {ship_table} (voyage_num)")
        conn_local.commit()
        logging.info(f"  테이블 {ship_table} 생성")

    # 마지막 저장 시점 확인
    cur.execute(f"SELECT MAX(datetime) FROM {ship_table}")
    last_dt = cur.fetchone()[0]

    if last_dt and last_dt >= datetime.strptime(end_date, '%Y-%m-%d'):
        logging.info(f"  Phase 1: 이미 {end_date}까지 데이터 있음, 스킵")
        return 0

    fetch_start = last_dt if last_dt else datetime.strptime(start_date, '%Y-%m-%d')

    # 수심 데이터 로드 (port_entering 판정)
    depth_lats, depth_lons, depth_elev = load_depth()
    lat_res = float(depth_lats[1] - depth_lats[0])
    lon_res = float(depth_lons[1] - depth_lons[0])

    # 월별로 21번 서버에서 가져오기
    total_inserted = 0
    d = fetch_start
    end = datetime.strptime(end_date, '%Y-%m-%d')

    remote_conn = psycopg2.connect(**REMOTE_DB)

    while d < end:
        table = f"db{d.year}{d.month:02d}"
        month_start = max(d, datetime(d.year, d.month, 1))
        if d.month == 12:
            month_end = min(datetime(d.year + 1, 1, 1), end)
        else:
            month_end = min(datetime(d.year, d.month + 1, 1), end)

        try:
            remote_cur = remote_conn.cursor()
            remote_cur.execute(f"""
                SELECT DISTINCT mmsi, datetime, rot, sog, lon, lat, cog, heading
                FROM {table}
                WHERE mmsi = %s AND datetime > %s AND datetime < %s
                  AND lon <> 0 AND lat <> 0
                ORDER BY datetime
            """, (int(mmsi), month_start, month_end))
            rows = remote_cur.fetchall()
            remote_cur.close()

            if rows:
                # port_entering 판정
                lat_arr = np.array([float(r[5]) for r in rows]) / 10000000.0
                lon_arr = np.array([float(r[4]) for r in rows]) / 10000000.0
                lat_idx = np.clip(((lat_arr - depth_lats[0]) / lat_res).astype(int), 0, len(depth_lats) - 1)
                lon_idx = np.clip(((lon_arr - depth_lons[0]) / lon_res).astype(int), 0, len(depth_lons) - 1)
                valid = (lat_arr >= depth_lats[0]) & (lat_arr <= depth_lats[-1]) & \
                        (lon_arr >= depth_lons[0]) & (lon_arr <= depth_lons[-1])
                port_flags = np.zeros(len(rows), dtype=bool)
                if valid.any():
                    port_flags[valid] = depth_elev[lat_idx[valid], lon_idx[valid]] >= LAND_THRESHOLD

                # INSERT
                batch_size = 5000
                for i in range(0, len(rows), batch_size):
                    batch = rows[i:i + batch_size]
                    batch_ports = port_flags[i:i + batch_size]
                    values = [row + (bool(pf),) for row, pf in zip(batch, batch_ports)]
                    args = ','.join(
                        cur.mogrify("(%s,%s,%s,%s,%s,%s,%s,%s,%s)", v).decode() for v in values
                    )
                    cur.execute(f"""
                        INSERT INTO {ship_table} (mmsi, datetime, rot, sog, lon, lat, cog, heading, port_entering)
                        VALUES {args}
                        ON CONFLICT (mmsi, datetime) DO NOTHING
                    """)
                conn_local.commit()
                total_inserted += len(rows)
                logging.info(f"  Phase 1: {table} → {len(rows):,}건 INSERT (누적: {total_inserted:,})")

        except Exception as e:
            logging.error(f"  Phase 1: {table} 오류: {e}")
            conn_local.rollback()
            try:
                remote_conn.close()
            except:
                pass
            remote_conn = psycopg2.connect(**REMOTE_DB)

        # 다음 달
        if d.month == 12:
            d = datetime(d.year + 1, 1, 1)
        else:
            d = datetime(d.year, d.month + 1, 1)

    remote_conn.close()
    return total_inserted


# ============================================================
# Phase 2: port_entering + voyage_num + kfw_ebp_voyage
# ============================================================

def phase2_voyage(mmsi, start_date, end_date, conn):
    """항차 구분 + voyage 테이블 저장"""
    depth_lats, depth_lons, depth_elev = load_depth()
    lat_res = float(depth_lats[1] - depth_lats[0])
    lon_res = float(depth_lons[1] - depth_lons[0])
    cur = conn.cursor()
    ship_table = f"ship_{mmsi}"

    # 기존 voyage 데이터 삭제 (새로 계산)
    cur.execute("DELETE FROM kfw_ebp_voyage WHERE mmsi = %s", (int(mmsi),))
    conn.commit()

    year_count = {}
    current_vnum = None
    prev_in_port = None
    total_records = 0
    all_voyage_data = {}

    # 월별 처리
    d = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')

    while d < end:
        start_dt = f"{d.year}-{d.month:02d}-01"
        if d.month == 12:
            end_dt = f"{d.year + 1}-01-01"
        else:
            end_dt = f"{d.year}-{d.month + 1:02d}-01"

        cur.execute(f"""
            SELECT datetime, lon, lat, port_entering, voyage_num
            FROM {ship_table} WHERE datetime >= %s AND datetime < %s
            ORDER BY datetime
        """, (start_dt, end_dt))
        records = cur.fetchall()

        if d.month == 12:
            d = datetime(d.year + 1, 1, 1)
        else:
            d = datetime(d.year, d.month + 1, 1)

        if not records:
            continue

        n = len(records)
        total_records += n
        lat_arr = np.array([float(r[2]) for r in records]) / 10000000.0
        lon_arr = np.array([float(r[1]) for r in records]) / 10000000.0
        dt_arr = [r[0] for r in records]

        # port_entering 재계산
        lat_idx = np.clip(((lat_arr - depth_lats[0]) / lat_res).astype(int), 0, len(depth_lats) - 1)
        lon_idx = np.clip(((lon_arr - depth_lons[0]) / lon_res).astype(int), 0, len(depth_lons) - 1)
        valid = (lat_arr >= depth_lats[0]) & (lat_arr <= depth_lats[-1]) & \
                (lon_arr >= depth_lons[0]) & (lon_arr <= depth_lons[-1])
        new_port = np.zeros(n, dtype=bool)
        if valid.any():
            new_port[valid] = depth_elev[lat_idx[valid], lon_idx[valid]] >= LAND_THRESHOLD

        # 입항 구간 판별
        real_port = np.zeros(n, dtype=bool)
        i = 0
        while i < n:
            if new_port[i]:
                seg_s = i
                while i < n and new_port[i]:
                    i += 1
                if records[i-1][0] - records[seg_s][0] >= PORT_STAY_THRESHOLD:
                    real_port[seg_s:i] = True
            else:
                i += 1

        # 항차번호
        voyage_nums = [None] * n
        if prev_in_port is None:
            prev_in_port = bool(real_port[0])
            if not prev_in_port:
                yy = records[0][0].year % 100
                year_count[yy] = year_count.get(yy, 0) + 1
                current_vnum = yy * 1000 + year_count[yy]

        for i in range(n):
            in_port = bool(real_port[i])
            if prev_in_port and not in_port:
                yy = records[i][0].year % 100
                year_count[yy] = year_count.get(yy, 0) + 1
                current_vnum = yy * 1000 + year_count[yy]
            if current_vnum is not None:
                voyage_nums[i] = current_vnum
            prev_in_port = in_port

        # UPDATE
        updates = [(dt_arr[i], bool(new_port[i]), voyage_nums[i]) for i in range(n)
                    if bool(new_port[i]) != (records[i][3] or False) or voyage_nums[i] != records[i][4]]
        if updates:
            cur.execute("CREATE TEMP TABLE tmp_v (dt TIMESTAMP, port BOOLEAN, vnum INTEGER) ON COMMIT DROP")
            for bi in range(0, len(updates), 5000):
                args = ','.join(cur.mogrify("(%s,%s,%s)", r).decode() for r in updates[bi:bi+5000])
                cur.execute("INSERT INTO tmp_v VALUES " + args)
            cur.execute(f"UPDATE {ship_table} t SET port_entering=tmp.port, voyage_num=tmp.vnum FROM tmp_v tmp WHERE t.datetime=tmp.dt")
            conn.commit()
        else:
            conn.commit()

        # voyage 수집
        for i in range(n):
            vnum = voyage_nums[i]
            if vnum is None:
                continue
            if vnum not in all_voyage_data:
                all_voyage_data[vnum] = {'times': [], 'lons': [], 'lats': [], 'ports': []}
            all_voyage_data[vnum]['times'].append(dt_arr[i])
            all_voyage_data[vnum]['lons'].append(lon_arr[i])
            all_voyage_data[vnum]['lats'].append(lat_arr[i])
            all_voyage_data[vnum]['ports'].append(bool(new_port[i]))

    # kfw_ebp_voyage 저장
    kfw_prep, ebp_prep = prep(KFW_POLY), prep(EBP_POLY)
    kfw_b, ebp_b = KFW_POLY.bounds, EBP_POLY.bounds

    for vnum, vd in all_voyage_data.items():
        v_times, v_lons, v_lats, v_ports = vd['times'], np.array(vd['lons']), np.array(vd['lats']), vd['ports']
        if len(v_times) < 2:
            continue

        t0 = v_times[0]
        t_sec = np.array([(t - t0).total_seconds() for t in v_times])
        if t_sec[-1] <= 0:
            continue
        t_i = np.arange(0, t_sec[-1], 60)
        if len(t_i) == 0:
            continue
        lon_i = np.interp(t_i, t_sec, v_lons)
        lat_i = np.interp(t_i, t_sec, v_lats)

        sog_i = np.zeros(len(t_i))
        for j in range(1, len(t_i)):
            dl, dn = lat_i[j]-lat_i[j-1], lon_i[j]-lon_i[j-1]
            sog_i[j] = np.sqrt((dl*60)**2 + (dn*60*np.cos(np.radians((lat_i[j]+lat_i[j-1])/2)))**2) / ((t_i[j]-t_i[j-1])/3600) if t_i[j] != t_i[j-1] else 0
        sog_i[0] = sog_i[1] if len(sog_i) > 1 else 0

        p_idx = np.clip(np.searchsorted(t_sec, t_i) - 1, 0, len(v_ports) - 1)
        port_i = np.array([v_ports[j] for j in p_idx])

        kfw_m = ebp_m = total_m = 0
        for j in range(len(lon_i)):
            lo, la, sp, pt = lon_i[j], lat_i[j], sog_i[j], port_i[j]
            if sp < 3 and not pt:
                total_m += 1
            if sp < 3:
                if kfw_b[0] <= lo <= kfw_b[2] and kfw_b[1] <= la <= kfw_b[3] and kfw_prep.contains(Point(lo, la)):
                    kfw_m += 1
                if ebp_b[0] <= lo <= ebp_b[2] and ebp_b[1] <= la <= ebp_b[3] and ebp_prep.contains(Point(lo, la)):
                    ebp_m += 1

        for area, mins in [('KFW', kfw_m), ('EBP', ebp_m), ('total', total_m)]:
            cur.execute("""
                INSERT INTO kfw_ebp_voyage (mmsi, voyage_num, start_time, end_time, target_area, duration)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (mmsi, voyage_num, target_area) DO UPDATE
                SET start_time=EXCLUDED.start_time, end_time=EXCLUDED.end_time, duration=EXCLUDED.duration
            """, (int(mmsi), vnum, v_times[0], v_times[-1], area, mins / 60.0))

    conn.commit()
    return total_records, len(all_voyage_data)


# ============================================================
# Phase 3: 딥러닝 조업 분류
# ============================================================

def interpolate_1min_df(df):
    if len(df) < 2 or df['datetime'].iloc[-1] - df['datetime'].iloc[0] <= pd.Timedelta(minutes=1):
        return None
    df = df.copy()
    dt_range = pd.date_range(start=df['datetime'].iloc[0].floor('min'), end=df['datetime'].iloc[-1], freq='1min')
    df['real'] = True
    m = pd.concat([df, pd.DataFrame({'datetime': dt_range})]).set_index('datetime').sort_index()
    m['sin_cog'] = np.sin(np.radians(m['cog']))
    m['cos_cog'] = np.cos(np.radians(m['cog']))
    for c in ['lat','lon','sog','sin_cog','cos_cog']:
        m[c] = m[c].astype(float).interpolate(method='linear')
    m['cog'] = (np.degrees(np.arctan2(m['sin_cog'], m['cos_cog'])) + 360) % 360
    m['mmsi'] = m['mmsi'].ffill().bfill()
    m = m[m['real'] != True].drop(columns=['real','sin_cog','cos_cog']).reset_index()
    return m if len(m) >= 2 else None

def create_features(df):
    df = df.copy()
    df['hour'] = df['datetime'].dt.hour
    df['delta_lat'] = df['lat'].diff().fillna(0)
    df['delta_lon'] = df['lon'].diff().fillna(0)
    lp, lnp = df['lat'].shift(1), df['lon'].shift(1)
    lnx, lny = df['lat'].shift(-1), df['lon'].shift(-1)
    dot = (lp-df['lat'])*(lnx-df['lat']) + (lnp-df['lon'])*(lny-df['lon'])
    mag = np.sqrt((lp-df['lat'])**2+(lnp-df['lon'])**2) * np.sqrt((lnx-df['lat'])**2+(lny-df['lon'])**2) + 1e-10
    df['angle'] = np.degrees(np.arccos(np.clip(dot/mag, -1, 1))).fillna(0)
    theta = np.degrees(np.arctan(df['delta_lat']/(df['delta_lon']+1e-10))).fillna(0)
    conds = [(df['delta_lon']>0)&(df['delta_lat']>0),(df['delta_lon']>0)&(df['delta_lat']<0),
             (df['delta_lon']<0)&(df['delta_lat']<0),(df['delta_lon']<0)&(df['delta_lat']>0)]
    Co = np.select(conds, [90-theta,90+theta,270-theta,270+theta], 0)
    Cd = np.abs(np.diff(Co, prepend=Co[0]))
    df['Cc'] = np.where(Cd>=180, 360-Cd, Cd)
    df['spd'] = np.sqrt(df['delta_lat']**2+df['delta_lon']**2)*60
    df.loc[df.index[0],'spd'] = 0
    coords = df[['lat','lon']].values
    hav = haversine_vector(coords[:-1], np.roll(coords,-1,axis=0)[:-1], Unit.KILOMETERS)
    df['haversine'] = np.concatenate([[0], hav])
    return df, ['hour','angle','haversine','Cc','spd']

def phase3_predict(mmsi, conn):
    """조업 분류 (voyage 테이블 기반, 배치)"""
    cur = conn.cursor()
    ship_table = f"ship_{mmsi}"

    cur.execute("""
        SELECT voyage_num, start_time, end_time FROM kfw_ebp_voyage
        WHERE mmsi=%s AND target_area='total' AND (model='none' OR model IS NULL)
        ORDER BY voyage_num
    """, (int(mmsi),))
    voyage_list = cur.fetchall()

    if not voyage_list:
        return 0

    # 전처리
    voyage_prepared = {}
    for vnum, v_start, v_end in voyage_list:
        try:
            cur.execute(f"""
                SELECT datetime, mmsi, lat/10000000.0, lon/10000000.0, sog/10.0, cog/10.0
                FROM {ship_table}
                WHERE datetime>=%s AND datetime<=%s AND port_entering=false
                  AND lat/10000000.0 BETWEEN -90 AND 90 AND lon/10000000.0 BETWEEN -180 AND 180
                ORDER BY datetime
            """, (v_start, v_end))
            rows = cur.fetchall()
            if not rows:
                continue

            df = pd.DataFrame(rows, columns=['datetime','mmsi','lat','lon','sog','cog'])
            df[['lat','lon','sog','cog']] = df[['lat','lon','sog','cog']].astype(float)
            df['datetime'] = pd.to_datetime(df['datetime'])

            if len(df) < MIN_WINDOW:
                cur.execute("UPDATE kfw_ebp_voyage SET model='model_1.0_skip' WHERE mmsi=%s AND voyage_num=%s AND target_area='total'", (int(mmsi), vnum))
                conn.commit()
                continue

            interp = interpolate_1min_df(df)
            if interp is None or len(interp) < MIN_WINDOW:
                cur.execute("UPDATE kfw_ebp_voyage SET model='model_1.0_skip' WHERE mmsi=%s AND voyage_num=%s AND target_area='total'", (int(mmsi), vnum))
                conn.commit()
                continue

            interp_feat, feat_cols = create_features(interp)
            feat_arr = interp_feat[feat_cols].values.astype(np.float32)

            ws = None
            for w in WINDOW_SIZES:
                if len(feat_arr) >= w:
                    ws = w
                    break
            if ws is None:
                continue

            voyage_prepared[vnum] = {
                'feat_arr': feat_arr, 'interp_dts': interp_feat['datetime'].values,
                'orig_dts': df['datetime'].values, 'window_size': ws,
            }
        except Exception as e:
            logging.error(f"    항차 {vnum} 전처리 오류: {e}")

    if not voyage_prepared:
        return 0

    logging.info(f"  Phase 3: {len(voyage_prepared)}개 항차 예측 중...")

    # Type 배치 예측
    type_groups = {}
    for vnum, vp in voyage_prepared.items():
        type_groups.setdefault(vp['window_size'], []).append(vnum)

    voyage_types = {}
    for ws, vnums in type_groups.items():
        batch = np.array([voyage_prepared[v]['feat_arr'][-ws:] for v in vnums], dtype=np.float32)
        model = _load_model(os.path.join(TYPE_MODEL_DIR, f'model_{ws}.h5'))
        preds = model.predict(batch, batch_size=max(1, len(vnums)), verbose=0)
        for i, vnum in enumerate(vnums):
            tc = int(np.argmax(preds[i]))
            voyage_types[vnum] = (tc, FISHERY_TYPES[tc])

    # Behavior 배치 예측
    behavior_groups, behavior_windows_data = {}, {}
    for vnum, (tc, tn) in voyage_types.items():
        bfile = BEHAVIOR_FILES.get(tn)
        if not bfile:
            continue
        bwin = BEHAVIOR_WINDOWS.get(tn, 360)
        fa = voyage_prepared[vnum]['feat_arr']
        if len(fa) < bwin:
            continue
        windows = [fa[i:i+bwin] for i in range(0, len(fa)-bwin+1, BEHAVIOR_STRIDE)]
        if not windows:
            continue
        behavior_groups.setdefault(tn, [])
        behavior_windows_data.setdefault(tn, [])
        si = len(behavior_windows_data[tn])
        behavior_windows_data[tn].extend(windows)
        behavior_groups[tn].append((vnum, si, len(windows), len(fa)))

    voyage_status = {}
    for tn, groups in behavior_groups.items():
        bmodel = _load_model(os.path.join(BEHAVIOR_MODEL_DIR, f'{BEHAVIOR_FILES[tn]}_model.h5'))
        all_w = np.array(behavior_windows_data[tn], dtype=np.float32)
        all_p = bmodel.predict(all_w, batch_size=512, verbose=0)
        for vnum, si, nw, fl in groups:
            p = all_p[si:si+nw]
            combined = np.zeros((fl, p.shape[2]))
            counts = np.zeros(fl)
            for i in range(len(p)):
                s = i * BEHAVIOR_STRIDE
                e = min(s + p.shape[1], fl)
                combined[s:e] += p[i, :e-s, :]
                counts[s:e] += 1
            v = counts > 0
            combined[v] /= counts[v, None]
            voyage_status[vnum] = np.argmax(combined, axis=1)

    # DB UPDATE
    all_updates = []
    for vnum in voyage_prepared:
        if vnum not in voyage_types:
            continue
        tc, tn = voyage_types[vnum]
        sa = voyage_status.get(vnum, np.zeros(len(voyage_prepared[vnum]['feat_arr']), dtype=int))
        for odt in voyage_prepared[vnum]['orig_dts']:
            idx = min(int(np.searchsorted(voyage_prepared[vnum]['interp_dts'], odt)), len(sa)-1)
            all_updates.append((pd.Timestamp(odt).to_pydatetime(), tc, int(sa[idx])))

    predicted = 0
    if all_updates:
        cur.execute("CREATE TEMP TABLE tmp_p (dt TIMESTAMP, ft SMALLINT, fs SMALLINT) ON COMMIT DROP")
        for bi in range(0, len(all_updates), 5000):
            args = ','.join(cur.mogrify("(%s,%s,%s)", r).decode() for r in all_updates[bi:bi+5000])
            cur.execute("INSERT INTO tmp_p VALUES " + args)
        cur.execute(f"UPDATE {ship_table} t SET model_fishing_type=tmp.ft, model_fishing_status=tmp.fs FROM tmp_p tmp WHERE t.datetime=tmp.dt")
        conn.commit()
        predicted = len(all_updates)

    # voyage model 마킹
    done = list(voyage_prepared.keys())
    if done:
        cur.execute("UPDATE kfw_ebp_voyage SET model='model_1.0' WHERE mmsi=%s AND target_area='total' AND voyage_num=ANY(%s)", (int(mmsi), done))
        conn.commit()

    for vnum in sorted(voyage_prepared):
        if vnum in voyage_types:
            tc, tn = voyage_types[vnum]
            logging.info(f"    항차 {vnum}: {len(voyage_prepared[vnum]['orig_dts']):,}건 → {tc}({tn})")

    return predicted


# ============================================================
# 메인
# ============================================================

NUM_WORKERS = 5


def process_phase3_worker(mmsi):
    """Phase 3: 조업 분류 (멀티프로세스 워커)"""
    try:
        conn = psycopg2.connect(**LOCAL_DB)
        predicted = phase3_predict(mmsi, conn)
        conn.close()
        return (mmsi, predicted, None)
    except Exception as e:
        return (mmsi, 0, str(e))


def process_phase12(args):
    """Phase 1+2: fetch + voyage (멀티프로세스용)"""
    mmsi, start_date, end_date = args
    try:
        t0 = time.time()
        conn = psycopg2.connect(**LOCAL_DB)

        inserted = phase1_fetch(mmsi, start_date, end_date, conn)
        records, voyages = phase2_voyage(mmsi, start_date, end_date, conn)

        conn.close()
        elapsed = time.time() - t0
        return (mmsi, inserted, records, voyages, elapsed, None)
    except Exception as e:
        return (mmsi, 0, 0, 0, 0, str(e))


def main():
    parser = argparse.ArgumentParser(description='선박 종합 처리')
    parser.add_argument('--mmsi', type=int, default=None, help='특정 선박 MMSI')
    parser.add_argument('--start', default='2019-01-01', help='시작일')
    parser.add_argument('--end', default='2022-01-01', help='종료일')
    parser.add_argument('--all', action='store_true', help='전체 선박')
    parser.add_argument('--workers', type=int, default=NUM_WORKERS, help='프로세스 수')
    args = parser.parse_args()

    logging.info("=" * 60)
    logging.info(f"선박 종합 처리 (Phase1+2: x{args.workers} CPU | Phase3: 싱글 CPU)")
    logging.info(f"기간: {args.start} ~ {args.end}")
    logging.info("=" * 60)

    if args.all or args.mmsi is None:
        conn = psycopg2.connect(**LOCAL_DB)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_shipinfo ORDER BY mmsi")
        mmsi_list = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
    else:
        mmsi_list = [str(args.mmsi)]

    logging.info(f"대상: {len(mmsi_list)}척")
    logging.info("")

    # ---- Phase 1+2: 멀티프로세스 (fetch + voyage) ----
    logging.info("=== Phase 1+2: 데이터 fetch + 항차 구분 (멀티프로세스) ===")
    tasks = [(mmsi, args.start, args.end) for mmsi in mmsi_list]

    from multiprocessing import Pool
    completed = 0
    with Pool(processes=args.workers) as pool:
        for result in pool.imap_unordered(process_phase12, tasks):
            mmsi, inserted, records, voyages, elapsed, err = result
            completed += 1
            if err:
                logging.error(f"  [{completed}/{len(mmsi_list)}] MMSI {mmsi} 오류: {err}")
            else:
                logging.info(f"  [{completed}/{len(mmsi_list)}] MMSI {mmsi}: "
                             f"fetch={inserted:,} | {records:,}건 {voyages}항차 | {elapsed:.0f}초")

    logging.info("")

    # ---- Phase 3: 멀티프로세스 (조업 분류, CPU) ----
    logging.info(f"=== Phase 3: 조업 분류 (x{args.workers} CPU) ===")

    total_predicted = 0
    completed3 = 0
    with Pool(processes=args.workers) as pool:
        for result in pool.imap_unordered(process_phase3_worker, mmsi_list):
            mmsi, predicted, err = result
            completed3 += 1
            if err:
                logging.error(f"  [{completed3}/{len(mmsi_list)}] MMSI {mmsi} 오류: {err}")
            elif predicted > 0:
                total_predicted += predicted
                logging.info(f"  [{completed3}/{len(mmsi_list)}] MMSI {mmsi}: {predicted:,}건 예측 | 누적: {total_predicted:,}")
    logging.info("")
    logging.info("=" * 60)
    logging.info(f"전체 완료! {total_predicted:,}건 예측")
    logging.info("=" * 60)


if __name__ == '__main__':
    main()
