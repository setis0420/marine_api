"""
AI Analyst 라우터
- 자연어 질문 → Claude가 SQL 생성 → 실행 → 결과 + 차트 반환
- 권한: api_users.ai_enabled = TRUE 인 사용자만
- 모델: Haiku 4.5 (기본)
"""
import os
import re
import time
import json
import math
from datetime import datetime, date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Header, Request, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import psycopg2

from services.auth_service import decode_token, get_local_conn
from config import AIS_DB, FISHERY_DB, LOCAL_DB, FISHERY_TABLES

# .env 로드
from pathlib import Path
_ENV_PATH = Path(__file__).parent.parent / '.env'
if _ENV_PATH.exists():
    for line in _ENV_PATH.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

import anthropic

router = APIRouter(prefix="/api/v1/analyst", tags=["AI Analyst"])

ANTHROPIC_MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001')
MAX_QUERY_ROWS = 10000
QUERY_TIMEOUT = 30  # seconds
DAILY_LIMIT = int(os.environ.get('ANALYST_DAILY_LIMIT', '10'))

# Haiku 4.5 요금 (USD per 1M tokens)
PRICE_INPUT = 1.00
PRICE_CACHE_WRITE = 1.25  # 5분 ephemeral: input × 1.25
PRICE_CACHE_READ = 0.10   # input × 0.1 (90% 할인)
PRICE_OUTPUT = 5.00

USD_TO_KRW = float(os.environ.get('USD_TO_KRW', '1400'))


def build_folium_map(rows, chart):
    """lat/lon 컬럼이 있는 결과를 folium HTML로 렌더링"""
    try:
        import folium
    except ImportError:
        return None

    lat_col = chart.get('lat')
    lon_col = chart.get('lon')
    label_col = chart.get('label')
    color_col = chart.get('color')
    title = chart.get('title') or ''
    if not lat_col or not lon_col or not rows:
        return None

    # 좌표 추출
    points = []
    for r in rows:
        try:
            lat = float(r.get(lat_col))
            lon = float(r.get(lon_col))
            if lat == 0 and lon == 0: continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180): continue
            points.append({'lat': lat, 'lon': lon, 'row': r})
        except (TypeError, ValueError):
            continue
    if not points: return None

    # 중심 + 줌
    avg_lat = sum(p['lat'] for p in points) / len(points)
    avg_lon = sum(p['lon'] for p in points) / len(points)
    lat_range = max(p['lat'] for p in points) - min(p['lat'] for p in points)
    lon_range = max(p['lon'] for p in points) - min(p['lon'] for p in points)
    span = max(lat_range, lon_range, 0.01)
    zoom = 10 if span < 0.5 else 8 if span < 2 else 6 if span < 8 else 4

    m = folium.Map(location=[avg_lat, avg_lon], zoom_start=zoom, tiles='CartoDB positron')

    # 색상 그라데이션 (수치 color_col이면)
    color_values = []
    if color_col:
        try:
            color_values = [float(p['row'].get(color_col)) for p in points if p['row'].get(color_col) is not None]
        except (TypeError, ValueError):
            color_values = []
    cmin, cmax = (min(color_values), max(color_values)) if color_values else (0, 1)

    def color_for(v):
        if not color_values: return '#3498db'
        try:
            f = float(v)
            t = (f - cmin) / (cmax - cmin + 1e-9)
            t = max(0, min(1, t))
            r = int(30 + t * 225)
            g = int(120 - t * 80)
            b = int(200 - t * 180)
            return f'#{r:02x}{g:02x}{b:02x}'
        except (TypeError, ValueError):
            return '#3498db'

    for p in points[:2000]:  # 최대 2000 마커
        label = ''
        if label_col and p['row'].get(label_col) is not None:
            label = str(p['row'].get(label_col))
        # 팝업: 모든 컬럼
        popup_html = '<div style="font-size:12px;">'
        for k, v in p['row'].items():
            popup_html += f'<b>{k}:</b> {v}<br>'
        popup_html += '</div>'
        color = color_for(p['row'].get(color_col)) if color_col else '#3498db'
        folium.CircleMarker(
            location=[p['lat'], p['lon']], radius=5,
            color=color, weight=1, fill=True, fill_color=color, fill_opacity=0.7,
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=label or f"{p['lat']:.4f}, {p['lon']:.4f}",
        ).add_to(m)

    if title:
        from branca.element import Element
        m.get_root().html.add_child(Element(
            f'<div style="position:fixed;top:10px;left:60px;background:white;padding:6px 12px;border:1px solid #bdc3c7;border-radius:4px;font-weight:600;z-index:9999;">{title}</div>'
        ))

    return m.get_root().render()


