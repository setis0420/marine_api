"""
선박별 통합 처리 스크립트
1) port_entering 재계산 (수심 -10m)
2) voyage_num 항차 구분 (입항 30분 이상)
3) 딥러닝 조업 분류 (Type + Behavior)
4) kfw_ebp_voyage 저장 (KFW/EBP/total 체류시간)

사용법:
    python process_ship_all.py                    # 전체
    python process_ship_all.py --mmsi 440137010   # 특정 선박
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
# 읽기/쓰기 모두 171
DB_READ = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}
DB_WRITE = DB_READ

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

# === KFW/EBP 구역 ===
KFW_COORDS = [(130.23,35.44),(130.20,35.48),(130.25,35.56),(130.29,35.58),
              (130.38,35.58),(130.41,35.55),(130.32,35.49),(130.23,35.44)]
EBP_COORDS = [(130.34,35.42),(130.43,35.47),(130.47,35.43),(130.43,35.37),
              (130.40,35.36),(130.34,35.42)]
KFW_POLY = Polygon(KFW_COORDS)
EBP_POLY = Polygon(EBP_COORDS)

# === 기간 설정 ===
START_DATE = '2022-01-01'
END_DATE = '2025-01-01'

# 윈도우 크기
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
log_file = os.path.join(MODEL_DIR, f'process_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# 모델/수심 캐시
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
    lats = np.array(ds.variables['lat'][:])
    lons = np.array(ds.variables['lon'][:])
    elev = np.array(ds.variables['elevation'][:])
    ds.close()
    _depth_data = (lats, lons, elev)
    return _depth_data


def generate_months():
    months = []
    d = datetime.strptime(START_DATE, '%Y-%m-%d')
    end = datetime.strptime(END_DATE, '%Y-%m-%d')
    while d < end:
        months.append((d.year, d.month))
        d = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
    return months


# ============================================================
# Phase 1: port_entering + voyage_num
# ============================================================

def phase1_voyage(mmsi, conn, months):
    """port_entering 재계산 + 항차번호 부여 + DB UPDATE"""
    depth_lats, depth_lons, depth_elev = load_depth()
    lat_res = float(depth_lats[1] - depth_lats[0])
    lon_res = float(depth_lons[1] - depth_lons[0])
    cur = conn.cursor()

    year_count = {}
    current_vnum = None
    prev_in_port = None
    total_records = 0
    all_voyage_data = {}

    for year, month in months:
        start_dt = f"{year}-{month:02d}-01"
        end_dt = f"{year + 1}-01-01" if month == 12 else f"{year}-{month + 1:02d}-01"

        cur.execute("""
            SELECT datetime, lon, lat, port_entering, voyage_num
            FROM kfw_ebp_trjdata
            WHERE mmsi = %s AND datetime >= %s AND datetime < %s
            ORDER BY datetime
        """, (int(mmsi), start_dt, end_dt))
        records = cur.fetchall()
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

        # 입항 구간 판별 (30분 이상)
        real_port = np.zeros(n, dtype=bool)
        i = 0
        while i < n:
            if new_port[i]:
                seg_start = i
                while i < n and new_port[i]:
                    i += 1
                if records[i - 1][0] - records[seg_start][0] >= PORT_STAY_THRESHOLD:
                    real_port[seg_start:i] = True
            else:
                i += 1

        # 항차번호 부여
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

        # DB UPDATE (임시 테이블)
        updates = []
        for i in range(n):
            p_val = bool(new_port[i])
            v_val = voyage_nums[i]
            old_p = records[i][3] if records[i][3] is not None else False
            old_v = records[i][4]
            if p_val != old_p or v_val != old_v:
                updates.append((dt_arr[i], p_val, v_val))

        if updates:
            cur.execute("CREATE TEMP TABLE tmp_v (dt TIMESTAMP, port BOOLEAN, vnum INTEGER) ON COMMIT DROP")
            for bi in range(0, len(updates), 5000):
                batch = updates[bi:bi + 5000]
                args = ','.join(cur.mogrify("(%s,%s,%s)", row).decode() for row in batch)
                cur.execute("INSERT INTO tmp_v VALUES " + args)
            cur.execute("""
                UPDATE kfw_ebp_trjdata t SET port_entering = tmp.port, voyage_num = tmp.vnum
                FROM tmp_v tmp WHERE t.mmsi = %s AND t.datetime = tmp.dt
            """, (int(mmsi),))
            conn.commit()
        else:
            conn.commit()

        # voyage 데이터 수집
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
    kfw_prep = prep(KFW_POLY)
    ebp_prep = prep(EBP_POLY)
    kfw_b = KFW_POLY.bounds
    ebp_b = EBP_POLY.bounds

    for vnum, vd in all_voyage_data.items():
        v_times, v_lons, v_lats, v_ports = vd['times'], np.array(vd['lons']), np.array(vd['lats']), vd['ports']
        if len(v_times) < 2:
            continue
        v_start, v_end = v_times[0], v_times[-1]

        # 1분 보간 + 속력
        t0 = v_times[0]
        t_sec = np.array([(t - t0).total_seconds() for t in v_times])
        total_sec = t_sec[-1]
        if total_sec <= 0:
            continue
        t_interp = np.arange(0, total_sec, 60)
        if len(t_interp) == 0:
            continue
        lon_i = np.interp(t_interp, t_sec, v_lons)
        lat_i = np.interp(t_interp, t_sec, v_lats)

        # 속력 계산
        sog_i = np.zeros(len(t_interp))
        for j in range(1, len(t_interp)):
            dlat = lat_i[j] - lat_i[j-1]
            dlon = lon_i[j] - lon_i[j-1]
            dist = np.sqrt((dlat*60)**2 + (dlon*60*np.cos(np.radians((lat_i[j]+lat_i[j-1])/2)))**2)
            sog_i[j] = dist / ((t_interp[j]-t_interp[j-1])/3600) if t_interp[j] != t_interp[j-1] else 0
        sog_i[0] = sog_i[1] if len(sog_i) > 1 else 0

        # port 보간
        port_idx = np.clip(np.searchsorted(t_sec, t_interp) - 1, 0, len(v_ports) - 1)
        port_i = np.array([v_ports[j] for j in port_idx])

        # 구역 체류시간 계산
        kfw_min = ebp_min = total_min = 0
        for j in range(len(lon_i)):
            lo, la, sp, pt = lon_i[j], lat_i[j], sog_i[j], port_i[j]
            if sp < 3 and not pt:
                total_min += 1
            if sp < 3:
                if kfw_b[0] <= lo <= kfw_b[2] and kfw_b[1] <= la <= kfw_b[3]:
                    if kfw_prep.contains(Point(lo, la)):
                        kfw_min += 1
                if ebp_b[0] <= lo <= ebp_b[2] and ebp_b[1] <= la <= ebp_b[3]:
                    if ebp_prep.contains(Point(lo, la)):
                        ebp_min += 1

        for area, mins in [('KFW', kfw_min), ('EBP', ebp_min), ('total', total_min)]:
            cur.execute("""
                INSERT INTO kfw_ebp_voyage (mmsi, voyage_num, start_time, end_time, target_area, duration)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (mmsi, voyage_num, target_area) DO UPDATE
                SET start_time=EXCLUDED.start_time, end_time=EXCLUDED.end_time, duration=EXCLUDED.duration
            """, (int(mmsi), vnum, v_start, v_end, area, mins / 60.0))

    conn.commit()
    return total_records, len(all_voyage_data), all_voyage_data


# ============================================================
# Phase 2: 딥러닝 조업 분류
# ============================================================

def interpolate_1min(df):
    if len(df) < 2 or df['datetime'].iloc[-1] - df['datetime'].iloc[0] <= pd.Timedelta(minutes=1):
        return None
    df = df.copy()
    dt_range = pd.date_range(start=df['datetime'].iloc[0].floor('min'), end=df['datetime'].iloc[-1], freq='1min')
    df['real'] = True
    merged = pd.concat([df, pd.DataFrame({'datetime': dt_range})]).set_index('datetime').sort_index()
    merged['sin_cog'] = np.sin(np.radians(merged['cog']))
    merged['cos_cog'] = np.cos(np.radians(merged['cog']))
    for c in ['lat','lon','sog','sin_cog','cos_cog']:
        merged[c] = merged[c].astype(float).interpolate(method='linear')
    merged['cog'] = (np.degrees(np.arctan2(merged['sin_cog'], merged['cos_cog'])) + 360) % 360
    merged['mmsi'] = merged['mmsi'].ffill().bfill()
    merged = merged[merged['real'] != True].drop(columns=['real','sin_cog','cos_cog']).reset_index()
    return merged if len(merged) >= 2 else None


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


def phase2_predict(mmsi, voyage_data, conn_read, conn_write):
    """항차별 조업 분류 (배치 처리)
    - conn_read: 171 (항적 읽기)
    - conn_write: 159 (결과 저장)
    """
    cur_r = conn_read.cursor()
    cur_w = conn_write.cursor()

    # ---- Step 0: 미처리 항차 조회 ----
    cur_r.execute("""
        SELECT voyage_num, start_time, end_time
        FROM kfw_ebp_voyage
        WHERE mmsi = %s AND target_area = 'total' AND (model = 'none' OR model IS NULL)
        ORDER BY voyage_num
    """, (int(mmsi),))
    voyage_list = cur_r.fetchall()

    if not voyage_list:
        return 0

    # ---- Step 1: 모든 항차 전처리 ----
    voyage_prepared = {}

    logging.info(f"    전처리 시작: {len(voyage_list)}개 항차")

    for vi, (vnum, v_start, v_end) in enumerate(voyage_list):
        try:
            logging.info(f"    [{vi+1}/{len(voyage_list)}] 항차 {vnum} 데이터 로드 중...")
            ship_table = f"ship_{mmsi}"
            cur_r.execute(f"""
                SELECT datetime, mmsi, lat/10000000.0, lon/10000000.0, sog/10.0, cog/10.0
                FROM {ship_table}
                WHERE datetime>=%s AND datetime<=%s AND port_entering=false
                  AND lat/10000000.0 BETWEEN -90 AND 90
                  AND lon/10000000.0 BETWEEN -180 AND 180
                ORDER BY datetime
            """, (v_start, v_end))
            rows = cur_r.fetchall()
            if not rows:
                continue

            df = pd.DataFrame(rows, columns=['datetime','mmsi','lat','lon','sog','cog'])
            df[['lat','lon','sog','cog']] = df[['lat','lon','sog','cog']].astype(float)
            df['datetime'] = pd.to_datetime(df['datetime'])

            if len(df) < MIN_WINDOW:
                # 스킵하지만 model은 업데이트 (재처리 방지)
                cur_w.execute("""
                    UPDATE kfw_ebp_voyage SET model='model_1.0_skip'
                    WHERE mmsi=%s AND voyage_num=%s AND target_area='total'
                """, (int(mmsi), vnum))
                conn_write.commit()
                continue

            interp = interpolate_1min(df)
            if interp is None or len(interp) < MIN_WINDOW:
                cur_w.execute("""
                    UPDATE kfw_ebp_voyage SET model='model_1.0_skip'
                    WHERE mmsi=%s AND voyage_num=%s AND target_area='total'
                """, (int(mmsi), vnum))
                conn_write.commit()
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
                'df': df,
                'feat_arr': feat_arr,
                'interp_dts': interp_feat['datetime'].values,
                'orig_dts': df['datetime'].values,
                'window_size': ws,
            }
        except Exception as e:
            logging.error(f"    항차 {vnum} 전처리 오류: {e}")

    if not voyage_prepared:
        return 0

    logging.info(f"    전처리 완료: {len(voyage_prepared)}개 항차 준비됨")

    # ---- Step 2: Type 예측 (윈도우 크기별 배치) ----
    logging.info(f"    Type 예측 중...")
    type_groups = {}
    for vnum, vp in voyage_prepared.items():
        ws = vp['window_size']
        if ws not in type_groups:
            type_groups[ws] = []
        type_groups[ws].append(vnum)

    voyage_types = {}

    for ws, vnums in type_groups.items():
        batch_data = np.array([voyage_prepared[v]['feat_arr'][-ws:] for v in vnums], dtype=np.float32)
        model = _load_model(os.path.join(TYPE_MODEL_DIR, f'model_{ws}.h5'))
        preds = model.predict(batch_data, batch_size=max(1, len(vnums)), verbose=0)
        pred_codes = np.argmax(preds, axis=1)
        for i, vnum in enumerate(vnums):
            tc = int(pred_codes[i])
            voyage_types[vnum] = (tc, FISHERY_TYPES[tc])

    # ---- Step 3: Behavior 예측 (어업유형별 배치) ----
    behavior_groups = {}
    behavior_windows = {}

    for vnum, (tc, tn) in voyage_types.items():
        bfile = BEHAVIOR_FILES.get(tn)
        if bfile is None:
            continue
        bwin = BEHAVIOR_WINDOWS.get(tn, 360)
        feat_arr = voyage_prepared[vnum]['feat_arr']
        if len(feat_arr) < bwin:
            continue

        windows = [feat_arr[i:i+bwin] for i in range(0, len(feat_arr)-bwin+1, BEHAVIOR_STRIDE)]
        if not windows:
            continue

        if tn not in behavior_groups:
            behavior_groups[tn] = []
            behavior_windows[tn] = []

        start_idx = len(behavior_windows[tn])
        behavior_windows[tn].extend(windows)
        behavior_groups[tn].append((vnum, start_idx, len(windows), len(feat_arr)))

    voyage_status = {}
    logging.info(f"    Behavior 예측 중... ({len(behavior_groups)}개 유형)")

    for tn, groups in behavior_groups.items():
        bfile = BEHAVIOR_FILES[tn]
        bmodel = _load_model(os.path.join(BEHAVIOR_MODEL_DIR, f'{bfile}_model.h5'))

        all_windows = np.array(behavior_windows[tn], dtype=np.float32)
        all_preds = bmodel.predict(all_windows, batch_size=512, verbose=0)

        for vnum, start_idx, n_windows, feat_len in groups:
            preds = all_preds[start_idx:start_idx + n_windows]
            combined = np.zeros((feat_len, preds.shape[2]))
            counts = np.zeros(feat_len)
            for i in range(len(preds)):
                s = i * BEHAVIOR_STRIDE
                e = min(s + preds.shape[1], feat_len)
                combined[s:e] += preds[i, :e-s, :]
                counts[s:e] += 1
            valid = counts > 0
            combined[valid] /= counts[valid, None]
            voyage_status[vnum] = np.argmax(combined, axis=1)

    # ---- Step 4: 결과 저장 (171 직접 UPDATE) ----
    logging.info(f"    DB 저장 중...")
    # 4a. trjdata UPDATE (임시 테이블)
    all_updates = []
    for vnum in voyage_prepared:
        if vnum not in voyage_types:
            continue
        tc, tn = voyage_types[vnum]
        status_arr = voyage_status.get(vnum, np.zeros(len(voyage_prepared[vnum]['feat_arr']), dtype=int))
        orig_dts = voyage_prepared[vnum]['orig_dts']
        interp_dts = voyage_prepared[vnum]['interp_dts']
        for odt in orig_dts:
            idx = min(int(np.searchsorted(interp_dts, odt)), len(status_arr) - 1)
            all_updates.append((pd.Timestamp(odt).to_pydatetime(), tc, int(status_arr[idx])))

    predicted = 0
    if all_updates:
        ship_table = f"ship_{mmsi}"
        cur_w.execute("CREATE TEMP TABLE tmp_p (dt TIMESTAMP, ft SMALLINT, fs SMALLINT) ON COMMIT DROP")
        for bi in range(0, len(all_updates), 5000):
            batch = all_updates[bi:bi + 5000]
            args = ','.join(cur_w.mogrify("(%s,%s,%s)", r).decode() for r in batch)
            cur_w.execute("INSERT INTO tmp_p VALUES " + args)
        cur_w.execute(f"""
            UPDATE {ship_table} t SET model_fishing_type=tmp.ft, model_fishing_status=tmp.fs
            FROM tmp_p tmp WHERE t.datetime=tmp.dt
        """)
        conn_write.commit()
        predicted = len(all_updates)

    # 4b. voyage model='model_1.0' UPDATE
    processed_vnums = list(voyage_prepared.keys())
    if processed_vnums:
        cur_w.execute("""
            UPDATE kfw_ebp_voyage SET model='model_1.0'
            WHERE mmsi=%s AND target_area='total' AND voyage_num = ANY(%s)
        """, (int(mmsi), processed_vnums))
        conn_write.commit()

    # 로그
    for vnum in sorted(voyage_prepared):
        if vnum in voyage_types:
            tc, tn = voyage_types[vnum]
            cnt = len(voyage_prepared[vnum]['orig_dts'])
            logging.info(f"    항차 {vnum}: {cnt:,}건 → 유형={tc}({tn})")

    return predicted


# ============================================================
# 메인
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='조업 분류 (Phase 2 only)')
    parser.add_argument('--mmsi', type=int, default=None)
    args = parser.parse_args()

    gpus = tf.config.list_physical_devices('GPU')
    logging.info("=" * 60)
    logging.info(f"조업 분류 실행 (GPU: {'있음' if gpus else 'CPU'})")
    logging.info(f"로그: {log_file}")
    logging.info("=" * 60)

    conn_read = psycopg2.connect(**DB_READ)
    conn_write = psycopg2.connect(**DB_WRITE)
    cur = conn_read.cursor()

    if args.mmsi:
        mmsi_list = [str(args.mmsi)]
    else:
        # 171에서 전체 선박
        cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_voyage WHERE target_area = 'total' ORDER BY mmsi")
        mmsi_list = [row[0] for row in cur.fetchall()]

    logging.info(f"대상: {len(mmsi_list)}척 (미처리)")
    logging.info(f"읽기: {DB_READ['host']} | 쓰기: {DB_WRITE['host']}")
    logging.info("")

    total_predicted = 0
    total_ships_done = 0

    for idx, mmsi in enumerate(mmsi_list):
        try:
            t0 = time.time()

            predicted = phase2_predict(mmsi, None, conn_read, conn_write)

            total_predicted += predicted
            if predicted > 0:
                total_ships_done += 1
            elapsed = time.time() - t0

            logging.info(f"  [{idx+1}/{len(mmsi_list)}] MMSI {mmsi}: "
                         f"{predicted:,}건 예측 | {elapsed:.0f}초 | "
                         f"누적: {total_predicted:,}건")

        except Exception as e:
            logging.error(f"  [{idx+1}/{len(mmsi_list)}] MMSI {mmsi} 오류: {e}")
            try:
                conn_read.close()
                conn_write.close()
            except:
                pass
            conn_read = psycopg2.connect(**DB_READ)
            conn_write = psycopg2.connect(**DB_WRITE)

    conn_read.close()
    conn_write.close()
    logging.info("")
    logging.info("=" * 60)
    logging.info(f"완료! {total_ships_done}척 | {total_predicted:,}건 예측")
    logging.info("=" * 60)


if __name__ == '__main__':
    main()
