"""
Step 2+3: 항차 구분 + 조업 분류 (GPU 싱글)
- ship_{mmsi} 테이블이 온전한 선박만 대상
- Step 2: port_entering + voyage_num + kfw_ebp_voyage
- Step 3: 딥러닝 조업 분류 (GPU)

사용법:
    python process_voyage_model.py                # 전체 (미처리만)
    python process_voyage_model.py --mmsi 440137010
    python process_voyage_model.py --force        # 기존 voyage 삭제 후 재처리
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
import tensorflow as tf
tf.get_logger().setLevel('ERROR')
from keras.models import load_model

# === DB 설정 ===
DB_CONFIG = {
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

log_file = os.path.join(MODEL_DIR, f'voyage_model_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
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


def get_ready_ships(conn):
    """ship_{mmsi} 테이블이 있는 전체 선박 조회"""
    cur = conn.cursor()

    cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE 'ship_%' AND schemaname='public'")
    ship_tables = set(r[0] for r in cur.fetchall())

    cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_shipinfo ORDER BY mmsi")
    all_mmsi = [row[0] for row in cur.fetchall()]

    return [m for m in all_mmsi if f"ship_{m}" in ship_tables]


# ============================================================
# Step 2: 항차 구분
# ============================================================

def step2_voyage(mmsi, conn):
    depth_lats, depth_lons, depth_elev = load_depth()
    lat_res = float(depth_lats[1] - depth_lats[0])
    lon_res = float(depth_lons[1] - depth_lons[0])
    cur = conn.cursor()
    ship_table = f"ship_{mmsi}"

    # 데이터 범위 확인
    cur.execute(f"SELECT MIN(datetime), MAX(datetime) FROM {ship_table}")
    dt_range = cur.fetchone()
    if not dt_range[0]:
        return 0, 0

    # 데이터가 있는 연도
    data_start_year = dt_range[0].year
    data_end_year = dt_range[1].year
    all_data_years = set(range(data_start_year, data_end_year + 1))

    # 이미 voyage가 있는 연도
    cur.execute("SELECT DISTINCT EXTRACT(YEAR FROM start_time)::int FROM kfw_ebp_voyage WHERE mmsi = %s", (int(mmsi),))
    done_years = set(r[0] for r in cur.fetchall())

    # 빠진 연도
    missing_years = sorted(all_data_years - done_years)
    if not missing_years:
        logging.info(f"    전체 연도 처리 완료, 스킵")
        return 0, 0

    all_years = missing_years
    logging.info(f"    처리할 연도: {all_years} (기존: {sorted(done_years)})")

    total_records = 0
    all_voyage_data = {}

    for target_year in all_years:
        year_count = {}
        current_vnum = None
        prev_in_port = None

        d = datetime(target_year, 1, 1)
        end = datetime(target_year + 1, 1, 1)
        end = min(end, dt_range[1] + timedelta(days=1))

        while d <= end:
            start_dt = d
            if d.month == 12:
                end_dt = d.replace(year=d.year + 1, month=1)
            else:
                end_dt = d.replace(month=d.month + 1)

            cur.execute(f"""
                SELECT datetime, lon, lat, port_entering, voyage_num
                FROM {ship_table} WHERE datetime >= %s AND datetime < %s
                ORDER BY datetime
            """, (start_dt, end_dt))
            records = cur.fetchall()

            d = end_dt
            if not records:
                continue

            n = len(records)
            total_records += n
            lat_arr = np.array([float(r[2]) for r in records]) / 10000000.0
            lon_arr = np.array([float(r[1]) for r in records]) / 10000000.0
            dt_arr = [r[0] for r in records]

            lat_idx = np.clip(((lat_arr - depth_lats[0]) / lat_res).astype(int), 0, len(depth_lats) - 1)
            lon_idx = np.clip(((lon_arr - depth_lons[0]) / lon_res).astype(int), 0, len(depth_lons) - 1)
            valid = (lat_arr >= depth_lats[0]) & (lat_arr <= depth_lats[-1]) & \
                    (lon_arr >= depth_lons[0]) & (lon_arr <= depth_lons[-1])
            new_port = np.zeros(n, dtype=bool)
            if valid.any():
                new_port[valid] = depth_elev[lat_idx[valid], lon_idx[valid]] >= LAND_THRESHOLD

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

    # kfw_ebp_voyage 저장 (빠진 연도 항차만)
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
        lon_i, lat_i = np.interp(t_i, t_sec, v_lons), np.interp(t_i, t_sec, v_lats)
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
# Step 3: 딥러닝 조업 분류
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

# Rule-based 대상 업종 키워드 → fishing_type 코드
RULE_BASED_BIZ = {
    '근해자망': 4, '연안자망': 4,
    '기타통발': 6,
}
RULE_SOG_THRESHOLD = 5      # 0.5 knots (DB에 10배 저장)
RULE_STANDBY_HOURS = 4


def step3_rule_based(mmsi, conn, fishing_type_code):
    """Rule-based 조업 판별 (자망/통발)"""
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

    total_updated = 0
    for vnum, v_start, v_end in voyage_list:
        cur.execute(f"""
            SELECT datetime, sog, port_entering FROM {ship_table}
            WHERE datetime >= %s AND datetime <= %s ORDER BY datetime
        """, (v_start, v_end))
        rows = cur.fetchall()
        if not rows:
            cur.execute("UPDATE kfw_ebp_voyage SET model='spd_lmt_0_5knot_4hour' WHERE mmsi=%s AND voyage_num=%s AND target_area='total'", (int(mmsi), vnum))
            conn.commit()
            continue

        datetimes = [r[0] for r in rows]
        sogs = [r[1] for r in rows]
        ports = [r[2] for r in rows]
        statuses = [0] * len(rows)

        # SOG < 0.5 + 항구 아님 → 조업(1)
        for i in range(len(rows)):
            if not ports[i] and sogs[i] < RULE_SOG_THRESHOLD:
                statuses[i] = 1

        # 연속 4시간 이상 → 대기(3)
        i = 0
        while i < len(rows):
            if statuses[i] == 1:
                seg_s = i
                while i < len(rows) and statuses[i] == 1:
                    i += 1
                if datetimes[i-1] - datetimes[seg_s] >= timedelta(hours=RULE_STANDBY_HOURS):
                    for j in range(seg_s, i):
                        statuses[j] = 3
            else:
                i += 1

        updates = [(datetimes[i], fishing_type_code, statuses[i]) for i in range(len(rows))]
        if updates:
            cur.execute("CREATE TEMP TABLE tmp_rb (dt TIMESTAMP, ft SMALLINT, fs SMALLINT) ON COMMIT DROP")
            for bi in range(0, len(updates), 5000):
                args = ','.join(cur.mogrify("(%s,%s,%s)", r).decode() for r in updates[bi:bi+5000])
                cur.execute("INSERT INTO tmp_rb VALUES " + args)
            cur.execute(f"UPDATE {ship_table} t SET model_fishing_type=tmp.ft, model_fishing_status=tmp.fs FROM tmp_rb tmp WHERE t.datetime=tmp.dt")
            conn.commit()
            total_updated += len(updates)

        cur.execute("UPDATE kfw_ebp_voyage SET model='spd_lmt_0_5knot_4hour' WHERE mmsi=%s AND voyage_num=%s AND target_area='total'", (int(mmsi), vnum))
        conn.commit()

    return total_updated


def get_ship_biz_type(mmsi, conn):
    """선박 업종 조회"""
    cur = conn.cursor()
    cur.execute("SELECT business_type FROM kfw_ebp_shipinfo WHERE mmsi = %s LIMIT 1", (str(mmsi),))
    row = cur.fetchone()
    return row[0] if row else None


# ============================================================
# Step 4: 항해/대기 시간 계산
# ============================================================

def step4_nav_stby(mmsi, conn):
    """nav_duration/stby_duration 계산 (nav_duration IS NULL 항차 대상)
    - nav: model_fishing_status=0 + 항구제외
    - stby: model_fishing_status=3 + 항구제외
    - 1분 보간 기준 (단위: 시간)
    """
    cur = conn.cursor()
    ship_table = f"ship_{mmsi}"

    cur.execute("""
        SELECT voyage_num, start_time, end_time FROM kfw_ebp_voyage
        WHERE mmsi=%s AND target_area='total' AND nav_duration IS NULL
        ORDER BY voyage_num
    """, (int(mmsi),))
    voyages = cur.fetchall()
    if not voyages:
        return 0

    updated = 0
    for vnum, v_start, v_end in voyages:
        cur.execute(f"""
            SELECT datetime, model_fishing_status, port_entering FROM {ship_table}
            WHERE datetime >= %s AND datetime <= %s ORDER BY datetime
        """, (v_start, v_end))
        rows = cur.fetchall()

        if len(rows) < 2:
            cur.execute("UPDATE kfw_ebp_voyage SET nav_duration=0, stby_duration=0 WHERE mmsi=%s AND voyage_num=%s AND target_area='total'", (int(mmsi), vnum))
            conn.commit()
            updated += 1
            continue

        datetimes = [r[0] for r in rows]
        statuses = [r[1] for r in rows]
        ports = [r[2] for r in rows]

        t0 = datetimes[0]
        t_sec = np.array([(t - t0).total_seconds() for t in datetimes])
        total_sec = t_sec[-1]

        if total_sec <= 60:
            cur.execute("UPDATE kfw_ebp_voyage SET nav_duration=0, stby_duration=0 WHERE mmsi=%s AND voyage_num=%s AND target_area='total'", (int(mmsi), vnum))
            conn.commit()
            updated += 1
            continue

        t_interp = np.arange(0, total_sec, 60)
        indices = np.clip(np.searchsorted(t_sec, t_interp, side='right') - 1, 0, len(rows) - 1)

        nav_min = 0
        stby_min = 0
        for idx in indices:
            if ports[idx]:
                continue
            s = statuses[idx]
            if s == 0:
                nav_min += 1
            elif s == 3:
                stby_min += 1

        cur.execute("""
            UPDATE kfw_ebp_voyage SET nav_duration=%s, stby_duration=%s
            WHERE mmsi=%s AND voyage_num=%s AND target_area='total'
        """, (nav_min / 60.0, stby_min / 60.0, int(mmsi), vnum))
        conn.commit()
        updated += 1

    return updated


def step3_predict(mmsi, conn):
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

    logging.info(f"    GPU 예측: {len(voyage_prepared)}개 항차")

    # Type 배치
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

    # Behavior 배치
    bg, bw = {}, {}
    for vnum, (tc, tn) in voyage_types.items():
        bf = BEHAVIOR_FILES.get(tn)
        if not bf:
            continue
        bwin = BEHAVIOR_WINDOWS.get(tn, 360)
        fa = voyage_prepared[vnum]['feat_arr']
        if len(fa) < bwin:
            continue
        wins = [fa[i:i+bwin] for i in range(0, len(fa)-bwin+1, BEHAVIOR_STRIDE)]
        if not wins:
            continue
        bg.setdefault(tn, [])
        bw.setdefault(tn, [])
        si = len(bw[tn])
        bw[tn].extend(wins)
        bg[tn].append((vnum, si, len(wins), len(fa)))

    vs = {}
    for tn, groups in bg.items():
        bmodel = _load_model(os.path.join(BEHAVIOR_MODEL_DIR, f'{BEHAVIOR_FILES[tn]}_model.h5'))
        aw = np.array(bw[tn], dtype=np.float32)
        ap = bmodel.predict(aw, batch_size=512, verbose=0)
        for vnum, si, nw, fl in groups:
            p = ap[si:si+nw]
            combined = np.zeros((fl, p.shape[2]))
            counts = np.zeros(fl)
            for i in range(len(p)):
                s = i * BEHAVIOR_STRIDE
                e = min(s + p.shape[1], fl)
                combined[s:e] += p[i, :e-s, :]
                counts[s:e] += 1
            v = counts > 0
            combined[v] /= counts[v, None]
            vs[vnum] = np.argmax(combined, axis=1)

    # DB UPDATE (항차별 배치)
    predicted = 0
    for vnum in sorted(voyage_prepared):
        if vnum not in voyage_types:
            continue
        tc, tn = voyage_types[vnum]
        sa = vs.get(vnum, np.zeros(len(voyage_prepared[vnum]['feat_arr']), dtype=int))

        updates = []
        for odt in voyage_prepared[vnum]['orig_dts']:
            idx = min(int(np.searchsorted(voyage_prepared[vnum]['interp_dts'], odt)), len(sa)-1)
            updates.append((pd.Timestamp(odt).to_pydatetime(), tc, int(sa[idx])))

        if updates:
            cur.execute("CREATE TEMP TABLE tmp_p (dt TIMESTAMP, ft SMALLINT, fs SMALLINT) ON COMMIT DROP")
            for bi in range(0, len(updates), 5000):
                args = ','.join(cur.mogrify("(%s,%s,%s)", r).decode() for r in updates[bi:bi+5000])
                cur.execute("INSERT INTO tmp_p VALUES " + args)
            cur.execute(f"UPDATE {ship_table} t SET model_fishing_type=tmp.ft, model_fishing_status=tmp.fs FROM tmp_p tmp WHERE t.datetime=tmp.dt")
            conn.commit()
            predicted += len(updates)

        # 항차별 voyage model 마킹
        cur.execute("UPDATE kfw_ebp_voyage SET model='model_1.0' WHERE mmsi=%s AND target_area='total' AND voyage_num=%s", (int(mmsi), vnum))
        conn.commit()

        logging.info(f"    항차 {vnum}: {len(updates):,}건 → {tc}({tn})")

    return predicted


# ============================================================
# 멀티프로세스 워커
# ============================================================

NUM_WORKERS = 1

def process_one_ship(mmsi):
    """한 선박의 Step 2+3 처리 (멀티프로세스 워커)"""
    conn = None
    try:
        t0 = time.time()
        conn = psycopg2.connect(**DB_CONFIG)

        # Step 2: 항차 구분
        records, voyages = step2_voyage(mmsi, conn)

        # Step 3: 업종에 따라 분기
        biz_type = get_ship_biz_type(mmsi, conn) or ''
        rule_code = None
        for keyword, code in RULE_BASED_BIZ.items():
            if keyword in biz_type:
                rule_code = code
                break

        if rule_code is not None:
            method = f'Rule({biz_type})'
            predicted = step3_rule_based(mmsi, conn, rule_code)
        else:
            method = f'DL({biz_type})'
            predicted = step3_predict(mmsi, conn)

        # Step 4: nav/stby duration 계산
        nav_updated = step4_nav_stby(mmsi, conn)

        conn.close()
        elapsed = time.time() - t0
        return (mmsi, records, voyages, predicted, method, elapsed, None, nav_updated)

    except Exception as e:
        if conn:
            try: conn.close()
            except: pass
        return (mmsi, 0, 0, 0, '', 0, str(e), 0)


# ============================================================
# 메인
# ============================================================

def main():
    from multiprocessing import Pool

    parser = argparse.ArgumentParser(description='Step 2+3: 항차 구분 + 조업 분류 (CPU 멀티)')
    parser.add_argument('--mmsi', type=int, default=None)
    parser.add_argument('--workers', type=int, default=NUM_WORKERS)
    args = parser.parse_args()

    logging.info("=" * 60)
    gpus = tf.config.list_physical_devices('GPU')
    logging.info(f"Step 2+3: 항차 구분 + 조업 분류 (x{args.workers}, {'GPU' if gpus else 'CPU'})")
    logging.info("=" * 60)

    conn = psycopg2.connect(**DB_CONFIG)

    if args.mmsi:
        mmsi_list = [str(args.mmsi)]
    else:
        mmsi_list = get_ready_ships(conn)

    conn.close()

    logging.info(f"대상: {len(mmsi_list)}척")
    logging.info("")

    total_records = 0
    total_voyages = 0
    total_predicted = 0
    total_nav = 0
    completed = 0

    try:
        with Pool(processes=args.workers) as pool:
            for result in pool.imap_unordered(process_one_ship, mmsi_list):
                mmsi, records, voyages, predicted, method, elapsed, err, nav_updated = result
                completed += 1
                if err:
                    logging.error(f"  [{completed}/{len(mmsi_list)}] MMSI {mmsi} 오류: {err}")
                else:
                    total_records += records
                    total_voyages += voyages
                    total_predicted += predicted
                    total_nav += nav_updated
                    logging.info(f"  [{completed}/{len(mmsi_list)}] MMSI {mmsi}: "
                                 f"{records:,}건 {voyages}항차 {predicted:,}건 {method} | nav/stby {nav_updated}항차 | {elapsed:.0f}초 | "
                                 f"누적: {total_predicted:,}")
    except KeyboardInterrupt:
        logging.info("\n중단됨!")

    logging.info("")
    logging.info("=" * 60)
    logging.info(f"완료! {total_records:,}건 | {total_voyages}항차 | {total_predicted:,}건 예측 | nav/stby {total_nav}항차")
    logging.info("=" * 60)


if __name__ == '__main__':
    main()
