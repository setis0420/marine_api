"""
선박별/년도별/항차별 항적 지도 생성
- 171 DB의 ship_{mmsi} + kfw_ebp_voyage 기반
- KFW=빨강, EBP=파랑, 밖=회색
- HTML + PNG 저장

사용법:
    python draw_all_voyages.py                        # 전체
    python draw_all_voyages.py --mmsi 440137010       # 특정 선박
    python draw_all_voyages.py --year 2023            # 특정 년도
    python draw_all_voyages.py --mmsi 440137010 --year 2023
"""

import os
import re
import time
import argparse
import logging
import psycopg2
import pandas as pd
import numpy as np
import folium
from folium.plugins import MousePosition
from shapely.geometry import Point, Polygon
from datetime import datetime

# PNG 캡처 (워커별 lazy 초기화)
_CHROME_SERVICE = None
HAS_SELENIUM = False
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager
    import logging as _logging
    _logging.getLogger('WDM').setLevel(_logging.NOTSET)
    HAS_SELENIUM = True
except ImportError:
    pass


def _get_chrome_service():
    global _CHROME_SERVICE
    if _CHROME_SERVICE is None:
        _CHROME_SERVICE = Service(ChromeDriverManager().install())
    return _CHROME_SERVICE

# === DB 설정 ===
DB_CONFIG = {
    'host': '203.253.202.171',
    'dbname': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
    'port': '5432'
}

# === 파일 경로 ===
BASE_DIR = r'k:\coding_project\해양수산 데이터 분석 플랫폼\voyage_map'
KFW_FILE = os.path.join(BASE_DIR, 'KFW.xlsx')
EBP_FILE = os.path.join(BASE_DIR, 'EBP.xlsx')
CABLE_FILE = os.path.join(BASE_DIR, 'cable route.xlsx')
EEZ_KR = os.path.join(BASE_DIR, 'korea_eez.geojson')
EEZ_JP = os.path.join(BASE_DIR, 'japan_eez.geojson')

SAVE_ROOT = r'H:\Dropbox\파일전송\어업피해조사\결과이미지'

# 로그
log_file = f'draw_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# KFW/EBP 폴리곤
_map_data = None

def load_map_data():
    global _map_data
    if _map_data:
        return _map_data
    import json
    kfw = pd.read_excel(KFW_FILE)
    ebp = pd.read_excel(EBP_FILE)
    cable = pd.read_excel(CABLE_FILE)
    with open(EEZ_KR) as f:
        kr_eez = json.load(f)
    with open(EEZ_JP) as f:
        jp_eez = json.load(f)
    poly_kfw = Polygon(kfw[['lon', 'lat']].values.tolist())
    poly_ebp = Polygon(ebp[['lon', 'lat']].values.tolist())
    _map_data = {
        'kfw': kfw, 'ebp': ebp, 'cable': cable,
        'kr_eez': kr_eez, 'jp_eez': jp_eez,
        'poly_kfw': poly_kfw, 'poly_ebp': poly_ebp,
    }
    return _map_data


def folium_html_to_png(html_path, png_path=None, width=1200, height=1000, delay=1.0):
    if not HAS_SELENIUM:
        return None
    html_path = os.path.abspath(html_path)
    if png_path is None:
        png_path = os.path.splitext(html_path)[0] + ".png"
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument(f"--window-size={width},{height}")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--hide-scrollbars")
    driver = webdriver.Chrome(service=_get_chrome_service(), options=opts)
    try:
        driver.get("file:///" + html_path.replace("\\", "/"))
        time.sleep(delay)
        driver.save_screenshot(png_path)
    finally:
        driver.quit()
    return png_path