def compute_cost(input_tokens, cache_write_tokens, cache_read_tokens, output_tokens):
    """USD + KRW 비용 계산"""
    usd = (
        (input_tokens or 0) * PRICE_INPUT / 1_000_000 +
        (cache_write_tokens or 0) * PRICE_CACHE_WRITE / 1_000_000 +
        (cache_read_tokens or 0) * PRICE_CACHE_READ / 1_000_000 +
        (output_tokens or 0) * PRICE_OUTPUT / 1_000_000
    )
    return {'usd': round(usd, 6), 'krw': round(usd * USD_TO_KRW, 2)}


# === 사용자 검증 ===
def require_ai_user(authorization: str):
    """AI 사용 가능한 로그인 사용자 검증"""
    try:
        token = authorization.replace("Bearer ", "")
        payload = decode_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰")

    user_id = int(payload.get('sub'))
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("SELECT ai_enabled, role, is_active FROM api_users WHERE id=%s", (user_id,))
    r = cur.fetchone()
    cur.close(); conn.close()
    if not r or not r[2]:
        raise HTTPException(status_code=403, detail="비활성 사용자")
    if not r[0]:
        raise HTTPException(status_code=403, detail="AI 기능 권한이 없습니다. 관리자에게 문의하세요.")
    return {'user_id': user_id, 'username': payload.get('username'), 'role': r[1]}


def get_user_daily_limit(user_id: int) -> int:
    """사용자별 한도. NULL이면 글로벌 기본값"""
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("SELECT ai_daily_limit FROM api_users WHERE id=%s", (user_id,))
    r = cur.fetchone()
    cur.close(); conn.close()
    if r and r[0] is not None:
        return int(r[0])
    return DAILY_LIMIT


def check_daily_quota(user_id: int, role: str):
    """하루 호출 횟수 제한 (admin은 예외)"""
    if role == 'admin':
        return None
    limit = get_user_daily_limit(user_id)
    if limit <= 0:
        raise HTTPException(status_code=403, detail="AI Analyst 사용이 비활성화되었습니다")
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM api_analyst_log
        WHERE user_id = %s AND created_at::date = CURRENT_DATE
    """, (user_id,))
    used = cur.fetchone()[0]
    cur.close(); conn.close()
    if used >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"오늘 AI Analyst 사용 한도 초과 ({used}/{limit}회). 내일 다시 이용해주세요."
        )
    return {'used': used, 'limit': limit, 'remaining': limit - used}


# === DB 접속 ===
def _conn(db_key: str):
    if db_key == 'fishery':
        return psycopg2.connect(**FISHERY_DB)
    if db_key == 'ais':
        return psycopg2.connect(**AIS_DB)
    if db_key == 'marine':
        return psycopg2.connect(host=LOCAL_DB['host'], dbname=LOCAL_DB['database'],
                                user=LOCAL_DB['user'], password=LOCAL_DB['password'],
                                port=LOCAL_DB['port'])
    raise ValueError(f"unknown db: {db_key}")


# === SQL 안전 검사 ===
BLOCKED_PATTERNS = [
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|VACUUM)\b',
    r';\s*\S',  # 세미콜론 후 추가 문장
    r'--', r'/\*',  # 주석 (인젝션 방지)
    r'\bpg_\w+',  # pg_ 시스템 테이블
    r'\binformation_schema\b',  # schema 노출 방지
]

def validate_sql(sql: str):
    s = sql.strip()
    if not s.upper().startswith('SELECT') and not s.upper().startswith('WITH'):
        raise ValueError("SELECT 또는 WITH만 허용됩니다")
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, s, re.IGNORECASE):
            raise ValueError(f"허용되지 않은 키워드: {pat}")
    # LIMIT 강제
    if 'LIMIT' not in s.upper():
        s = s.rstrip(';').rstrip() + f' LIMIT {MAX_QUERY_ROWS}'
    return s


# === JSON 변환 ===
def to_json_safe(v):
    if v is None: return None
    if isinstance(v, (datetime, date)): return v.isoformat()
    if isinstance(v, Decimal):
        return float(v) if v.is_finite() else None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v): return None
        return v
    if isinstance(v, (bytes, bytearray)): return f"<binary {len(v)} bytes>"
    if isinstance(v, (int, bool)): return v
    return str(v)


# === SQL 실행 ===
def execute_sql(db: str, sql: str, limit: int = MAX_QUERY_ROWS):
    sql = validate_sql(sql)
    conn = _conn(db)
    cur = conn.cursor()
    try:
        cur.execute(f"SET statement_timeout = {QUERY_TIMEOUT*1000}")
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchmany(limit)
        data = [{cols[i]: to_json_safe(v) for i, v in enumerate(row)} for row in rows]
        return {'columns': cols, 'rows': data, 'row_count': len(data)}
    finally:
        cur.close(); conn.close()


# === 스키마 컨텍스트 (prompt caching 적용) ===
def build_schema_context():
    """Claude에게 전달할 DB 스키마 설명. 자주 쓰는 테이블 요약."""
    text = f"""당신은 한국 제주대학교 해양수산 빅데이터 DB를 분석하는 AI 분석가입니다.
