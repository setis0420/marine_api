# 해양수산 데이터 분석 플랫폼

제주대학교 해양수산 빅데이터 연구실의 AIS 항적 · 어선 조업 분류 · API 플랫폼.

## 구성

### 1. AIS 항적 처리 (`fishing_model/`)
- `fetch_all_ships.py` — 21번 AIS DB → 171번 marine DB의 ship_{mmsi}로 fetch
- `process_voyage_model.py` — 항차 분리 + 조업 분류 (CNN-LSTM 2단계)
- `rule_based_classify.py` — 자망/통발 등 미학습 업종은 룰 기반 분류
- `draw_all_voyages.py` — 항차별 지도 PNG 생성 (folium + Selenium)

### 2. 근해 어업 확장 (루트)
- `fetch_offshore.py` — 9개 근해 업종 1,368척 AIS fetch + voyage_num + fishing_voyage 적재
- `fetch_offshore.bat` — Windows 어디서든 실행 가능한 배치 래퍼
- `add_ship_pk_parallel.py` — ship_{mmsi} 모든 테이블에 datetime PK 추가 (병렬)
- `offshore_target.csv` — 근해 9개 업종 대상 선박 리스트

### 3. API 서버 (`ais_api/`)
FastAPI 기반 REST API. 회원가입/API Key/사용량 제한 포함.

- `main.py` — 진입점 (uvicorn 실행)
- `routers/`
  - `ais.py` — 항적 조회
  - `ships.py` — 어선/수협 조회
  - `fishery.py` — 수산 통계 37개 테이블
  - `auth.py` — 로그인/회원가입
  - `admin.py` — 관리자 (사용자/할당량/로그)
  - `analyst.py` — **AI Analyst** (Claude Haiku 4.5 기반 자연어 SQL)
  - `schema.py` — DB 스키마 인터랙티브 탐색 페이지
- `services/` — 비즈니스 로직
- `templates/` — 프런트엔드 (홈/관리자/분석가/스키마 페이지)

#### 주요 페이지
- `/` — 홈 (로그인 + 회원가입)
- `/admin` — 관리자 (사용자/AI권한/할당량/로그/비용)
- `/schema` — DB 스키마 인터랙티브 탐색
- `/analyst` — AI Analyst (자연어 → SQL → 표/지도/차트 + 다운로드)
- `/docs` — Swagger API 문서

## DB 구성

| 서버 | DB | 용도 |
|---|---|---|
| 203.253.202.21 | aisdb | AIS 월별 항적 `dbYYYYMM` (2012-01 ~ 2024-12) |
| 203.253.202.21 | fishery | 수산 통계 37개 테이블 |
| 203.253.202.171 | marine | `ship_{mmsi}` 개별 항적 + `shipinfo` 통합 메타 + `fishing_voyage` 항차 |

## 실행

### API 서버
```bash
cd ais_api
pip install -r requirements.txt
python main.py  # 포트 8005
```

### 항적 fetch (근해)
```bash
cd "K:\coding_project\해양수산 데이터 분석 플랫폼"
fetch_offshore.bat --fishing-type 대형트롤어업 --workers 7
fetch_offshore.bat                                       # 1,368척 전체
```

## 환경

- Python 3.13 (Windows)
- PostgreSQL 12.5
- TensorFlow 2.10 (CUDA 11.8) — 조업 분류 모델
- Anthropic SDK — AI Analyst
- folium · netCDF4 · numpy · psycopg2 · FastAPI · Selenium

## 보안 주의

- **DB 비밀번호가 코드에 하드코딩**돼 있음 → public repo 부적합. private 사용 권장.
- `ais_api/.env`에 Anthropic API 키 보관 (gitignore 적용됨)
- 운영 시 환경변수 분리 권장

## 라이선스

내부 연구용. 외부 배포 전 별도 협의.