def draw_voyage(mmsi, voyage_num, start_time, end_time, vessel_name, conn, voyage_duration=None):
    """한 항차의 항적 지도 생성"""
    md = load_map_data()
    cur = conn.cursor()
    ship_table = f"ship_{mmsi}"

    # voyage 테이블에서 KFW/EBP/total duration 조회
    cur.execute("""
        SELECT target_area, duration FROM kfw_ebp_voyage
        WHERE mmsi = %s AND voyage_num = %s
    """, (int(mmsi), voyage_num))
    dur_map = {r[0]: r[1] for r in cur.fetchall()}
    dur_kfw = dur_map.get('KFW', 0) or 0
    dur_ebp = dur_map.get('EBP', 0) or 0
    dur_total = dur_map.get('total', 0) or 0

    # 캡처 범위
    latlmt = [35.33, 35.61]
    lonlmt = [130.15, 130.52]
    # 범위 약간 확장 (0.05도)
    lat_min_q = (latlmt[0] - 0.05) * 10000000
    lat_max_q = (latlmt[1] + 0.05) * 10000000
    lon_min_q = (lonlmt[0] - 0.05) * 10000000
    lon_max_q = (lonlmt[1] + 0.05) * 10000000

    # 항적 데이터 조회 (캡처 범위 내만)
    cur.execute(f"""
        SELECT datetime, lat/10000000.0 as lat, lon/10000000.0 as lon, sog/10.0 as sog,
               port_entering, model_fishing_type, model_fishing_status
        FROM {ship_table}
        WHERE datetime >= %s AND datetime <= %s
          AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
        ORDER BY datetime
    """, (start_time, end_time, lat_min_q, lat_max_q, lon_min_q, lon_max_q))
    rows = cur.fetchall()

    if not rows:
        rows = []

    trj = pd.DataFrame(rows, columns=['datetime', 'lat', 'lon', 'sog', 'port_entering', 'fishing_type', 'fishing_status'])
    if not trj.empty:
        trj[['lat', 'lon', 'sog']] = trj[['lat', 'lon', 'sog']].astype(float)
        trj['datetime'] = pd.to_datetime(trj['datetime'])

    # 1분 보간
    if len(trj) >= 2 and (trj['datetime'].iloc[-1] - trj['datetime'].iloc[0]).total_seconds() > 60:
        dt_range = pd.date_range(start=trj['datetime'].iloc[0].floor('min'), end=trj['datetime'].iloc[-1], freq='1min')
        trj_orig = trj.copy()
        trj_orig['real'] = True
        merged = pd.concat([trj_orig, pd.DataFrame({'datetime': dt_range})]).set_index('datetime').sort_index()
        for c in ['lat', 'lon', 'sog']:
            merged[c] = merged[c].astype(float).interpolate(method='linear')
        # 카테고리 컬럼은 원본 제거 전에 ffill
        merged['fishing_type'] = merged['fishing_type'].ffill().bfill()
        merged['fishing_status'] = merged['fishing_status'].ffill().bfill()
        merged['port_entering'] = merged['port_entering'].ffill().bfill()
        # 원본 제거 (보간 행만 남김)
        merged = merged[merged['real'] != True].drop(columns=['real']).reset_index()
        trj_interp = merged.dropna(subset=['lat', 'lon'])
    elif not trj.empty:
        trj_interp = trj.dropna(subset=['lat', 'lon'])
    else:
        trj_interp = pd.DataFrame(columns=['lat', 'lon', 'sog', 'fishing_type', 'fishing_status', 'port_entering'])

    # 지도 생성
    map_center = [float(md['kfw']['lat'].mean()), float(md['kfw']['lon'].mean())]
    m = folium.Map(location=map_center, zoom_start=10, tiles='CartoDB positron')

    # EEZ
    import json
    folium.GeoJson(md['kr_eez'], style_function=lambda x: {'color': '#ff4444', 'weight': 2, 'fillOpacity': 0.03, 'dashArray': '5,5'}).add_to(m)
    folium.GeoJson(md['jp_eez'], style_function=lambda x: {'color': '#4444ff', 'weight': 2, 'fillOpacity': 0.03, 'dashArray': '5,5'}).add_to(m)

    # KFW/EBP/Cable
    folium.Polygon(md['kfw'][['lat', 'lon']].values.tolist(), color='blue', weight=2, fill=True, fill_color='blue', fill_opacity=0.2).add_to(m)
    folium.Polygon(md['ebp'][['lat', 'lon']].values.tolist(), color='green', weight=2, fill=True, fill_color='green', fill_opacity=0.2).add_to(m)
    folium.PolyLine(md['cable'][['lat', 'lon']].values.tolist(), color='red', weight=2).add_to(m)

    # 보간된 항적 점
    # fill_color: 조업상태 (0=비조업:gray, 1=투망:red, 2=양망:orange, 3=대기:yellow)
    # edge color: 구역 (KFW:red, EBP:blue, 밖:없음)
    status_colors = {0: 'gray', 1: 'red', 2: 'orange', 3: 'yellow'}
    fg = folium.FeatureGroup(name="항적", show=True)

    # 모델 기준 KFW/EBP 조업 시간 계산
    model_kfw_min = 0
    model_ebp_min = 0
    model_total_min = 0

    for _, row in trj_interp.iterrows():
        lat, lon = float(row['lat']), float(row['lon'])
        pt = Point(lon, lat)

        fs = int(row['fishing_status']) if pd.notna(row.get('fishing_status')) else 0
        fill_color = status_colors.get(fs, 'gray')
        is_fishing = fs in [1, 2]  # 투망 또는 양망

        in_kfw = md['poly_kfw'].contains(pt)
        in_ebp = md['poly_ebp'].contains(pt)

        # edge: 구역
        if in_kfw:
            edge_color = 'red'
        elif in_ebp:
            edge_color = 'blue'
        else:
            edge_color = fill_color

        # 모델 기준 조업 시간
        if is_fishing:
            model_total_min += 1
            if in_kfw:
                model_kfw_min += 1
            if in_ebp:
                model_ebp_min += 1

        folium.CircleMarker(
            [lat, lon], radius=4,
            color=edge_color, weight=2,
            fill=True, fill_color=fill_color, fill_opacity=0.8
        ).add_to(fg)

    m.add_child(fg)

    m.fit_bounds([[latlmt[0], lonlmt[0]], [latlmt[1], lonlmt[1]]])
    folium.Rectangle(bounds=[[latlmt[0], lonlmt[0]], [latlmt[1], lonlmt[1]]], color="black", weight=1, fill=False, dash_array="5,5").add_to(m)

    # 정보 박스
    type_names = {0: '권현망', 1: '선망', 2: '안강망', 3: '연승', 4: '자망', 5: '채낚기', 6: '통발', 7: '트롤'}
    ft = trj_interp['fishing_type'].dropna().mode()
    ft_str = f"{int(ft.iloc[0])}({type_names.get(int(ft.iloc[0]), '?')})" if len(ft) > 0 else '-'

    start_str = str(start_time)[:10]
    end_str = str(end_time)[:10]

    model_kfw_h = model_kfw_min / 60.0
    model_ebp_h = model_ebp_min / 60.0
    model_total_h = model_total_min / 60.0

    html_stats = f"""
    <div style="position:absolute; top:10px; right:10px; z-index:9999;
        font-size:13px; background:rgba(255,255,255,0.9); padding:10px 14px;
        border:1px solid #555; border-radius:4px; line-height:1.5;">
        <b>MMSI:</b> {mmsi} | <b>선명:</b> {vessel_name}<br>
        <b>항차:</b> {voyage_num} | <b>유형:</b> {ft_str}<br>
        <b>기간:</b> {start_str} ~ {end_str}<br>
        <hr style="margin:4px 0;">
        <b>[모델 조업]</b><br>
        <b>Total:</b> {model_total_h:.1f}h ({model_total_min}분)<br>
        <span style="color:red;"><b>KFW:</b> {model_kfw_h:.1f}h ({model_kfw_min}분)</span> |
        <span style="color:blue;"><b>EBP:</b> {model_ebp_h:.1f}h ({model_ebp_min}분)</span><br>
        <hr style="margin:4px 0;">
        <b>[속력 기준]</b><br>
        <b>Total:</b> {dur_total:.1f}h |
        <span style="color:red;"><b>KFW:</b> {dur_kfw:.1f}h</span> |
        <span style="color:blue;"><b>EBP:</b> {dur_ebp:.1f}h</span><br>
        <hr style="margin:4px 0;">
        <span style="color:red;">●</span> 투망 &nbsp;<span style="color:orange;">●</span> 양망 &nbsp;
        <span style="color:gray;">●</span> 비조업 &nbsp;<span style="color:yellow;">●</span> 대기<br>
        <span style="border:2px solid red;padding:0 4px;">□</span> KFW &nbsp;
        <span style="border:2px solid blue;padding:0 4px;">□</span> EBP
    </div>
    """
    m.get_root().html.add_child(folium.Element(html_stats))

    # 저장
    year = str(start_time)[:4]
    folder = os.path.join(SAVE_ROOT, f"{mmsi}_{vessel_name}", year)
    os.makedirs(folder, exist_ok=True)

    fname = f"{mmsi}_{voyage_num}_{start_str}_{end_str}_kfw{model_kfw_min}_ebp{model_ebp_min}"

    # 임시 HTML 저장 → PNG 캡처 → HTML 삭제
    html_path = os.path.join(folder, fname + ".html")
    png_path = os.path.join(folder, fname + ".png")
    m.save(html_path)

    if HAS_SELENIUM:
        try:
            folium_html_to_png(html_path, png_path=png_path, width=800, height=800, delay=1.0)
        except:
            pass

    # HTML 삭제
    try:
        os.remove(html_path)
    except:
        pass

    return {'png': png_path, 'total_h': dur_total, 'kfw_h': dur_kfw, 'ebp_h': dur_ebp}