사용자의 자연어 질문을 받아 SQL을 생성하고 실행하여 결과를 반환하세요.

=== 사용 가능한 데이터베이스 3개 ===

## 1) fishery (수산 데이터)
host: 203.253.202.21 / db: fishery
허용 테이블 (화이트리스트):
"""
    # fishery tables
    for t, info in FISHERY_TABLES.items():
        text += f"- {t}: {info['description']}\n"

    text += f"""
## 2) ais (AIS 항적 데이터, 전 선박)
host: 203.253.202.21 / db: aisdb
**월별 테이블 `dbYYYYMM`** (2012-01 ~ 2026-12, 총 228개 테이블). 예: `db202401` = 2024년 1월.
월별 테이블은 각 8억 행에 달하므로 반드시 **mmsi + datetime 범위 필터 필수**.
여객선/화물선/어선 구분 없이 모든 AIS 송신 선박 포함.

## 3) marine (선박 메타 + 분석 DB)
host: 203.253.202.171 / db: marine
- **shipinfo: 모든 선박 통합 메타정보 55,225척 (우선 사용)**
- fishing_shipinfo: 한국 어선 부속 정보 3,264척
- shipinfo_ner: 음성인식(NER) 전용 발음 변형 테이블 — **일반 조회에는 사용 금지**
- kfw_ebp_shipinfo: 내부 분석용 어선 1,006척 (사용자에 노출 X)
- kfw_ebp_voyage: 내부 항차 분석 (사용자에 노출 X)
- ship_{{mmsi}}: MMSI별 개별 항적 테이블 (조업 분석용)

=== 자주 쓰는 테이블의 주요 컬럼 (탐색 없이 바로 사용) ===

### ★ shipinfo (marine, 55,225척) — **선박 메타정보 통합 조회용 (우선 사용)**
여객선/화물선/어선 포함 모든 선박. 선박명·톤수·업종 등 메타정보는 **반드시 marine.shipinfo**에서 조회.
mmsi(bigint), shipname(text), shipname_kr(text), shipname_eng(text), imo_num(text),
callsign(text), shiptype(bigint), shiptype_portmis(text), company(text), nationality(text),
gross_ton(float), net_ton(float), intnational_ton(float),
loa(float), width(bigint), depth(float), draught(bigint), length(bigint),
fish_ship_name(text), fish_ship_num(float), fish_ton(text),
fishing_type(text), fishing_type_sub1(text), fishing_type_sub2(text),
nav_area(text), ship_num(text)

**shiptype_portmis 주요 값**: 일반화물선, 풀컨테이너선, 산물선(벌크선), 원양 어선, 석유제품 운반선,
견인용예선, 냉동.냉장선, **여객선**, 원유운반선, 케미칼 운반선, 연근해 어선, LNG 운반선

여객선/어선 구분 예:
```
SELECT mmsi, shipname_kr, shiptype_portmis, gross_ton, company
FROM shipinfo WHERE shiptype_portmis='여객선' AND nationality='대한민국' LIMIT 20
```

### fishing_shipinfo (marine, 3,264척 - 어선 보조)
shipinfo에 이미 대부분 컬럼 있으므로 특수한 경우에만 사용.

### fish_landing_suhyub (fishery, 1억건, 반드시 date 필터)
datetime(date), suhyub(str), fish_name(str), weight_kg(float), amount(int),
price_total(int), fishing_type(str), ship_id 없음
예: WHERE datetime BETWEEN '2024-01-01' AND '2024-12-31' AND fish_name='꽃게'

### fish_cpue (fishery)
year(int), month(int), fishing_type(str), fish_name(str), cpue(float), effort(float)

### fishing_tac (fishery)
year(int), fish_name(str), tac_weight_kg(float), catch_weight_kg(float), usage_pct(float)

### kfw_ebp_shipinfo (marine, 1006척) — **내부 분석용, 답변에 언급 금지**
특정 연구 과제(KFW/EBP) 대상 어선 내부 참조 테이블. 사용자에게 이 테이블명·프로젝트명 언급 X.
선박 기본정보는 shipinfo_ner 사용. 이 테이블은 port/business_type 등 일부 별도 컬럼이 필요하거나
ship_{{mmsi}} 항적 테이블과 연결할 때만 사용.
mmsi(text), group_type(text), vessel_name(text), tonnage(float), length(float),
business_type(text), port(text)

### kfw_ebp_voyage (marine)
mmsi(int), voyage_num(int), target_area(text, KFW/EBP/total),
start_time(ts), end_time(ts), duration(int, 분), model(text)

### ship_{{mmsi}} (marine, 개별 항적)
datetime(ts), lat(int, ×10^7), lon(int, ×10^7), sog(int, ×10),
port_entering(int), model_fishing_type(int), model_fishing_status(int)

