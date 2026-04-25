"""
전체 선박 항차별 조업 분류 스크립트
- update_port_and_voyage.py 실행 후 사용
- 항차별로 항적 추출 → 딥러닝 모델 예측 → DB UPDATE
- 입력: 항차 시작 시 속력 1노트 이상부터 항차 끝까지 (항구 정박 제외)
- GPU 사용 (RTX 3060)

사용법:
    python predict_all_voyages.py
    python predict_all_voyages.py --mmsi 440137010        # 특정 선박만
    python predict_all_voyages.py --workers 1             # 싱글 프로세스
"""

import os
import sys
import argparse
import logging
import time
import numpy as np
import pandas as pd
import psycopg2
from datetime import datetime

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import tensorflow as tf
tf.get_logger().setLevel('ERROR')
from keras.models import load_model

# === 설정 ===
DB_CONFIG = {
    'host': '203.253.202.159',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420!',
    'port': '5432'
}

MODEL_DIR = r'C:\fishing_model'
TYPE_MODEL_DIR = os.path.join(MODEL_DIR, 'models', 'type_model')
BEHAVIOR_MODEL_DIR = os.path.join(MODEL_DIR, 'models', 'behavior_model')

BEHAVIOR_STRIDE = 10  # 슬라이딩 윈도우 이동 간격

# 어업유형 (LabelEncoder 가나다순)
FISHERY_TYPES = ['권현망', '선망', '안강망', '연승', '자망', '채낚기', '통발', '트롤']

# 어업유형별 행동 윈도우 크기 (모델 input_shape 기준)
BEHAVIOR_WINDOWS = {
    '자망': 360, '연승': 360, '통발': 360,
    '안강망': 180, '권현망': 180,
    '트롤': 60, '채낚기': 60,
    '선망': 30,
}

# 어업유형 → 행동모델 파일명
BEHAVIOR_FILES = {
    '선망': 'sunmang', '자망': 'jamang', '안강망': 'angangmang',
    '채낚기': 'chaenakgi', '권현망': 'kwonhyunmang',
    '통발': 'tongbal', '트롤': 'troll', '연승': 'yeonseung',
}

# 윈도우 크기 목록
def _get_window_sizes():
    import re
    sizes = []
    for f in os.listdir(TYPE_MODEL_DIR):
        m = re.search(r'model_(\d+)\.h5', f)
        if m:
            sizes.append(int(m.group(1)))
    return sorted(sizes, reverse=True)

WINDOW_SIZES = _get_window_sizes()
MIN_WINDOW = WINDOW_SIZES[-1] if WINDOW_SIZES else 360

