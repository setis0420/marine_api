import psycopg2
import folium

conn = psycopg2.connect(
    host='localhost',
    dbname='marine',
    user='postgres',
    password='prhkddlf0420!',
    port='5432'
)

cur = conn.cursor()
cur.execute("SELECT johab_code, johab_name, lat_start, lat_end, lon_start, lon_end FROM fish_suhyub_location")
rows = cur.fetchall()
cur.close()
conn.close()

m = folium.Map(location=[35.5, 128.0], zoom_start=7, tiles='CartoDB positron')

colors = [
    '#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
    '#42d4f4', '#f032e6', '#bfef45', '#fabed4', '#469990',
    '#dcbeff', '#9A6324', '#800000', '#aaffc3', '#808000',
    '#ffd8b1', '#000075', '#a9a9a9', '#000000', '#ffe119',
]

code_color = {}
color_idx = 0

valid_count = 0
null_count = 0
null_names = []

for row in rows:
    johab_code, johab_name, lat_start, lat_end, lon_start, lon_end = row

    if lat_start is None or lat_end is None or lon_start is None or lon_end is None:
        null_count += 1
        null_names.append(f"{johab_name} (code: {johab_code})")
        continue

    valid_count += 1

    if johab_code not in code_color:
        code_color[johab_code] = colors[color_idx % len(colors)]
        color_idx += 1

    color = code_color[johab_code]

    bounds = [[lat_start, lon_start], [lat_end, lon_end]]
    folium.Rectangle(
        bounds=bounds,
        color=color,
        fill=True,
        fill_color=color,
        fill_opacity=0.3,
        weight=2,
        popup=f"<b>{johab_name}</b><br>코드: {johab_code}<br>"
              f"위도: {lat_start:.4f}~{lat_end:.4f}<br>"
              f"경도: {lon_start:.4f}~{lon_end:.4f}",
        tooltip=johab_name,
    ).add_to(m)

    folium.Marker(
        location=[(lat_start + lat_end) / 2, (lon_start + lon_end) / 2],
        icon=folium.DivIcon(
            html=f'<div style="font-size:8px; white-space:nowrap; color:{color}; font-weight:bold;">{johab_name}</div>',
            icon_size=(150, 20),
            icon_anchor=(75, 10),
        ),
    ).add_to(m)

# Print summary
print(f"총 레코드: {len(rows)}")
print(f"유효 구역: {valid_count}")
print(f"좌표 없음: {null_count}")
if null_names:
    print("\n좌표가 없는 수협:")
    for name in null_names:
        print(f"  - {name}")

output_path = r"k:\coding_project\해양수산 데이터 분석 플랫폼\suhyub_location_map.html"
m.save(output_path)
print(f"\n지도 저장 완료: {output_path}")