### shipinfo_ner (fishery, 53,909척) — **여객선/화물선/운반선 등 모든 선박 통합 정보**
mmsi(bigint), shipname(text), shipname_kr(text), shipname_eng(text), imo_num(text),
callsign(text), shiptype(bigint), shiptype_portmis(text), company(text), nationality(text),
gross_ton(float), loa(float), width(bigint), fishing_type(text), port_entry_info 등

**shiptype_portmis 주요 값**: 일반화물선, 풀컨테이너선, 산물선(벌크선), 원양 어선, 석유제품 운반선,
견인용예선, 냉동.냉장선, 여객선, 원유운반선, 케미칼 운반선, 연근해 어선, LNG 운반선

여객선 찾기 예:
```
SELECT mmsi, shipname, shipname_kr, company, gross_ton, loa
FROM shipinfo_ner
WHERE shiptype_portmis = '여객선'
LIMIT 20
```

### haegu_location, fish_suhyub_location (fishery)
해구번호/수협 + lat, lon (위경도 그대로)

### fish_code (fishery, 184행): 어종 코드 참조
### fish_type_reference2 (fishery): 어업 유형 매핑

=== 항적 조회 방법 ===

**① 일반 선박 (여객선/화물선/어선 모두) — ais DB의 `dbYYYYMM` 월별 테이블**
컬럼: datetime(ts), mmsi(int), sog(smallint×10), lat(int×10^7), lon(int×10^7), cog(smallint×10), heading(smallint), nav_status, rot
```
SELECT datetime, lat/10000000.0 AS lat, lon/10000000.0 AS lon, sog/10.0 AS sog
FROM db202401
WHERE mmsi = 440156000
  AND datetime BETWEEN '2024-01-01' AND '2024-01-20'
ORDER BY datetime
LIMIT 5000
```
여러 달 걸치면 UNION ALL:
```
SELECT * FROM db202401 WHERE mmsi=XXX AND datetime >= '2024-01-15'
UNION ALL
SELECT * FROM db202402 WHERE mmsi=XXX AND datetime <  '2024-02-05'
```
대용량이라 반드시 mmsi 필터 먼저. 지도용 샘플링 예:
`WHERE mmsi=XXX AND EXTRACT(MINUTE FROM datetime)::int % 5 = 0`

**② 어선 조업/비조업 분석 — marine DB의 `ship_{{mmsi}}` (824개)**
이 테이블들은 조업 분류(model_fishing_type, model_fishing_status) 결과가 포함된 어선 전용.
일반 항적(여객선/화물선 등)은 ①번 ais.dbYYYYMM 사용.
컬럼: datetime, lat(×10^7), lon(×10^7), sog(×10), port_entering, model_fishing_type, model_fishing_status
```
SELECT datetime, lat/10000000.0 AS lat, lon/10000000.0 AS lon, sog/10.0 AS sog,
       model_fishing_type, model_fishing_status
FROM ship_440137010
WHERE datetime BETWEEN '2024-06-01' AND '2024-06-20'
ORDER BY datetime LIMIT 5000
```

**③ 항적 지도 시각화 — 필수 규칙**
위경도(lat/lon)를 포함한 결과는 **반드시** `chart={{"type":"map", "lat":"lat", "lon":"lon", "label":"datetime"}}` 지정.
scatter/bar/line은 지도가 아니라 차트라 항적 표시에 부적합. 항적 = 무조건 map.

**④ AIS sog/cog 특수값 주의**
- `sog = 1023` → "측정 불가/미지원" (AIS 표준 N/A 값). 항상 `AND sog < 1023` 또는 `AND sog BETWEEN 0 AND 500` 필터.
- `cog = 3600` → N/A, `heading = 511` → N/A 로도 필터.
- 속력 단위는 0.1노트. 실제 속도 = sog/10.0.
- 정상 조업/항해 속도는 1~30노트 (sog 10~300). 이를 벗어나면 정박 또는 에러.

**⑤ "속도 있는 구간" 질문 처리**
정박 제외한 항해 구간 찾기:
```
SELECT datetime, lat/10000000.0 AS lat, lon/10000000.0 AS lon, sog/10.0 AS sog_knots
FROM db202203
WHERE mmsi = 440156000
  AND sog BETWEEN 50 AND 500   -- 5~50노트 (항해중)
  AND datetime BETWEEN '2022-03-01' AND '2022-03-31'
ORDER BY datetime LIMIT 3000
```

