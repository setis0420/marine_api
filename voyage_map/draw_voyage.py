"""
항차별 항적 지도 생성 모듈
- DB에서 특정 선박의 특정 항차 데이터를 조회
- KFW/EBP 구역, Cable Route, 해구도, 한일 EEZ 표시
- 날짜별 항적 레이어 + 속도 색상 구분
- 단독 실행 시 인자로 mmsi, voyage_num 지정

사용법:
    python draw_voyage.py 440137010 22001
    python draw_voyage.py 440137010 22001 --output result/test.html
"""

import os
import sys
import json
import argparse
import psycopg2
import numpy as np
import pandas as pd
import folium

# === 설정 ===
DB_CONFIG = {
    'host': '203.253.202.159',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420!',
    'port': '5432'
}

# 데이터 파일 경로 (이 파일 기준 상대경로)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, 'result')


def load_map_data():
    """KFW/EBP/Cable/해구도/EEZ 데이터 로드"""
    data = {}

    data['KFW'] = pd.read_excel(os.path.join(BASE_DIR, 'KFW.xlsx'))
    data['EBP'] = pd.read_excel(os.path.join(BASE_DIR, 'EBP.xlsx'))
    data['cable'] = pd.read_excel(os.path.join(BASE_DIR, 'cable route.xlsx'))

    sohaegu = pd.read_excel(os.path.join(BASE_DIR, '소해구 좌표.xlsx'))
    sohaegu['haegu_num'] = sohaegu['대해구 번호'].astype(str) + '-' + sohaegu['소해구 번호'].astype(str)
    sohaegu.rename(columns={'경도': 'lon', '위도': 'lat'}, inplace=True)
    data['sohaegu'] = sohaegu[['haegu_num', 'lon', 'lat']].copy()

    with open(os.path.join(BASE_DIR, 'korea_eez.geojson')) as f:
        data['korea_eez'] = json.load(f)
    with open(os.path.join(BASE_DIR, 'japan_eez.geojson')) as f:
        data['japan_eez'] = json.load(f)

    return data


def get_voyage_data(mmsi, voyage_num):
    """DB에서 특정 항차 데이터 조회"""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    ship_table = f"ship_{mmsi}"
    cur.execute(f"""
        SELECT datetime, lon / 10000000.0 as lon, lat / 10000000.0 as lat,
               sog / 10.0 as sog, port_entering
        FROM {ship_table}
        WHERE voyage_num = %s
        ORDER BY datetime
    """, (int(voyage_num),))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return None

    trj = pd.DataFrame(rows, columns=['datetime', 'lon', 'lat', 'sog', 'port_entering'])
    trj['datetime'] = pd.to_datetime(trj['datetime'])
    trj['date_str'] = trj['datetime'].dt.strftime('%Y-%m-%d')
    return trj


def get_vessel_name(mmsi):
    """선박명 조회"""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SELECT vessel_name FROM kfw_ebp_shipinfo WHERE mmsi = %s LIMIT 1", (str(mmsi),))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else str(mmsi)


def build_haegu_layer(df, lat_range=(34.5, 36.5), lon_range=(129, 131)):
    """해구도 레이어 생성"""
    df_region = df[(df['lat'] >= lat_range[0]) & (df['lat'] <= lat_range[1]) &
                   (df['lon'] >= lon_range[0]) & (df['lon'] <= lon_range[1])]

    unique_lats = sorted(df_region['lat'].unique())
    unique_lons = sorted(df_region['lon'].unique())

    lat_bounds = {}
    for i, lat in enumerate(unique_lats):
        lower = lat - (unique_lats[min(i+1, len(unique_lats)-1)] - lat) / 2 if i == 0 else (unique_lats[i-1] + lat) / 2
        upper = lat + (lat - unique_lats[max(i-1, 0)]) / 2 if i == len(unique_lats)-1 else (lat + unique_lats[i+1]) / 2
        lat_bounds[lat] = (lower, upper)

    lon_bounds = {}
    for j, lon in enumerate(unique_lons):
        left = lon - (unique_lons[min(j+1, len(unique_lons)-1)] - lon) / 2 if j == 0 else (unique_lons[j-1] + lon) / 2
        right = lon + (lon - unique_lons[max(j-1, 0)]) / 2 if j == len(unique_lons)-1 else (lon + unique_lons[j+1]) / 2
        lon_bounds[lon] = (left, right)

    haegu = folium.FeatureGroup(name='해구도', show=False)
    for _, row in df_region.iterrows():
        lat, lon = row['lat'], row['lon']
        la_min, la_max = lat_bounds[lat]
        lo_min, lo_max = lon_bounds[lon]
        coords = [[la_min, lo_min], [la_min, lo_max], [la_max, lo_max], [la_max, lo_min]]
        haegu.add_child(folium.Polygon(locations=coords, color='black', weight=1, opacity=0.5, fill=False))
        haegu.add_child(folium.Marker(location=[lat, lon], icon=folium.DivIcon(
            html=f'<div style="white-space:nowrap;opacity:0.5;font-size:8pt;color:black;text-align:center;">{row["haegu_num"]}</div>')))
    return haegu


