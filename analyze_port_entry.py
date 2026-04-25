import psycopg2
import folium

# 1. 로컬 marine DB에서 어선 MMSI 목록 가져오기
local_conn = psycopg2.connect(
    host='localhost', dbname='marine', user='postgres',
    password='prhkddlf0420!', port='5432'
)
cur = local_conn.cursor()
cur.execute("SELECT mmsi FROM fishing_shipinfo WHERE mmsi IS NOT NULL")
fishing_mmsi = [row[0] for row in cur.fetchall()]
cur.close()
local_conn.close()
print(f"어선 MMSI 수: {len(fishing_mmsi)}")

# 2. 21번 서버에서 어선별로 조회 (sog <= 1, 2023-05-01 ~ 05-07)
remote_conn = psycopg2.connect(
    host='203.253.202.21', dbname='aisdb', user='fishery_readonly_2',
    password='readonly', port='5432'
)
cur = remote_conn.cursor()

all_rows = []
batch_size = 200
total = len(fishing_mmsi)

for i in range(0, total, batch_size):
    batch = fishing_mmsi[i:i+batch_size]
    placeholders = ','.join(['%s'] * len(batch))
    query = f"""
        SELECT mmsi,
               lon / 6000000.0 as lon,
               lat / 6000000.0 as lat,
               sog,
               datetime
        FROM db202305
        WHERE mmsi IN ({placeholders})
          AND datetime >= '2023-05-01' AND datetime < '2023-05-08'
          AND sog <= 1
          AND lon <> 0 AND lat <> 0
    """
    cur.execute(query, batch)
    rows = cur.fetchall()
    all_rows.extend(rows)
    done = min(i + batch_size, total)
    print(f"  진행: {done}/{total} ({done*100//total}%) - 누적 {len(all_rows)}건", flush=True)

cur.close()
remote_conn.close()
print(f"\n어선 정박 데이터 총: {len(all_rows)}건")

# 3. 위치 그룹화 (소수점 3자리 = ~111m 그리드)
grid = {}

for mmsi, lon, lat, sog, dt in all_rows:
    grid_key = (round(lat, 3), round(lon, 3))
    if grid_key not in grid:
        grid[grid_key] = {'count': 0, 'mmsi_set': set(), 'lat_sum': 0, 'lon_sum': 0}
    grid[grid_key]['count'] += 1
    grid[grid_key]['mmsi_set'].add(mmsi)
    grid[grid_key]['lat_sum'] += lat
    grid[grid_key]['lon_sum'] += lon

print(f"그리드 그룹 수: {len(grid)}")

# 4. 3척 이상 모인 곳만
top_groups = sorted(grid.items(), key=lambda x: len(x[1]['mmsi_set']), reverse=True)
significant = [(k, v) for k, v in top_groups if len(v['mmsi_set']) >= 3]
print(f"3척 이상 모인 지점: {len(significant)}개")

# 5. 지도 생성
m = folium.Map(location=[35.5, 128.0], zoom_start=7, tiles='CartoDB positron')

for (grid_lat, grid_lon), info in significant:
    avg_lat = info['lat_sum'] / info['count']
    avg_lon = info['lon_sum'] / info['count']
    ship_count = len(info['mmsi_set'])
    record_count = info['count']

    radius = min(3 + ship_count * 0.5, 30)

    if ship_count >= 50:
        color = '#e6194b'
    elif ship_count >= 20:
        color = '#f58231'
    elif ship_count >= 10:
        color = '#ffe119'
    else:
        color = '#3cb44b'

    folium.CircleMarker(
        location=[avg_lat, avg_lon],
        radius=radius,
        color=color,
        fill=True,
        fill_color=color,
        fill_opacity=0.6,
        weight=1,
        popup=f"<b>선박 {ship_count}척</b><br>"
              f"레코드: {record_count}건<br>"
              f"위치: {avg_lat:.4f}, {avg_lon:.4f}",
        tooltip=f"{ship_count}척 / {record_count}건",
    ).add_to(m)

legend_html = """
<div style="position:fixed; bottom:30px; left:30px; z-index:1000;
     background:white; padding:10px; border-radius:5px; border:1px solid #ccc;
     font-size:12px;">
<b>어선 정박 위치 (2023.05.01~07)</b><br>
<i style="background:#e6194b;width:12px;height:12px;display:inline-block;border-radius:50%;"></i> 50척 이상<br>
<i style="background:#f58231;width:12px;height:12px;display:inline-block;border-radius:50%;"></i> 20~49척<br>
<i style="background:#ffe119;width:12px;height:12px;display:inline-block;border-radius:50%;"></i> 10~19척<br>
<i style="background:#3cb44b;width:12px;height:12px;display:inline-block;border-radius:50%;"></i> 3~9척<br>
</div>
"""
m.get_root().html.add_child(folium.Element(legend_html))

output_path = r"k:\coding_project\해양수산 데이터 분석 플랫폼\port_entry_analysis.html"
m.save(output_path)
print(f"\n지도 저장 완료: {output_path}")

# 6. 상위 20개 지점 출력
print("\n=== 상위 20개 정박 지점 ===")
print(f"{'순위':>4} {'위도':>10} {'경도':>12} {'선박수':>6} {'레코드':>8}")
print("-" * 50)
for i, ((grid_lat, grid_lon), info) in enumerate(significant[:20], 1):
    avg_lat = info['lat_sum'] / info['count']
    avg_lon = info['lon_sum'] / info['count']
    print(f"{i:>4} {avg_lat:>10.4f} {avg_lon:>12.4f} {len(info['mmsi_set']):>6} {info['count']:>8}")