=== 규칙 ===
1. 반드시 execute_sql tool을 사용해 SQL을 실행하세요.
2. SELECT/WITH만 사용. 테이블/컬럼 생성/수정 금지.
3. 대용량 테이블 (fish_landing_suhyub 1억건, fish_spatio_temporal_db 2100만건)은 반드시 WHERE로 필터링.
4. 날짜 필터 예: WHERE datetime BETWEEN '2024-01-01' AND '2024-12-31'
5. 결과가 많으면 집계(GROUP BY, COUNT, SUM, AVG) 활용.
6. LIMIT을 명시하지 않아도 최대 {MAX_QUERY_ROWS}행으로 자동 제한됨.
7. 한국 어선 MMSI는 440/441로 시작.
8. **항적/위치 결과(lat, lon 컬럼 포함)는 예외 없이 chart.type='map' 사용.** (scatter 금지)
9. AIS sog 필터 항상 적용: `AND sog < 1023` (1023=N/A).
10. 여러 쿼리 시도 후에도 실패하면 4회째에는 반드시 사용자에게 현재 상태와 제안을 답변으로 전달.
11. **선박 메타정보(선명/톤수/업종/국적 등)는 marine.shipinfo 하나로만 조회**. shipinfo_ner는 음성인식 전용이므로 사용 금지.
12. **최종 답변에 "KFW", "EBP", "KFW/EBP", "KFW_EBP", "kfw_ebp_" 같은 내부 프로젝트/테이블명을 노출하지 말 것.**
    그냥 "어선", "선박", "분석 대상" 등으로 표현. 사용자는 일반 연구자임.
13. 답변 수치는 전체 통계인지 부분집합인지 명확히. 특정 프로젝트 데이터면 "일부 표본 1,006척 기준" 같이 맥락 제공.

=== 응답 형식 ===
실행 후 다음 정보를 자연스러운 한국어로 알려주세요:
- 무엇을 조회했는지 요약
- 주요 인사이트 (최대값, 합계, 패턴 등)
- 차트가 적합하면 chart 정보 포함 (bar/line/pie + x컬럼, y컬럼)

=== 지도 시각화 ===
위경도 좌표가 포함된 결과는 지도로 시각화할 수 있습니다:
- chart.type = 'map'
- chart.lat = '위도컬럼명', chart.lon = '경도컬럼명'
- chart.label = '마커 툴팁 컬럼' (선택, 예: 수협명, 항구명)
- chart.color = '색상 기준 수치 컬럼' (선택, 그라데이션)

지도 사용 예시:
- 수협 위치 지도: SELECT name, lat, lon FROM fish_suhyub_location
- 해구별 위치: SELECT haegu_num, lat, lon FROM haegu_location
- 특정 선박 항적: SELECT datetime, lat/10000000.0 as lat, lon/10000000.0 as lon FROM ship_440137010 WHERE datetime BETWEEN '2024-01-01' AND '2024-01-07' LIMIT 2000

AIS/ship_* 테이블의 lat/lon은 ×10000000 저장이므로 /10000000.0 해서 SELECT 해야 합니다.