NUM_WORKERS = 15


def check_png_exists(mmsi, vnum, start, name):
    """이미 생성된 PNG가 있는지 확인"""
    import glob
    year = str(start)[:4]
    start_str = str(start)[:10]
    folder = os.path.join(SAVE_ROOT, f"{mmsi}_{name}", year)
    pattern = os.path.join(folder, f"{mmsi}_{vnum}_{start_str}_*_kfw*_ebp*.png")
    return len(glob.glob(pattern)) > 0


def draw_voyage_worker(args):
    """멀티프로세스 워커"""
    mmsi, vnum, start, end, name = args
    name = name or str(mmsi)

    # 이미 생성된 이미지가 있으면 스킵
    if check_png_exists(mmsi, vnum, start, name):
        return (mmsi, vnum, 0, 0, 0, 'skip')

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        result = draw_voyage(mmsi, vnum, start, end, name, conn)
        conn.close()
        if result:
            return (mmsi, vnum, result['total_h'], result['kfw_h'], result['ebp_h'], None)
        return (mmsi, vnum, 0, 0, 0, 'no data')
    except Exception as e:
        if conn:
            try: conn.close()
            except: pass
        return (mmsi, vnum, 0, 0, 0, str(e))


def main():
    from multiprocessing import Pool

    parser = argparse.ArgumentParser(description='선박별/년도별/항차별 항적 지도')
    parser.add_argument('--mmsi', type=int, default=None)
    parser.add_argument('--year', type=int, default=None)
    parser.add_argument('--workers', type=int, default=NUM_WORKERS)
    parser.add_argument('--no-png', action='store_true', help='PNG 생성 안함')
    parser.add_argument('--xlsx', type=str, default=None, help='mmsi 목록이 담긴 엑셀 파일 경로')
    parser.add_argument('--start', type=int, default=None, help='엑셀 시작 번호 (1부터)')
    parser.add_argument('--end', type=int, default=None, help='엑셀 끝 번호 (포함)')
    args = parser.parse_args()

    global HAS_SELENIUM
    if args.no_png:
        HAS_SELENIUM = False

    logging.info("=" * 60)
    logging.info(f"선박별/년도별/항차별 항적 지도 생성 (x{args.workers} 프로세스)")
    logging.info(f"저장: {SAVE_ROOT}")
    logging.info("=" * 60)

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    # 누락 선박 목록 (항적이미지 누락.xlsx 기준 451척)
    MISSING_MMSI = [
        440217340,440701390,440137010,440409310,440600351,440323660,440050560,440079930,440209330,440140760,
        440705170,440703050,440200238,440145950,440025090,440408410,440110480,440600181,440059630,440100368,
        440408240,440326710,440051710,440331290,440406150,440406020,440080940,440402770,440126140,440091040,
        440403910,440204770,440402530,440404230,440413110,440404250,440404660,440061880,440700780,440332730,
        440401520,440409660,440308010,440700850,440208010,440132110,440319660,440067880,440411060,440148940,
        440600240,440412520,440098940,440101390,440070220,440119290,440414150,440406040,440600316,440170080,
        440125720,440120430,440600374,440600030,440600393,440600040,440405810,440150220,440150580,440404640,
        440412760,440600015,440142370,440410950,440403340,440400160,440412240,440600535,440410480,440126340,
        440401020,440600192,440404630,440015140,440107860,440133090,440134060,440402630,440193680,440121320,
        440600114,440404540,440123760,440056570,440600512,440125740,440108160,440132130,440121160,440601480,
        440401680,440412060,440150190,440019260,440148090,440104780,440009150,440136110,440142520,440118430,
        440154250,441206000,440402690,440017900,440147310,440120950,440405520,440125180,440029250,440103720,
        440106500,440121730,441272000,440117850,440060290,440105820,440110990,440413180,440105320,440113970,
        441624000,440410840,440198180,440402960,440413050,440151080,440191220,440023890,440195340,440405330,
        440044990,440191230,440126950,440154850,440600140,440137850,440133080,440061860,440403950,441074000,
        440129490,440154010,440401970,440401800,440403490,440149760,440107550,440056990,440503600,440001060,
        440155510,440735000,440405980,440145420,440053060,440148150,440105680,440151290,440104810,440600166,
        440145450,440046550,440196880,440066640,440404550,440147560,440067980,440117230,440134370,440157450,
        440304830,440041240,440400890,440120530,440105140,440112280,441371000,440124870,440052480,440014010,
        440132990,440121650,440121180,440600521,440125150,440600407,440409110,440125160,440121850,440125970,
        440114260,440132260,440117590,440005560,440403370,440600402,440404620,440402510,440100120,440402440,
        440600370,440117530,440406010,440334240,440402520,440126250,440109300,440600596,440600133,440155220,
        440323510,440117110,440700650,440067000,440082290,440107960,440113360,441026000,440406080,440148140,
        440121170,440581000,440158240,440112040,440132950,440101100,440148160,440147710,440146500,440316590,
        440121680,440406130,440150520,440125750,440104540,440084580,440600250,440120830,441147000,440912000,
        440145090,440158360,440045780,440109280,440411750,440125760,440708030,440101860,440108180,440018300,
        440070850,440125630,440105940,440701870,440147160,440411030,440404710,440150860,440105310,440187980,
        440063640,440404790,440044390,440600128,440406110,440070870,440146920,440070770,441295000,440123370,
        440133610,440096970,440401160,440149050,440105660,440600539,440402270,440128270,440403510,440066150,
        440130010,440127060,440149930,440106740,440017700,440502000,440145410,440140200,440600523,440028280,
        440410260,440600285,440017050,440600385,440600055,440013890,440404340,440011600,440408660,440403480,
        440117260,440142360,440169190,440401690,440600414,440404680,440106080,440412570,440081160,440406120,
        440107660,440217740,440060000,440025000,440600123,440600515,440140510,440117340,440145430,440108350,
        440114130,440107990,440600475,441713000,440117890,440406050,440401610,440125090,440131500,440111720,
        440703030,440113370,440121530,440405430,440600175,440096000,440106720,440500173,440701910,440125640,
        440109040,440065110,440100156,440081210,440170960,440144660,440132960,440038000,440124730,440123230,
        440005070,440600387,440702780,440076000,440323960,440015700,440600348,440146730,440103360,440110190,
        440600506,440855000,440118100,440408550,440403450,440411270,440404740,440201440,440142840,440101690,
        440722000,440128980,440124740,440195720,440107670,440403690,440600185,440600223,440401150,440117370,
        440125710,440132760,440113390,440153520,440411860,440405510,440600171,440143700,440161210,440165240,
        440121920,440124540,440173130,440101040,440403320,440150330,440412860,440600454,440400850,440600127,
        440149120,440079920,440048020,440401660,440026390,440120200,440117480,440145120,440600124,440015800,
        440405320,440103710,440208050,440402190,440701020,440218130,440402760,440131590,440408230,440402680,
        440601030,440053860,440122840,440411710,440411770,440125320,440112320,440157570,440502680,440402740,
        440011150,440083240,440010500,440402490,440403830,440149780,440022850,440068410,440186250,440412830,
        440410770,
    ]

    # xlsx 또는 내장 목록에서 mmsi 추출
    xlsx_mmsi = None
    if args.xlsx:
        import re as _re
        _df = pd.read_excel(args.xlsx)
        # 번호 범위 필터링
        if args.start is not None or args.end is not None:
            s = (args.start or 1) - 1  # 0-based
            e = args.end or len(_df)
            _df = _df.iloc[s:e]
            logging.info(f"엑셀 {s+1}~{e}번 선택 ({len(_df)}척)")
        xlsx_mmsi = set()
        for val in _df.iloc[:, 1]:
            _m = _re.search(r'(\d{9})', str(val))
            if _m:
                xlsx_mmsi.add(int(_m.group(1)))
        logging.info(f"엑셀에서 {len(xlsx_mmsi)}척 mmsi 로드")
    else:
        # --start/--end만 지정한 경우 내장 목록 사용
        if args.start is not None or args.end is not None:
            s = (args.start or 1) - 1
            e = args.end or len(MISSING_MMSI)
            xlsx_mmsi = set(MISSING_MMSI[s:e])
            logging.info(f"내장 목록 {s+1}~{e}번 선택 ({len(xlsx_mmsi)}척)")

    # 대상 voyage 조회
    where = "WHERE target_area = 'total'"
    params = []
    if args.mmsi:
        where += " AND v.mmsi = %s"
        params.append(args.mmsi)
    if xlsx_mmsi:
        where += " AND v.mmsi = ANY(%s)"
        params.append(list(xlsx_mmsi))
    if args.year:
        where += " AND EXTRACT(YEAR FROM v.start_time) = %s"
        params.append(args.year)

    cur.execute(f"""
        SELECT v.mmsi, v.voyage_num, v.start_time, v.end_time, s.vessel_name
        FROM kfw_ebp_voyage v
        LEFT JOIN kfw_ebp_shipinfo s ON v.mmsi::text = s.mmsi
        {where}
        ORDER BY v.mmsi, v.voyage_num
    """, params)
    voyages = cur.fetchall()
    cur.close()
    conn.close()

    tasks = [(mmsi, vnum, start, end, name or str(mmsi)) for mmsi, vnum, start, end, name in voyages]

    logging.info(f"대상: {len(tasks)}개 항차")
    logging.info("")

    if not tasks:
        logging.info("처리할 항차 없음")
        return

    total = 0
    skipped = 0
    completed = 0

    try:
        with Pool(processes=args.workers) as pool:
            for result in pool.imap_unordered(draw_voyage_worker, tasks):
                mmsi, vnum, total_h, kfw_h, ebp_h, err = result
                completed += 1
                if err == 'skip':
                    skipped += 1
                    if skipped % 500 == 0:
                        logging.info(f"  [{completed}/{len(tasks)}] 스킵 누적: {skipped}")
                elif err:
                    logging.error(f"  [{completed}/{len(tasks)}] {mmsi} 항차{vnum} 오류: {err}")
                else:
                    total += 1
                    logging.info(f"  [{completed}/{len(tasks)}] {mmsi} 항차{vnum}: "
                                 f"total={total_h:.1f}h KFW={kfw_h:.1f}h EBP={ebp_h:.1f}h")
    except KeyboardInterrupt:
        logging.info("\n중단됨!")

    logging.info("")
    logging.info(f"완료! {total}개 생성 | {skipped}개 스킵")


if __name__ == '__main__':
    main()
