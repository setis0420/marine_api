# fishing_voyage 입항 좌표 + 수협 매칭 프로세스

**작성일**: 2026-05-04
**스크립트**: [match_voyage_johab.py](../match_voyage_johab.py)
**소요 시간**: 7.6시간 (27,523초)
**대상**: fishing_voyage 1,434,475행

---

## 1. 배경

### 문제
- 항차(voyage)가 끝났을 때 어디 항구로 들어왔는지 정보 부재
- 수협-항차 연결을 위해 **입항 좌표 + 수협 매칭** 필요

### 결과 컬럼 (fishing_voyage 추가)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | BIGSERIAL | FK용 surrogate key (위판 테이블 연결) |
| `entry_lat` | DOUBLE PRECISION | 입항 시점 위도 |
| `entry_lon` | DOUBLE PRECISION | 입항 시점 경도 |
| `johab_code` | BIGINT | 매칭된 수협 코드 |
| `johab_name` | TEXT | 매칭된 수협명 |
| `entry_distance_km` | REAL | 매칭 거리 (km) |

---

## 2. 처리 단계

### Phase 1: 컬럼 추가
```sql
ALTER TABLE fishing_voyage
  ADD COLUMN id BIGSERIAL,
  ADD COLUMN entry_lat DOUBLE PRECISION,
  ADD COLUMN entry_lon DOUBLE PRECISION,
  ADD COLUMN johab_code BIGINT,
  ADD COLUMN johab_name TEXT,
  ADD COLUMN entry_distance_km REAL;
CREATE UNIQUE INDEX ON fishing_voyage(id);
```
소요: 즉시

### Phase 2: 입항 좌표 추출 (척별)

각 항차의 `end_time` 시각의 위치를 `ship_{mmsi}`에서 조회

```sql
UPDATE fishing_voyage v
SET entry_lat = s.lat / 10000000.0,
    entry_lon = s.lon / 10000000.0
FROM ship_{mmsi} s
WHERE v.mmsi = {mmsi}
  AND v.end_time = s.datetime  -- datetime PK로 빠른 조인
  AND v.entry_lat IS NULL      -- 재실행 안전 (이미 처리된 것 스킵)
```

- ship_{mmsi}.datetime이 PRIMARY KEY → 즉시 조회
- 좌표 변환: 정수(×10^7) → 실제 위경도 (예: 357748333 / 10000000.0 = 35.7748333)
- 1,323척 처리, 척당 ~1초 (다른 fetch 작업과 락 경합 시 더 느림)

**연결 끊김 처리**
- `psycopg2.OperationalError: server closed the connection` 발생 → 자동 재연결 + 재시도 (최대 3회)
- `WHERE entry_lat IS NULL` 필터로 idempotent 보장 (중간 재시작 가능)

소요: 약 6시간 (락 경합 영향)

### Phase 3: 수협 매칭

**소스 데이터**: `fish_suhyub_location` (21번 fishery DB)
- 139행 / 69 수협 / 좌표 보유 56수협 / 81구역
- 한 수협이 1~4개 사각형 구역 가짐 (예: 통영수협 = 4구역)

```sql
SELECT johab_code, johab_name,
       (lat_start + lat_end)/2 AS lat,
       (lon_start + lon_end)/2 AS lon
FROM fish_suhyub_location
WHERE lat_start IS NOT NULL
```

**알고리즘** (Haversine 거리 기반 nearest neighbor)

```python
import numpy as np

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088  # 지구 반경 (km)
    lat1_r = np.radians(lat1)
    lat2_r = np.radians(lat2)
    dlat = lat2_r - lat1_r
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat/2)**2 + np.cos(lat1_r)*np.cos(lat2_r)*np.sin(dlon/2)**2
    return 2 * R * np.arcsin(np.sqrt(a))

# 각 voyage 입항점 → 81구역 모두와 거리 계산
for voyage in voyages:
    distances = haversine_km(voy.lat, voy.lon, sh_lats, sh_lons)  # numpy (81,)
    min_idx = np.argmin(distances)
    min_dist = distances[min_idx]

    if min_dist <= 5.0:  # 5km 임계값
        voyage.johab_code = sh_codes[min_idx]
        voyage.johab_name = sh_names[min_idx]
        voyage.entry_distance_km = min_dist
    # else: NULL
```

**규칙**
- 가장 가까운 수협 1곳만 매핑
- 5km 초과 시 NULL (매칭 실패)
- 한 수협이 여러 구역 → 가장 가까운 구역 기준

**처리 효율**
- 110만 voyage × 81구역 = 약 9천만 거리 계산
- numpy 벡터화 → 약 5분
- DB UPDATE (TEMP TABLE + JOIN UPDATE) → 80분

소요: 약 1.5시간

### Phase 4: 인덱스
```sql
CREATE INDEX idx_fv_johab ON fishing_voyage(johab_code);
CREATE INDEX idx_fv_entry ON fishing_voyage(entry_lat, entry_lon);
```

---

## 3. 최종 결과

### 매칭 통계 (전체 1,434,475 voyage)

| 분류 | 수치 | 비율 |
|---|---:|---:|
| ✅ 수협 매칭 성공 | **588,749** | **41.0%** |
| ❌ 좌표 있으나 5km 초과 | 512,244 | 35.7% |
| ❌ 좌표 없음 | 333,482 | 23.2% |
| 평균 매칭 거리 | 1.74 km | - |
| 매칭된 고유 수협 수 | 55개 | - |

### 매칭 상위 10 수협