# 로그 설정
log_file = os.path.join(MODEL_DIR, f'predict_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# 모델 캐시
_model_cache = {}


def _load_cached_model(path):
    if path not in _model_cache:
        _model_cache[path] = load_model(path, compile=False)
    return _model_cache[path]


# ============================================================
# 전처리 함수
# ============================================================

def interpolate_1min(df):
    """1분 단위 보간"""
    if len(df) < 2:
        return None
    if df['datetime'].iloc[-1] - df['datetime'].iloc[0] <= pd.Timedelta(minutes=1):
        return None

    df = df.copy()
    dt_range = pd.date_range(
        start=df['datetime'].iloc[0].floor('min'),
        end=df['datetime'].iloc[-1],
        freq='1min'
    )
    range_df = pd.DataFrame({'datetime': dt_range})
    df['real'] = True

    merged = pd.concat([df, range_df]).set_index('datetime').sort_index()
    merged['sin_cog'] = np.sin(np.radians(merged['cog']))
    merged['cos_cog'] = np.cos(np.radians(merged['cog']))

    num_cols = ['lat', 'lon', 'sog', 'sin_cog', 'cos_cog']
    merged[num_cols] = merged[num_cols].astype(float).interpolate(method='linear')

    merged['cog'] = np.degrees(np.arctan2(merged['sin_cog'], merged['cos_cog']))
    merged['cog'] = (merged['cog'] + 360) % 360

    merged['mmsi'] = merged['mmsi'].ffill().bfill()
    merged = merged[merged['real'] != True].drop(columns=['real', 'sin_cog', 'cos_cog'])
    merged = merged.reset_index()

    if len(merged) < 2:
        return None
    return merged


def create_features(df):
    """5개 피처 생성"""
    df = df.copy()
    from haversine import haversine_vector, Unit

    df['hour'] = df['datetime'].dt.hour
    df['delta_lat'] = df['lat'].diff().fillna(0)
    df['delta_lon'] = df['lon'].diff().fillna(0)

    # angle
    lat_prev, lon_prev = df['lat'].shift(1), df['lon'].shift(1)
    lat_next, lon_next = df['lat'].shift(-1), df['lon'].shift(-1)
    BA_x, BA_y = lat_prev - df['lat'], lon_prev - df['lon']
    BC_x, BC_y = lat_next - df['lat'], lon_next - df['lon']
    dot = BA_x * BC_x + BA_y * BC_y
    mag_BA = np.sqrt(BA_x**2 + BA_y**2)
    mag_BC = np.sqrt(BC_x**2 + BC_y**2)
    val = np.clip(dot / (mag_BA * mag_BC + 1e-10), -1.0, 1.0)
    df['angle'] = np.degrees(np.arccos(val)).fillna(0)

    # course change
    theta = np.degrees(np.arctan(df['delta_lat'] / (df['delta_lon'] + 1e-10))).fillna(0)
    conditions = [
        (df['delta_lon'] > 0) & (df['delta_lat'] > 0),
        (df['delta_lon'] > 0) & (df['delta_lat'] < 0),
        (df['delta_lon'] < 0) & (df['delta_lat'] < 0),
        (df['delta_lon'] < 0) & (df['delta_lat'] > 0),
    ]
    choices = [90 - theta, 90 + theta, 270 - theta, 270 + theta]
    Co = np.select(conditions, choices, default=0)
    Cd = np.abs(np.diff(Co, prepend=Co[0]))
    df['Cc'] = np.where(Cd >= 180, 360 - Cd, Cd)

    # speed
    df['spd'] = np.sqrt(df['delta_lat']**2 + df['delta_lon']**2) * 60
    df.loc[df.index[0], 'spd'] = 0

    # haversine
    coords = df[['lat', 'lon']].values
    shifted = np.roll(coords, -1, axis=0)
    hav = haversine_vector(coords[:-1], shifted[:-1], Unit.KILOMETERS)
    df['haversine'] = np.concatenate([[0], hav])

    return df, ['hour', 'angle', 'haversine', 'Cc', 'spd']


# ============================================================
# 모델 예측 함수
# ============================================================

def predict_type(feature_arr):
    """어업 유형 예측 → (type_code, type_name) 또는 None"""
    length = len(feature_arr)
    window_size = None
    for ws in WINDOW_SIZES:
        if length >= ws:
            window_size = ws
            break
    if window_size is None:
        return None

    data = feature_arr[-window_size:]
    data_batch = np.array([data], dtype=np.float32)

    model_path = os.path.join(TYPE_MODEL_DIR, f'model_{window_size}.h5')
    model = _load_cached_model(model_path)

    pred = model.predict(data_batch, batch_size=1, verbose=0)
    type_code = int(np.argmax(pred, axis=1)[0])
    return type_code, FISHERY_TYPES[type_code]


def predict_behavior(feature_arr, fishing_type):
    """조업 상태 예측 (타임스텝별) → numpy array (0~2)"""
    file_key = BEHAVIOR_FILES.get(fishing_type)
    if file_key is None:
        return np.zeros(len(feature_arr), dtype=int)

    model_path = os.path.join(BEHAVIOR_MODEL_DIR, f'{file_key}_model.h5')
    model = _load_cached_model(model_path)

    window_size = BEHAVIOR_WINDOWS.get(fishing_type, 360)
    stride = BEHAVIOR_STRIDE

    if len(feature_arr) < window_size:
        return np.zeros(len(feature_arr), dtype=int)

    # 슬라이딩 윈도우
    windows = []
    for i in range(0, len(feature_arr) - window_size + 1, stride):
        windows.append(feature_arr[i:i + window_size])

    if not windows:
        return np.zeros(len(feature_arr), dtype=int)

    window_batch = np.array(windows, dtype=np.float32)
    predictions = model.predict(window_batch, batch_size=512, verbose=0)

    # 확률 평균
    n_samples = predictions.shape[1]
    n_classes = predictions.shape[2]
    original_length = len(feature_arr)

    combined = np.zeros((original_length, n_classes))
    counts = np.zeros(original_length)

    for i in range(len(predictions)):
        start = i * stride
        end = min(start + n_samples, original_length)
        actual_len = end - start
        combined[start:end] += predictions[i, :actual_len, :]
        counts[start:end] += 1

    valid = counts > 0
    combined[valid] /= counts[valid, None]

    return np.argmax(combined, axis=1)


# ============================================================
# 메인 처리 함수
# ============================================================

def process_voyage(mmsi, voyage_num, conn, start_time=None, end_time=None):
    """한 항차 처리: 데이터 추출 → 예측 → DB UPDATE"""
    cur = conn.cursor()

    # 1. 항적 데이터 추출 (항구 정박 제외, start_time~end_time 범위)
    if start_time and end_time:
        cur.execute("""
            SELECT datetime, mmsi,
                   lat / 10000000.0 as lat, lon / 10000000.0 as lon,
                   sog / 10.0 as sog, cog / 10.0 as cog
            FROM kfw_ebp_trjdata
            WHERE mmsi = %s AND datetime >= %s AND datetime <= %s
              AND port_entering = false
            ORDER BY datetime
        """, (int(mmsi), start_time, end_time))
    else:
        cur.execute("""
            SELECT datetime, mmsi,
                   lat / 10000000.0 as lat, lon / 10000000.0 as lon,
                   sog / 10.0 as sog, cog / 10.0 as cog
            FROM kfw_ebp_trjdata
            WHERE mmsi = %s AND voyage_num = %s AND port_entering = false
            ORDER BY datetime
        """, (int(mmsi), int(voyage_num)))
    rows = cur.fetchall()

    if not rows:
        return 0, None, None

    df = pd.DataFrame(rows, columns=['datetime', 'mmsi', 'lat', 'lon', 'sog', 'cog'])
    df[['lat', 'lon', 'sog', 'cog']] = df[['lat', 'lon', 'sog', 'cog']].astype(float)
    df['datetime'] = pd.to_datetime(df['datetime'])

    # 2. 시작 시 속력 1노트 이상부터 (앞부분 저속 제거)
    first_idx = df[df['sog'] >= 1.0].index
    if len(first_idx) == 0:
        return 0, None, None
    df = df.loc[first_idx[0]:].reset_index(drop=True)

    if len(df) < MIN_WINDOW:
        return 0, None, None

    # 3. 정박 체크
    if df['sog'].mean() <= 0.5:
        return 0, None, None

    # 4. 1분 보간
    interp = interpolate_1min(df)
    if interp is None or len(interp) < MIN_WINDOW:
        return 0, None, None

    # 5. 피처 생성
    interp_feat, feat_cols = create_features(interp)
    feature_arr = interp_feat[feat_cols].values.astype(np.float32)

    # 6. Type 예측
    type_result = predict_type(feature_arr)
    if type_result is None:
        return 0, None, None
    type_code, type_name = type_result

    # 7. Behavior 예측
    status_arr = predict_behavior(feature_arr, type_name)

    # 8. 보간 결과를 원본 datetime에 매핑
    #    원본 데이터의 각 datetime에 가장 가까운 보간 결과 매핑
    orig_datetimes = df['datetime'].values
    interp_datetimes = interp_feat['datetime'].values

    updates = []
    for orig_dt in orig_datetimes:
        idx = np.searchsorted(interp_datetimes, orig_dt)
        idx = min(idx, len(status_arr) - 1)
        # numpy.datetime64 → python datetime 변환
        dt_python = pd.Timestamp(orig_dt).to_pydatetime()
        updates.append((dt_python, type_code, int(status_arr[idx])))

    # 9. DB UPDATE (임시 테이블 JOIN)
    if updates:
        cur.execute("""
            CREATE TEMP TABLE tmp_pred (
                dt TIMESTAMP, ftype SMALLINT, fstatus SMALLINT
            ) ON COMMIT DROP
        """)

        batch_size = 5000
        for i in range(0, len(updates), batch_size):
            batch = updates[i:i + batch_size]
            args_str = ','.join(
                cur.mogrify("(%s,%s,%s)", row).decode() for row in batch
            )
            cur.execute("INSERT INTO tmp_pred (dt, ftype, fstatus) VALUES " + args_str)

        cur.execute("""
            UPDATE kfw_ebp_trjdata t
            SET model_fishing_type = tmp.ftype, model_fishing_status = tmp.fstatus
            FROM tmp_pred tmp
            WHERE t.mmsi = %s AND t.datetime = tmp.dt
        """, (int(mmsi),))

        conn.commit()

    return len(updates), type_code, type_name


def process_ship(mmsi, conn):
    """한 선박의 전체 항차 처리 (kfw_ebp_voyage에서 항차 목록 조회)"""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT voyage_num, start_time, end_time
        FROM kfw_ebp_voyage
        WHERE mmsi = %s
        ORDER BY voyage_num
    """, (int(mmsi),))
    voyages = cur.fetchall()

    if not voyages:
        return 0, 0

    total_updated = 0
    for vnum, start_time, end_time in voyages:
        try:
            count, type_code, type_name = process_voyage(mmsi, vnum, conn, start_time, end_time)
            if count > 0:
                total_updated += count
                logging.info(f"    항차 {vnum}: {count:,}건 → 유형={type_code}({type_name})")
        except Exception as e:
            logging.error(f"    항차 {vnum} 오류: {e}")
            conn.rollback()

    return len(voyages), total_updated


def main():
    parser = argparse.ArgumentParser(description='전체 선박 항차별 조업 분류')
    parser.add_argument('--mmsi', type=int, default=None, help='특정 선박만 처리')
    parser.add_argument('--start-voyage', type=int, default=None, help='시작 항차번호')
    args = parser.parse_args()

    # GPU 확인
    gpus = tf.config.list_physical_devices('GPU')
    gpu_name = gpus[0].name if gpus else 'CPU'

    logging.info("=" * 60)
    logging.info(f"전체 선박 항차별 조업 분류 (GPU: {gpu_name})")
    logging.info(f"모델: {MODEL_DIR}")
    logging.info(f"Behavior stride: {BEHAVIOR_STRIDE}")
    logging.info(f"로그: {log_file}")
    logging.info("=" * 60)

    # MMSI 목록
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    if args.mmsi:
        mmsi_list = [str(args.mmsi)]
        logging.info(f"대상 선박: MMSI {args.mmsi}")
    else:
        cur.execute("SELECT DISTINCT mmsi FROM kfw_ebp_shipinfo ORDER BY mmsi")
        mmsi_list = [row[0] for row in cur.fetchall()]
        logging.info(f"대상 선박: {len(mmsi_list)}척")

    logging.info("")

    total_ships = len(mmsi_list)
    total_voyages = 0
    total_updated = 0
    error_count = 0
    start_time = time.time()

    for idx, mmsi in enumerate(mmsi_list):
        try:
            ship_start = time.time()
            v_count, u_count = process_ship(mmsi, conn)

            if v_count > 0:
                total_voyages += v_count
                total_updated += u_count
                elapsed = time.time() - ship_start
                logging.info(f"  [{idx+1}/{total_ships}] MMSI {mmsi}: "
                             f"{v_count}항차, {u_count:,}건 | {elapsed:.1f}초 | "
                             f"누적: {total_updated:,}건")
            else:
                logging.info(f"  [{idx+1}/{total_ships}] MMSI {mmsi}: 항차 없음")

        except Exception as e:
            logging.error(f"  [{idx+1}/{total_ships}] MMSI {mmsi} 오류: {e}")
            error_count += 1
            try:
                conn.close()
            except:
                pass
            conn = psycopg2.connect(**DB_CONFIG)

    conn.close()

    total_elapsed = time.time() - start_time
    logging.info("")
    logging.info("=" * 60)
    logging.info(f"완료! {total_elapsed:.0f}초 소요")
    logging.info(f"  선박: {total_ships}척 | 항차: {total_voyages}개 | "
                 f"UPDATE: {total_updated:,}건 | 오류: {error_count}건")
    logging.info("=" * 60)


if __name__ == '__main__':
    main()