짧고 명확하게. 불필요한 장황함 금지.
"""
    return text


# === Anthropic tool 정의 ===
TOOLS = [
    {
        "name": "execute_sql",
        "description": "안전한 SELECT/WITH SQL을 실행하여 결과를 반환합니다. 결과가 위경도(lat, lon) 좌표를 포함하는 경우 chart.type='map'으로 지도 표시 가능.",
        "input_schema": {
            "type": "object",
            "properties": {
                "database": {
                    "type": "string",
                    "enum": ["fishery", "ais", "marine"],
                    "description": "대상 DB",
                },
                "sql": {
                    "type": "string",
                    "description": "실행할 SQL (SELECT/WITH만, 세미콜론 없이)",
                },
                "chart": {
                    "type": "object",
                    "description": "시각화 힌트 (선택). 지도 시각화는 type='map' 사용하고 lat/lon 컬럼명 지정.",
                    "properties": {
                        "type": {"type": "string", "enum": ["bar", "line", "pie", "scatter", "map", "none"]},
                        "x": {"type": "string", "description": "x축 컬럼명 (bar/line/pie/scatter용)"},
                        "y": {"type": "string", "description": "y축 컬럼명 (bar/line/pie/scatter용)"},
                        "lat": {"type": "string", "description": "위도 컬럼명 (map용)"},
                        "lon": {"type": "string", "description": "경도 컬럼명 (map용)"},
                        "label": {"type": "string", "description": "지도 마커 라벨 컬럼명 (선택)"},
                        "color": {"type": "string", "description": "지도 마커 색상 기준 컬럼명 (선택, 수치면 그라데이션)"},
                        "title": {"type": "string"},
                    },
                },
            },
            "required": ["database", "sql"],
        },
    }
]


# === 요청 모델 ===
class AskRequest(BaseModel):
    question: str
    history: list = []  # [{role, content}, ...] — optional multi-turn


# === 핵심 엔드포인트 ===
@router.post("/ask", summary="자연어 분석 질문")
def ask(req: AskRequest, authorization: str = Header(...)):
    """사용자 질문을 Claude에게 보내고 SQL을 실행하여 결과 반환"""
    user = require_ai_user(authorization)
    quota = check_daily_quota(user['user_id'], user['role'])
    t0 = time.time()

    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

    system = [
        {
            "type": "text",
            "text": build_schema_context(),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # 히스토리 + 새 질문
    messages = []
    for h in req.history[-6:]:  # 최근 6턴만
        if h.get('role') in ('user', 'assistant') and h.get('content'):
            messages.append({"role": h['role'], "content": h['content']})
    messages.append({"role": "user", "content": req.question})

    executed_queries = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read = 0
    total_cache_create = 0

    MAX_ITER = 5  # tool 루프 최대 반복

    for _ in range(MAX_ITER):
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2048,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        usage = resp.usage
        total_input_tokens += getattr(usage, 'input_tokens', 0) or 0
        total_output_tokens += getattr(usage, 'output_tokens', 0) or 0
        total_cache_read += getattr(usage, 'cache_read_input_tokens', 0) or 0
        total_cache_create += getattr(usage, 'cache_creation_input_tokens', 0) or 0

        if resp.stop_reason == 'tool_use':
            # assistant 턴 그대로 저장
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == 'tool_use' and block.name == 'execute_sql':
                    args = block.input
                    db = args.get('database', 'fishery')
                    sql = args.get('sql', '')
                    chart = args.get('chart')
                    try:
                        result = execute_sql(db, sql)
                        executed_queries.append({
                            'database': db, 'sql': sql, 'chart': chart,
                            'columns': result['columns'],
                            'rows': result['rows'],
                            'row_count': result['row_count'],
                        })
                        # Claude에게 요약만 전달 (용량 절약)
                        preview = result['rows'][:5]
                        tool_text = f"row_count={result['row_count']}, columns={result['columns']}, sample={json.dumps(preview, ensure_ascii=False, default=str)[:1000]}"
                        if result['row_count'] > 5:
                            tool_text += f" ...(+{result['row_count']-5}행)"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": tool_text,
                        })
                    except Exception as e:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"ERROR: {e}",
                            "is_error": True,
                        })
            messages.append({"role": "user", "content": tool_results})
            continue
        else:
            break

    # 최종 텍스트 답변 추출
    final_text = ""
    for block in resp.content:
        if getattr(block, 'type', None) == 'text':
            final_text += block.text

    duration_ms = int((time.time() - t0) * 1000)

    # 로깅 (ai 사용 기록)
    try:
        conn = get_local_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO api_analyst_log (user_id, question, answer, queries,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                duration_ms, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (user['user_id'], req.question[:2000], final_text[:5000],
              json.dumps(executed_queries, ensure_ascii=False, default=str)[:8000],
              total_input_tokens, total_output_tokens, total_cache_read,
              total_cache_create, duration_ms))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        print(f'[analyst log error] {e}')

    # 이번 요청 후 남은 횟수 계산
    remaining = None
    if user['role'] != 'admin':
        try:
            conn = get_local_conn()
            cur = conn.cursor()
            cur.execute("""SELECT COUNT(*) FROM api_analyst_log
                           WHERE user_id = %s AND created_at::date = CURRENT_DATE""",
                        (user['user_id'],))
            used_now = cur.fetchone()[0]
            cur.close(); conn.close()
            remaining = max(0, DAILY_LIMIT - used_now)
        except Exception:
            pass

    cost = compute_cost(total_input_tokens, total_cache_create, total_cache_read, total_output_tokens)
    return {
        'answer': final_text,
        'queries': executed_queries,
        'usage': {
            'input_tokens': total_input_tokens,
            'output_tokens': total_output_tokens,
            'cache_read_tokens': total_cache_read,
            'cache_creation_tokens': total_cache_create,
            'duration_ms': duration_ms,
            'cost_usd': cost['usd'],
            'cost_krw': cost['krw'],
        },
        'quota': {'daily_limit': DAILY_LIMIT, 'remaining': remaining} if remaining is not None else {'daily_limit': None, 'remaining': None, 'note': 'admin - 무제한'},
    }


@router.get("/quota", summary="내 오늘 사용량/남은 횟수")
def quota_info(authorization: str = Header(...)):
    """현재 사용자의 오늘 사용 횟수 + 누적 비용"""
    user = require_ai_user(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    # 오늘 사용 횟수
    cur.execute("""SELECT COUNT(*) FROM api_analyst_log
                   WHERE user_id = %s AND created_at::date = CURRENT_DATE""",
                (user['user_id'],))
    used = cur.fetchone()[0]
    # 누적 토큰/비용 (전체 기간 + 오늘)
    cur.execute("""SELECT
                     COALESCE(SUM(input_tokens),0),
                     COALESCE(SUM(cache_creation_tokens),0),
                     COALESCE(SUM(cache_read_tokens),0),
                     COALESCE(SUM(output_tokens),0),
                     COUNT(*)
                   FROM api_analyst_log WHERE user_id = %s""", (user['user_id'],))
    inp_t, cw_t, cr_t, out_t, total_calls = cur.fetchone()
    cur.execute("""SELECT
                     COALESCE(SUM(input_tokens),0),
                     COALESCE(SUM(cache_creation_tokens),0),
                     COALESCE(SUM(cache_read_tokens),0),
                     COALESCE(SUM(output_tokens),0)
                   FROM api_analyst_log
                   WHERE user_id = %s AND created_at::date = CURRENT_DATE""",
                (user['user_id'],))
    inp_d, cw_d, cr_d, out_d = cur.fetchone()
    cur.close(); conn.close()

    total_cost = compute_cost(inp_t, cw_t, cr_t, out_t)
    today_cost = compute_cost(inp_d, cw_d, cr_d, out_d)

    payload = {
        'used_today': used,
        'total_calls': total_calls,
        'today_cost_krw': today_cost['krw'],
        'today_cost_usd': today_cost['usd'],
        'total_cost_krw': total_cost['krw'],
        'total_cost_usd': total_cost['usd'],
    }
    if user['role'] == 'admin':
        payload.update({'daily_limit': None, 'remaining': None, 'note': 'admin은 무제한'})
    else:
        limit = get_user_daily_limit(user['user_id'])
        payload.update({'daily_limit': limit, 'remaining': max(0, limit - used)})
    return payload


@router.post("/ask/stream", summary="자연어 분석 (SSE 스트리밍)")
def ask_stream(req: AskRequest, authorization: str = Header(...)):
    """이벤트 스트리밍: status → query_start → query_result → answer → done"""
    user = require_ai_user(authorization)
    check_daily_quota(user['user_id'], user['role'])

    def event(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    def generator():
        t0 = time.time()
        try:
            yield event('status', {'text': '스키마 컨텍스트 준비 중...'})

            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
            system = [{
                "type": "text",
                "text": build_schema_context(),
                "cache_control": {"type": "ephemeral"},
            }]
            messages = []
            for h in (req.history or [])[-6:]:
                if h.get('role') in ('user', 'assistant') and h.get('content'):
                    messages.append({"role": h['role'], "content": h['content']})
            messages.append({"role": "user", "content": req.question})

            executed_queries = []
            total_in = total_out = total_cr = total_cw = 0

            yield event('status', {'text': 'Claude에게 질문 전달 중...'})

            final_text = ''
            resp = None
            for step in range(8):
                resp = client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=2048,
                    system=system,
                    tools=TOOLS,
                    messages=messages,
                )
                u = resp.usage
                total_in += getattr(u, 'input_tokens', 0) or 0
                total_out += getattr(u, 'output_tokens', 0) or 0
                total_cr += getattr(u, 'cache_read_input_tokens', 0) or 0
                total_cw += getattr(u, 'cache_creation_input_tokens', 0) or 0

                # 중간 텍스트 스트림 — tool_use 동반 시에만 "생각" 표시
                # 최종 답변(tool_use 없음)일 경우 done에서 answer로만 노출 → 중복 방지
                if resp.stop_reason == 'tool_use':
                    for block in resp.content:
                        if getattr(block, 'type', None) == 'text' and block.text:
                            yield event('thinking', {'text': block.text})

                if resp.stop_reason == 'tool_use':
                    messages.append({"role": "assistant", "content": resp.content})
                    tool_results = []
                    for block in resp.content:
                        if block.type == 'tool_use' and block.name == 'execute_sql':
                            args = block.input
                            db = args.get('database', 'fishery')
                            sql = args.get('sql', '')
                            chart = args.get('chart')
                            yield event('query_start', {'database': db, 'note': f'{db} DB 쿼리 실행 중...'})
                            try:
                                result = execute_sql(db, sql)
                                q_info = {
                                    'database': db, 'sql': sql, 'chart': chart,
                                    'columns': result['columns'],
                                    'rows': result['rows'],
                                    'row_count': result['row_count'],
                                }
                                executed_queries.append(q_info)
                                map_html = None
                                if chart and chart.get('type') == 'map':
                                    try:
                                        map_html = build_folium_map(result['rows'], chart)
                                    except Exception as me:
                                        print(f'[folium error] {me}')
                                yield event('query_result', {
                                    'database': db,
                                    'row_count': result['row_count'],
                                    'columns': result['columns'],
                                    'rows': result['rows'],
                                    'chart': chart,
                                    'map_html': map_html,
                                    'index': len(executed_queries) - 1,
                                })
                                preview = result['rows'][:5]
                                text = f"row_count={result['row_count']}, columns={result['columns']}, sample={json.dumps(preview, ensure_ascii=False, default=str)[:1000]}"
                                if result['row_count'] > 5:
                                    text += f" ...(+{result['row_count']-5}행)"
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": text,
                                })
                            except Exception as e:
                                yield event('query_error', {'database': db, 'error': str(e)})
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": f"ERROR: {e}",
                                    "is_error": True,
                                })
                    messages.append({"role": "user", "content": tool_results})
                    yield event('status', {'text': '결과 분석 중...'})
                    continue
                else:
                    break

            # 최종 답변 추출
            final_text = ''
            for block in resp.content:
                if getattr(block, 'type', None) == 'text':
                    final_text += block.text

            # 마지막이 tool_use로 끝났거나 답변이 너무 짧으면 요약 호출 1회 추가
            need_summary = (
                resp.stop_reason == 'tool_use'
                or len(final_text.strip()) < 40
                or '하겠습니다' in final_text[-60:]  # 중간 발화로 끝남
            )
            if need_summary and executed_queries:
                try:
                    # tool 결과를 적극적으로 활용하도록 프롬프트
                    messages.append({"role": "assistant", "content": resp.content})
                    if resp.stop_reason == 'tool_use':
                        # tool_result 없이 그냥 요약 요청하면 API 오류, 마지막 tool call에 더미 결과
                        dummy = []
                        for block in resp.content:
                            if getattr(block, 'type', None) == 'tool_use':
                                dummy.append({"type":"tool_result","tool_use_id":block.id,"content":"(이전 결과를 참조하여 사용자에게 최종 답변 제공)"})
                        if dummy:
                            messages.append({"role":"user","content":dummy})
                    messages.append({"role":"user","content":"지금까지 조회한 결과를 바탕으로 사용자에게 최종 답변을 한국어로 간결하게 정리해서 주세요. 추가 쿼리 없이 바로 답변만."})
                    yield event('status', {'text': '답변 정리 중...'})
                    summary_resp = client.messages.create(
                        model=ANTHROPIC_MODEL,
                        max_tokens=1024,
                        system=system,
                        messages=messages,  # tools 제거
                    )
                    u2 = summary_resp.usage
                    total_in += getattr(u2, 'input_tokens', 0) or 0
                    total_out += getattr(u2, 'output_tokens', 0) or 0
                    total_cr += getattr(u2, 'cache_read_input_tokens', 0) or 0
                    total_cw += getattr(u2, 'cache_creation_input_tokens', 0) or 0
                    summary_text = ''
                    for block in summary_resp.content:
                        if getattr(block, 'type', None) == 'text':
                            summary_text += block.text
                    if summary_text.strip():
                        final_text = summary_text
                except Exception as se:
                    print(f'[summary fallback error] {se}')

            duration_ms = int((time.time() - t0) * 1000)
            cost = compute_cost(total_in, total_cw, total_cr, total_out)

            # 로깅
            try:
                conn = get_local_conn()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO api_analyst_log (user_id, question, answer, queries,
                        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                        duration_ms, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """, (user['user_id'], req.question[:2000], final_text[:5000],
                      json.dumps(executed_queries, ensure_ascii=False, default=str)[:8000],
                      total_in, total_out, total_cr, total_cw, duration_ms))
                conn.commit()
                cur.close(); conn.close()
            except Exception as e:
                print(f'[analyst log error] {e}')

            # 남은 횟수
            remaining = None
            user_limit = None
            if user['role'] != 'admin':
                try:
                    user_limit = get_user_daily_limit(user['user_id'])
                    conn = get_local_conn()
                    cur = conn.cursor()
                    cur.execute("""SELECT COUNT(*) FROM api_analyst_log
                                   WHERE user_id = %s AND created_at::date = CURRENT_DATE""",
                                (user['user_id'],))
                    used = cur.fetchone()[0]
                    cur.close(); conn.close()
                    remaining = max(0, user_limit - used)
                except Exception:
                    pass

            yield event('done', {
                'answer': final_text,
                'usage': {
                    'input_tokens': total_in,
                    'output_tokens': total_out,
                    'cache_read_tokens': total_cr,
                    'cache_creation_tokens': total_cw,
                    'duration_ms': duration_ms,
                    'cost_usd': cost['usd'],
                    'cost_krw': cost['krw'],
                },
                'quota': {'daily_limit': user_limit, 'remaining': remaining},
            })
        except Exception as e:
            yield event('error', {'error': str(e)})

    return StreamingResponse(generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@router.get("/history", summary="내 분석 기록")
def history(limit: int = 50, authorization: str = Header(...)):
    user = require_ai_user(authorization)
    conn = get_local_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, question, answer, queries, input_tokens, output_tokens,
               cache_read_tokens, duration_ms, created_at
        FROM api_analyst_log
        WHERE user_id = %s
        ORDER BY id DESC LIMIT %s
    """, (user['user_id'], limit))
    items = [{
        'id': r[0], 'question': r[1], 'answer': r[2],
        'queries': json.loads(r[3]) if r[3] else [],
        'input_tokens': r[4], 'output_tokens': r[5],
        'cache_read_tokens': r[6], 'duration_ms': r[7],
        'created_at': str(r[8]),
    } for r in cur.fetchall()]
    cur.close(); conn.close()
    return {'count': len(items), 'items': items}


def ensure_analyst_table():
    """분석 로그 테이블 생성 (최초 실행 시)"""
    try:
        conn = get_local_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_analyst_log (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES api_users(id),
                question TEXT,
                answer TEXT,
                queries TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                duration_ms INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_analyst_user ON api_analyst_log(user_id, id DESC)")
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        print(f'[ensure_analyst_table] {e}')


ensure_analyst_table()
