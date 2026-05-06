"""
대형트롤 어선 항적을 folium HTML로 저장
- 길이가 6시간 이상인 항차 우선 선택
- ship별 1개 항차씩 / 또는 다수 항차
"""
import psycopg2
import folium
import os, sys
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

LOCAL_DB = {'host': '203.253.202.171', 'dbname': 'marine',
            'user': 'postgres', 'password': 'prhkddlf0420', 'port': 5432}

OUT_DIR = r'k:\coding_project\해양수산 데이터 분석 플랫폼\html'
os.makedirs(OUT_DIR, exist_ok=True)

NUM_SHIPS = 8       # 항적 그릴 선박 수
VOYAGES_PER_SHIP = 2 # 척당 항차 수


def get_target_voyages():
    """대형트롤 척별로 충분히 긴 항차 선택"""
    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT v.mmsi, v.voyage_num, v.start_time, v.end_time, v.duration,
               s.shipname_kr, s.shipname
        FROM fishing_voyage v
        JOIN shipinfo s ON s.mmsi = v.mmsi
        WHERE s.fishing_type='대형트롤어업'
          AND v.target_area='total'
          AND v.duration > 360       -- 6시간 이상
        ORDER BY v.mmsi, v.duration DESC
    """)
    all_voys = cur.fetchall()
    cur.close(); conn.close()

    # 척별로 상위 N개씩 선별
    by_ship = {}
    for mmsi, vnum, st, et, dur, sk, sn in all_voys:
        by_ship.setdefault(mmsi, []).append((mmsi, vnum, st, et, dur, sk or sn or str(mmsi)))

    selected = []
    for mmsi, voys in list(by_ship.items())[:NUM_SHIPS]:
        selected.extend(voys[:VOYAGES_PER_SHIP])
    return selected


def draw_voyage(mmsi, vnum, start_time, end_time, ship_name):
    """한 항차 항적을 folium으로 그리고 HTML 저장"""
    conn = psycopg2.connect(**LOCAL_DB)
    cur = conn.cursor()

    # 항적 데이터 (1분 샘플링)
    cur.execute(f"""
        SELECT datetime, lat/10000000.0, lon/10000000.0, sog/10.0
        FROM ship_{mmsi}
        WHERE datetime >= %s AND datetime <= %s
          AND lat BETWEEN -900000000 AND 900000000
          AND lon BETWEEN -1800000000 AND 1800000000
        ORDER BY datetime
    """, (start_time, end_time))
    rows = cur.fetchall()
    cur.close(); conn.close()

    if len(rows) < 5:
        print(f'  {mmsi} v{vnum}: 데이터 부족 ({len(rows)}건) — 스킵')
        return None

    pts = [(float(r[1]), float(r[2]), float(r[3] or 0)) for r in rows
           if r[1] is not None and r[2] is not None
           and -90 <= float(r[1]) <= 90 and -180 <= float(r[2]) <= 180]
    if len(pts) < 5: return None

    # 다운샘플링 (너무 많으면)
    if len(pts) > 5000:
        step = len(pts) // 5000 + 1
        pts = pts[::step]

    avg_lat = sum(p[0] for p in pts) / len(pts)
    avg_lon = sum(p[1] for p in pts) / len(pts)

    m = folium.Map(location=[avg_lat, avg_lon], zoom_start=8, tiles='CartoDB positron')

    # 항적선
    folium.PolyLine([(p[0], p[1]) for p in pts], color='#3498db', weight=2, opacity=0.7).add_to(m)

    # 속도별 마커 (샘플링)
    sample_every = max(1, len(pts) // 200)
    for i, (lat, lon, sog) in enumerate(pts[::sample_every]):
        if sog < 0.5:
            color = '#7f8c8d'  # 정박/대기
        elif sog < 5:
            color = '#27ae60'  # 저속(조업 가능성)
        elif sog < 12:
            color = '#3498db'  # 항해
        else:
            color = '#e74c3c'  # 고속
        folium.CircleMarker(
            location=[lat, lon], radius=3,
            color=color, fill=True, fill_color=color, fill_opacity=0.8,
            popup=f'{rows[i*sample_every][0]}<br>SOG: {sog:.1f}kn',
        ).add_to(m)

    # 시작/종료 마커
    folium.Marker([pts[0][0], pts[0][1]], popup=f'시작 {start_time}',
                  icon=folium.Icon(color='green', icon='play')).add_to(m)
    folium.Marker([pts[-1][0], pts[-1][1]], popup=f'종료 {end_time}',
                  icon=folium.Icon(color='red', icon='stop')).add_to(m)

    # 정보 박스
    duration_h = (end_time - start_time).total_seconds() / 3600
    info_html = f"""
    <div style="position:fixed;top:10px;right:10px;background:white;padding:10px 14px;border:1px solid #ccc;border-radius:6px;font-family:'Noto Sans KR',sans-serif;font-size:13px;z-index:9999;">
        <b>대형트롤어업</b><br>
        MMSI: {mmsi}<br>
        선명: {ship_name}<br>
        항차: {vnum}<br>
        기간: {duration_h:.1f}시간<br>
        시작: {str(start_time)[:19]}<br>
        종료: {str(end_time)[:19]}<br>
        포인트: {len(pts):,}개<br>
        <hr style="margin:5px 0;">
        <span style="color:#7f8c8d;">●</span> 정박 (&lt;0.5kn)<br>
        <span style="color:#27ae60;">●</span> 저속 (0.5~5kn)<br>
        <span style="color:#3498db;">●</span> 항해 (5~12kn)<br>
        <span style="color:#e74c3c;">●</span> 고속 (&gt;12kn)
    </div>
    """
    m.get_root().html.add_child(folium.Element(info_html))

    fname = f'trawl_{mmsi}_v{vnum}_{str(start_time)[:10]}.html'
    out_path = os.path.join(OUT_DIR, fname)
    m.save(out_path)
    return out_path, len(pts), duration_h


def main():
    print(f'대상 폴더: {OUT_DIR}')
    print(f'대상: 대형트롤 어선 {NUM_SHIPS}척 × 항차 {VOYAGES_PER_SHIP}개\n')

    voyages = get_target_voyages()
    print(f'선택된 항차: {len(voyages)}개\n')

    success = 0
    for mmsi, vnum, st, et, dur, name in voyages:
        try:
            r = draw_voyage(mmsi, vnum, st, et, name)
            if r:
                path, pts, hours = r
                print(f'  ✓ {os.path.basename(path)}: {pts}점, {hours:.1f}h')
                success += 1
            else:
                print(f'  ✗ MMSI {mmsi} v{vnum}: 스킵')
        except Exception as e:
            print(f'  ✗ MMSI {mmsi} v{vnum}: {e}')

    print(f'\n완료: {success}개 HTML 파일 → {OUT_DIR}')


if __name__ == '__main__':
    main()