def draw_voyage(mmsi, voyage_num, output_path=None):
    """항차 지도 생성 메인 함수"""
    # 데이터 로드
    map_data = load_map_data()
    trj = get_voyage_data(mmsi, voyage_num)

    if trj is None or len(trj) == 0:
        print(f"데이터 없음: MMSI {mmsi}, 항차 {voyage_num}")
        return None

    vessel_name = get_vessel_name(mmsi)
    start_dt = trj['datetime'].iloc[0].strftime('%Y-%m-%d %H:%M')
    end_dt = trj['datetime'].iloc[-1].strftime('%Y-%m-%d %H:%M')

    print(f"{vessel_name} (MMSI:{mmsi}) 항차 {voyage_num}")
    print(f"  기간: {start_dt} ~ {end_dt}")
    print(f"  레코드: {len(trj):,}건")

    # 지도 생성
    map_center = [map_data['KFW']['lat'].mean(), map_data['KFW']['lon'].mean()]
    m = folium.Map(location=map_center, zoom_start=9)

    # 한국/일본 EEZ
    fg_kr = folium.FeatureGroup(name='한국 EEZ', show=True)
    folium.GeoJson(map_data['korea_eez'], style_function=lambda x: {
        'color': '#ff4444', 'weight': 2, 'fillOpacity': 0.03, 'dashArray': '5,5'
    }).add_to(fg_kr)
    fg_kr.add_to(m)

    fg_jp = folium.FeatureGroup(name='일본 EEZ', show=True)
    folium.GeoJson(map_data['japan_eez'], style_function=lambda x: {
        'color': '#4444ff', 'weight': 2, 'fillOpacity': 0.03, 'dashArray': '5,5'
    }).add_to(fg_jp)
    fg_jp.add_to(m)

    # KFW / EBP / Cable Route
    kfw_list = map_data['KFW'][['lat', 'lon']].values.tolist()
    folium.Polygon(locations=kfw_list, color='blue', weight=3, fill=True,
                   fill_color='blue', fill_opacity=0.2).add_to(m)

    ebp_list = map_data['EBP'][['lat', 'lon']].values.tolist()
    folium.Polygon(locations=ebp_list, color='green', weight=3, fill=True,
                   fill_color='green', fill_opacity=0.2).add_to(m)

    cable_list = map_data['cable'][['lat', 'lon']].values.tolist()
    folium.PolyLine(locations=cable_list, color='red', weight=3, fill=False,
                    fill_opacity=0.2).add_to(m)

    # 해구도
    haegu = build_haegu_layer(map_data['sohaegu'])
    m.add_child(haegu)

    # 1분 단위 보간
    times = trj['datetime'].values
    lats = trj['lat'].astype(float).values
    lons = trj['lon'].astype(float).values
    sogs = trj['sog'].astype(float).values

    t0 = times[0]
    t_sec = np.array([(t - t0) / np.timedelta64(1, 's') for t in times])

    total_sec = t_sec[-1]
    if total_sec > 0:
        t_interp = np.arange(0, total_sec, 60)  # 1분 간격
        lat_interp = np.interp(t_interp, t_sec, lats)
        lon_interp = np.interp(t_interp, t_sec, lons)

        # 각 보간점의 수신 간격 (해당 구간의 원본 점 사이 시간)
        gap_sec = np.zeros(len(t_interp))
        for i, t in enumerate(t_interp):
            idx = np.searchsorted(t_sec, t, side='right')
            if idx == 0:
                gap_sec[i] = 0
            elif idx >= len(t_sec):
                gap_sec[i] = t - t_sec[-1]
            else:
                gap_sec[i] = t_sec[idx] - t_sec[idx - 1]

        # 속력 계산: 보간점 사이 거리/시간 (knots)
        sog_interp = np.zeros(len(t_interp))
        for i in range(1, len(t_interp)):
            dlat = lat_interp[i] - lat_interp[i - 1]
            dlon = lon_interp[i] - lon_interp[i - 1]
            # 간이 거리 계산 (해리)
            lat_mid = (lat_interp[i] + lat_interp[i - 1]) / 2.0
            dist_nm = np.sqrt((dlat * 60) ** 2 + (dlon * 60 * np.cos(np.radians(lat_mid))) ** 2)
            dt_hours = (t_interp[i] - t_interp[i - 1]) / 3600.0
            if dt_hours > 0:
                sog_interp[i] = dist_nm / dt_hours
        sog_interp[0] = sog_interp[1] if len(sog_interp) > 1 else 0
    else:
        lat_interp = lats
        lon_interp = lons
        sog_interp = sogs
        gap_sec = np.zeros(len(lats))

    # 항적 라인: 수신 간격 3분 이상 구간은 연하게
    fg_line = folium.FeatureGroup(name=f'항차 {voyage_num} 항적', show=True)

    seg_start = 0
    for i in range(1, len(lat_interp)):
        is_gap = gap_sec[i] >= 180  # 3분 이상
        prev_gap = gap_sec[i - 1] >= 180 if i > 0 else False

        if is_gap != prev_gap or i == len(lat_interp) - 1:
            end_i = i + 1 if i == len(lat_interp) - 1 else i + 1
            seg_coords = list(zip(lat_interp[seg_start:end_i], lon_interp[seg_start:end_i]))
            if len(seg_coords) >= 2:
                opacity = 0.2 if prev_gap else 0.6
                weight = 1 if prev_gap else 2
                folium.PolyLine(locations=seg_coords, color='black',
                                weight=weight, opacity=opacity).add_to(fg_line)
            seg_start = i

    m.add_child(fg_line)

    # 속도 점: 수신 간격 3분 이상 구간은 적게/연하게
    fg_pts = folium.FeatureGroup(name=f'항차 {voyage_num} 속도점', show=True)
    for i in range(len(lat_interp)):
        is_gap = gap_sec[i] >= 180
        # 3분 이상 갭 구간은 10개당 1개, 정상은 2개당 1개
        if is_gap and i % 10 != 0:
            continue
        if not is_gap and i % 2 != 0:
            continue

        point_color = 'red' if sog_interp[i] < 3 else 'blue'
        fill_opacity = 0.3 if is_gap else 0.8
        radius = 2 if is_gap else 3

        folium.CircleMarker(
            location=[lat_interp[i], lon_interp[i]], radius=radius,
            color=point_color, fill=True, fill_color=point_color,
            fill_opacity=fill_opacity
        ).add_to(fg_pts)
    m.add_child(fg_pts)

    folium.LayerControl().add_to(m)

    # 저장
    if output_path is None:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        output_path = os.path.join(OUTPUT_DIR, f'{mmsi}_{voyage_num}.html')

    # 출력 폴더 생성
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    m.save(output_path)
    print(f"  저장: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description='항차별 항적 지도 생성')
    parser.add_argument('mmsi', type=int, help='선박 MMSI')
    parser.add_argument('voyage_num', type=int, help='항차번호 (예: 22001)')
    parser.add_argument('--output', '-o', type=str, default=None, help='출력 파일 경로')
    args = parser.parse_args()

    path = draw_voyage(args.mmsi, args.voyage_num, args.output)
    if path:
        os.startfile(path)


if __name__ == '__main__':
    main()