| 순위 | 수협 | 항차 수 |
|---:|---|---:|
| 1 | 부산시수산업협동조합 | 117,828 |
| 2 | 울산수산업협동조합 | 62,220 |
| 3 | 서산수산업협동조합 | 56,644 |
| 4 | 구룡포수산업협동조합 | 40,591 |
| 5 | 경주시수산업협동조합 | 38,522 |
| 6 | 보령수산업협동조합 | 36,847 |
| 7 | 한림수산업협동조합 | 27,316 |
| 8 | 죽변수산업협동조합 | 23,507 |
| 9 | 성산포수산업협동조합 | 20,943 |
| 10 | 제주시수산업협동조합 | 16,019 |

→ 부산/울산/서산 상위 3곳이 전체 매칭의 약 **40%** 차지

### 지역별 매칭 분포 (개략)
- **남해 동부**: 부산, 울산, 통영, 거제, 기장 (대형선망/트롤 거점)
- **동해**: 구룡포, 경주, 죽변, 후포, 포항, 영덕 (오징어/대게)
- **서해**: 서산, 보령, 안면도, 신안 (꽃게/안강망)
- **제주**: 한림, 성산포, 제주시, 서귀포, 모슬포, 추자도

---

## 4. 미매칭 사유 분석

### 23.2% (333,482) — 좌표 자체 없음
- ship_{mmsi}에 voyage end_time과 정확히 일치하는 datetime 행 없음
- ship_{mmsi} 테이블이 아예 없는 mmsi (231개)
- 데이터 fetch 미완료 또는 손실

**개선 방안** (선택)
- end_time ±1분 범위 내 가장 가까운 행 매칭하도록 변경
- ship_{mmsi} 미확보 척은 fetch 후 재처리

### 35.7% (512,244) — 5km 초과
- 좌표 미보유 13개 수협 (강진, 경남고성 등) 인근 입항
- **외해 입항**: 외국항, 먼바다 입항 (실제 한국 수협 관할 외)
- **수협 구역 중심점 부정확**: 큰 항만(부산 등)을 사각형 중심점 1개로 대표 → 외곽 입항점은 5km 초과 가능
- 좌표 있는 수협 데이터의 사각형이 실제 항구 위치와 어긋난 경우

**개선 방안** (선택)
- 임계값 5km → 10km 완화 (정확도 vs 매칭률 tradeoff)
- 좌표 미보유 13개 수협 좌표 보강
- 큰 항만은 다중 중심점 (구역별) 사용

---

## 5. 활용 예시

### 특정 수협 입항 항차
```sql
SELECT mmsi, voyage_num, end_time, entry_distance_km
FROM fishing_voyage
WHERE johab_name = '부산시수산업협동조합'
ORDER BY end_time DESC LIMIT 100;
```

### 어선별 주 입항 수협
```sql
SELECT mmsi, johab_name, COUNT(*) AS visits
FROM fishing_voyage
WHERE johab_code IS NOT NULL
GROUP BY mmsi, johab_name
ORDER BY mmsi, visits DESC;
```

### 월별 수협 입항량
```sql
SELECT DATE_TRUNC('month', end_time) AS month,
       johab_name, COUNT(*) AS entries
FROM fishing_voyage
WHERE johab_code IS NOT NULL AND target_area='total'
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

### 입항 위치 지도화 (folium)
```python
SELECT entry_lat, entry_lon, johab_name, COUNT(*)
FROM fishing_voyage
WHERE entry_lat IS NOT NULL
GROUP BY entry_lat, entry_lon, johab_name;
# → folium 마커 표시
```

---

## 6. 다음 단계

### 위판 데이터 매칭 (별도 작업)
- `fishing_voyage_landing` 테이블 생성 (FK: voyage.id)
- fish_landing_suhyub (1억건)에서 같은 johab_code + 시간 범위 매칭
- 위판 데이터 = 어종별 위판량(kg)
- **선박 식별자 부재**로 정확 매칭 불가 → 노력량(effort) 비례 분배 방식 사용 예정

```sql
CREATE TABLE fishing_voyage_landing (
    id BIGSERIAL PRIMARY KEY,
    voyage_id BIGINT REFERENCES fishing_voyage(id),
    landing_date DATE,
    wepan_code TEXT,
    wepan_name TEXT,
    fish_code TEXT,
    fish_name TEXT,
    weight_kg REAL,
    price_total INTEGER,
    salenum TEXT
);
```

### AI Analyst 프롬프트 업데이트 (예정)
- 새 컬럼 4개 안내 (entry_lat/lon, johab_name, entry_distance_km)
- 자연어 질의 가능: "부산 입항 항차 보여줘"

---

## 7. 코드 참조

- 메인 스크립트: [match_voyage_johab.py](../match_voyage_johab.py)
- DB 스키마: 171 marine.fishing_voyage
- 수협 좌표 소스: 21 fishery.fish_suhyub_location

## 8. 학습 사항

1. **연결 안정성**: 장시간(7시간+) 작업은 connection drop 대비 retry/reconnect 필수
2. **Idempotent 설계**: `WHERE x IS NULL` 필터로 중간 재시작 안전
3. **벡터화 vs 루프**: numpy haversine으로 9천만 거리 계산을 5분 내 완료
4. **임시 테이블 UPDATE**: 대량 UPDATE는 TEMP TABLE + JOIN이 개별 UPDATE보다 빠름
5. **수협 데이터 한계**: 56/139 수협만 좌표 보유 → 일부 입항 매칭 한계
