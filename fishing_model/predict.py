"""
어선 조업 분류 딥러닝 모델 추론 모듈
- Stage 1: 어업 유형 예측 (선망, 자망, 안강망, 연승, 통발, 채낚기, 트롤, 권현망)
- Stage 2: 조업 상태 예측 (조업/비조업)

사용법:
    from predict import predict_fishing
    result = predict_fishing(df)  # df: datetime, mmsi, lat, lon, sog, cog 컬럼 필요

    # 또는 DB에서 직접
    result = predict_from_db(mmsi=440137010, start='2022-01-01', end='2022-02-01')
"""

import os
import re
import numpy as np
import pandas as pd
from datetime import timedelta
from sklearn.preprocessing import LabelEncoder
from haversine import haversine_vector, Unit

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # TF 로그 억제
import tensorflow as tf
tf.get_logger().setLevel('ERROR')
from keras.models import load_model

# === 경로 설정 (한글 경로 문제로 영문 경로 고정) ===
BASE_DIR = r'C:\fishing_model'
TYPE_MODEL_DIR = os.path.join(BASE_DIR, 'models', 'type_model')
BEHAVIOR_MODEL_DIR = os.path.join(BASE_DIR, 'models', 'behavior_model')

# === 모델 윈도우 크기 ===
def _get_window_sizes():
    sizes = []
    for f in os.listdir(TYPE_MODEL_DIR):
        m = re.search(r'model_(\d+)\.h5', f)
        if m:
            sizes.append(int(m.group(1)))
    return sorted(sizes, reverse=True)

WINDOW_SIZES = _get_window_sizes()
MIN_WINDOW = WINDOW_SIZES[-1] if WINDOW_SIZES else 360

# 어업유형별 행동 윈도우 크기 (모델 input_shape과 일치)
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

# 어업유형 라벨
FISHERY_TYPES = ['선망', '안강망', '연승', '자망', '채낚기', '통발', '트롤', '권현망']

# 모델 캐시
_model_cache = {}


# ============================================================
# 데이터 전처리 함수들
# ============================================================

def interpolate_1min(df):
    """1분 단위 보간 (cog는 sin/cos 변환 후 보간)"""
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
    """5개 피처 생성: hour, angle, haversine, Cc, spd"""
    df = df.copy()

    # hour
    df['hour'] = df['datetime'].dt.hour

    # delta lat/lon
    df['delta_lat'] = df['lat'].diff().fillna(0)
    df['delta_lon'] = df['lon'].diff().fillna(0)

    # angle (3점 사이 각도)
    lat_prev = df['lat'].shift(1)
    lon_prev = df['lon'].shift(1)
    lat_next = df['lat'].shift(-1)
    lon_next = df['lon'].shift(-1)

    BA_x = lat_prev - df['lat']
    BA_y = lon_prev - df['lon']
    BC_x = lat_next - df['lat']
    BC_y = lon_next - df['lon']

    dot = BA_x * BC_x + BA_y * BC_y
    mag_BA = np.sqrt(BA_x**2 + BA_y**2)
    mag_BC = np.sqrt(BC_x**2 + BC_y**2)

    val = np.clip(dot / (mag_BA * mag_BC + 1e-10), -1.0, 1.0)
    df['angle'] = np.degrees(np.arccos(val)).fillna(0)

    # course & course change
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

    features = ['hour', 'angle', 'haversine', 'Cc', 'spd']
    return df, features


def split_by_gap(df, gap_minutes=15):
    """시간 간격이 gap_minutes 이상이면 분리"""
    df = df.sort_values('datetime').reset_index(drop=True)
    time_diff = df['datetime'].diff().dt.total_seconds() / 60
    splits = []
    start = 0
    for i, td in enumerate(time_diff):
        if pd.notna(td) and td >= gap_minutes:
            splits.append(df.iloc[start:i].reset_index(drop=True))
            start = i
    splits.append(df.iloc[start:].reset_index(drop=True))
    return [s for s in splits if len(s) >= 2]


# ============================================================
# 모델 추론 함수들
# ============================================================

def _load_cached_model(path):
    if path not in _model_cache:
        _model_cache[path] = load_model(path, compile=False)
    return _model_cache[path]


def predict_type_single(feature_arr):
    """단일 시퀀스의 어업 유형 예측.
    Returns: (type_code, type_name) 또는 None
        type_code: 0~7 (LabelEncoder 가나다순 인덱스)
        type_name: 한글 어업유형명
    """
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
    type_name = FISHERY_TYPES[type_code]

    return type_code, type_name


def predict_behavior_single(feature_arr, fishing_type):
    """단일 시퀀스의 조업 상태 예측 (타임스텝별)"""
    file_key = BEHAVIOR_FILES.get(fishing_type)
    if file_key is None:
        return np.zeros(len(feature_arr), dtype=int)

    model_path = os.path.join(BEHAVIOR_MODEL_DIR, f'{file_key}_model.h5')
    model = _load_cached_model(model_path)

    window_size = BEHAVIOR_WINDOWS.get(fishing_type, 360)
    stride = 3

    if len(feature_arr) < window_size:
        return np.zeros(len(feature_arr), dtype=int)

    # 슬라이딩 윈도우 생성
    windows = []
    for i in range(0, len(feature_arr) - window_size + 1, stride):
        windows.append(feature_arr[i:i + window_size])

    if not windows:
        return np.zeros(len(feature_arr), dtype=int)

    window_batch = np.array(windows, dtype=np.float32)
    predictions = model.predict(window_batch, batch_size=512, verbose=0)

    # 겹치는 예측 평균
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

    final = np.argmax(combined, axis=1)

    # 그대로 반환: 0=비조업, 1=투망(또는 조업), 2=양망
    # 채낚기/안강망은 2클래스(0=비조업, 1=조업)
    return final


# ============================================================
# 메인 추론 함수
# ============================================================

def predict_fishing(df):
    """
    어업 유형 + 조업 상태 예측

    Args:
        df: DataFrame with columns [datetime, mmsi, lat, lon, sog, cog]

    Returns:
        DataFrame with added columns [fishing_type_pred, fishing_status]
        - fishing_type_pred: 어업 유형 (선망, 자망 등)
        - fishing_status: 0=비조업, 1=조업
    """
    df = df.copy()
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    df['model_fishing_type'] = None   # 0~7 코드
    df['model_fishing_status'] = 0    # 0=비조업, 1=투망, 2=양망

    # 시간 간격으로 분리
    segments = split_by_gap(df, gap_minutes=15)

    results = []

    for seg in segments:
        if len(seg) < MIN_WINDOW:
            seg['model_fishing_type'] = None
            seg['model_fishing_status'] = 0
            results.append(seg)
            continue

        # 1분 보간
        interp = interpolate_1min(seg)
        if interp is None or len(interp) < MIN_WINDOW:
            seg['model_fishing_type'] = None
            seg['model_fishing_status'] = 0
            results.append(seg)
            continue

        # 정박 체크
        if interp['sog'].mean() <= 0.5:
            interp['model_fishing_type'] = None
            interp['model_fishing_status'] = 0
            results.append(interp)
            continue

        # 피처 생성
        interp_feat, feat_cols = create_features(interp)
        feature_arr = interp_feat[feat_cols].values.astype(np.float32)

        # Stage 1: 어업 유형 예측
        type_result = predict_type_single(feature_arr)
        if type_result:
            type_code, type_name = type_result
            interp_feat['model_fishing_type'] = type_code

            # Stage 2: 조업 상태 예측
            status = predict_behavior_single(feature_arr, type_name)
            interp_feat['model_fishing_status'] = status
        else:
            interp_feat['model_fishing_type'] = None
            interp_feat['model_fishing_status'] = 0

        results.append(interp_feat[['datetime', 'mmsi', 'lat', 'lon', 'sog', 'cog',
                                     'model_fishing_type', 'model_fishing_status']])

    if results:
        return pd.concat(results, ignore_index=True)
    return df


def predict_from_db(mmsi, start, end, db_config=None):
    """
    DB에서 항적 데이터를 가져와서 예측

    Args:
        mmsi: 선박 MMSI
        start: 시작일 (str)
        end: 종료일 (str)
        db_config: DB 접속 정보 dict (기본: 159 서버)

    Returns:
        DataFrame with predictions
    """
    import psycopg2

    if db_config is None:
        db_config = {
            'host': '203.253.202.159',
            'dbname': 'marine',
            'user': 'postgres',
            'password': 'prhkddlf0420!',
            'port': '5432'
        }

    conn = psycopg2.connect(**db_config)
    cur = conn.cursor()
    ship_table = f"ship_{mmsi}"
    cur.execute(f"""
        SELECT datetime, mmsi, lat / 10000000.0 as lat, lon / 10000000.0 as lon,
               sog / 10.0 as sog, cog / 10.0 as cog
        FROM {ship_table}
        WHERE datetime >= %s AND datetime < %s
        ORDER BY datetime
    """, (start, end))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        print(f"데이터 없음: MMSI {mmsi}")
        return None

    df = pd.DataFrame(rows, columns=['datetime', 'mmsi', 'lat', 'lon', 'sog', 'cog'])
    df[['lat', 'lon', 'sog', 'cog']] = df[['lat', 'lon', 'sog', 'cog']].astype(float)
    print(f"MMSI {mmsi}: {len(df):,}건 로드, 예측 시작...")

    result = predict_fishing(df)
    print(f"  예측 완료: {len(result):,}건")

    return result


# ============================================================
# 단독 실행
# ============================================================
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='어선 조업 분류 예측')
    parser.add_argument('mmsi', type=int, help='선박 MMSI')
    parser.add_argument('--start', default='2022-01-01', help='시작일')
    parser.add_argument('--end', default='2022-02-01', help='종료일')
    args = parser.parse_args()

    # 코드 → 한글 매핑
    TYPE_NAMES = {i: n for i, n in enumerate(FISHERY_TYPES)}
    STATUS_NAMES = {0: '비조업', 1: '투망', 2: '양망'}

    result = predict_from_db(args.mmsi, args.start, args.end)
    if result is not None:
        type_code = result['model_fishing_type'].mode().iloc[0]
        type_name = TYPE_NAMES.get(type_code, '알수없음') if type_code is not None else '없음'
        print(f"\n어업 유형: {type_code} ({type_name})")

        total = len(result)
        for code, name in STATUS_NAMES.items():
            cnt = (result['model_fishing_status'] == code).sum()
            print(f"  {code}={name}: {cnt:,}건 ({cnt/total*100:.1f}%)")
