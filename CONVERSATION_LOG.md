# 해양수산 데이터 분석 플랫폼 작업 로그

**기간**: 2026-04-22 ~ 2026-05-02
**저장소**: https://github.com/setis0420/marine_api

---

## 1. KFW/EBP 항적 이미지 누락 처리

### 문제
- `H:\Dropbox\파일전송\어업피해조사\결과이미지\` 에 451척 누락
- `draw_all_voyages.py`가 PNG 캡처 실패 시 `except: pass`로 silent 종료 → 폴더만 만들고 PNG 없음

### 해결
- `MISSING_MMSI` 451척 하드코딩 + `--xlsx`/`--start`/`--end` 옵션 추가
- **Selenium 드라이버 재사용**으로 Chrome 프로세스 폭증 (5,000개) 해결
  - `_thread_local.driver` 패턴 + `atexit` 정리
  - 워커당 Chrome 1개로 제한
- `check_png_exists()` skip 로직으로 중복 작업 방지

### 결과
- 451척 PNG 정상 생성

### 추가 처리
- KFW/EBP 대상 1,006척 중 1척(`440402650`, 제109행복호) 누락 발견 → 수동 추가

---

## 2. fishing_shipinfo 정리

### 171 marine vs 21 fishery 비교
- 21 fishery: 8,698척 (원본)
- 171 marine: 8,698척 → **fishing_type='None' 5,434척 삭제** → 3,264척

### 삭제 작업
- `fishing_type='None'` 5,434척 DROP + 그들의 `ship_{mmsi}` 테이블 181개 DROP
- 백업: `fishing_shipinfo_backup_none_20260422`

### 검증
- 21번과 171번 어선 데이터 100% 일치 (3,264척 모두)

---

## 3. AIS API 서버 (FastAPI) 구축

### 구조
```
ais_api/
├── main.py              # 진입점 (포트 8006)
├── config.py            # DB / API / JWT 설정
├── routers/
│   ├── auth.py          # 회원가입/로그인 (JWT)
│   ├── ais.py           # AIS 항적 (X-Api-Key)
│   ├── ships.py         # 선박 (X-Api-Key) ★보안 추가
│   ├── fishery.py       # 수산 통계 37개 테이블
│   ├── admin.py         # 관리자 (JWT)
│   ├── analyst.py       # AI Analyst (JWT)
│   └── schema.py        # DB 탐색 (X-Api-Key/JWT) ★보안 추가
├── services/            # 비즈니스 로직
└── templates/           # HTML 페이지
```

### 페이지
- `/` 홈 (로그인)
- `/admin` 관리자
- `/schema` DB 스키마 탐색 ★ 인터랙티브
- `/analyst` AI Analyst ★ Claude Haiku 4.5
- `/docs` Swagger

### 관리자 기능
- 사용자 목록 / 역할 / 활성화 / API Key 재발급
- AI 권한 토글, AI 일일 한도 개별 설정 (기본 10회/일, admin 무제한)
- 사용자별 API 호출 로그 (api_usage_log)
- AI 사용량 + 비용 표시 (USD/KRW 환산)

### admin 계정
- username: `admin`
- password: `admin`

---

## 4. AI Analyst (Claude Haiku 4.5)

### 기능
- 자연어 질문 → SQL 자동 생성 → 실행 → 결과 + 차트 + 지도 시각화
- 멀티턴 대화 (최근 6턴)
- 스키마 컨텍스트 prompt caching (5분 TTL)
- 차트 4종 (bar/line/pie/scatter) + **folium 지도** 자동 생성

### 다운로드
- CSV / HTML / 차트PNG / 지도HTML

### 보안
- SELECT/WITH만 허용
- INSERT/UPDATE/DELETE/DROP/ALTER 차단
- pg_*, information_schema 차단
- LIMIT 자동 10,000
- statement_timeout 30초

### SSE 스트리밍
- `/api/v1/analyst/ask/stream`
- 이벤트: status, thinking, query_start, query_result, query_error(숨김), done

### 비용 (Haiku 4.5)
- 입력 $1/M, 캐시쓰기 $1.25/M, 캐시읽기 $0.10/M, 출력 $5/M
- 환율 ₩1,400/$
- 평균 ₩10~30 / 질문

### 프롬프트 가이드
- 항적은 무조건 `chart.type='map'`
- AIS `sog=1023`은 N/A → `sog < 1023` 필터 필수
- 속도 있는 구간: `sog BETWEEN 50 AND 500` (5~50 노트)
- KFW/EBP 같은 내부 프로젝트명은 답변에 노출 금지

---

## 5. DB 단일화 작업 (171 marine)

### 통합 메타 테이블: shipinfo
- 21 aisdb.shipinfo (55,225척) → 171 marine.shipinfo로 복사
- 31컬럼: mmsi, shipname_kr, shiptype_portmis, gross_ton, fishing_type 등
- 인덱스: mmsi, shiptype_portmis, fishing_type

### 정리
- shipinfo_ner (음성인식용) 171에서 DROP
- kfw_ebp_trjdata DROP (368GB 회수)

### 비어선 ship_{mmsi} 정리
- 824개 ship_ 중 명확한 비어선 53개 DROP
- 자동차운반선, 화물선, 관공선 등
- 백업: `ship_drop_log_20260425`

---

## 6. 근해 9개 업종 ship_{mmsi} 확장

### 대상
| 업종 | 척수 | 신규 |
|---|---:|---:|
| 근해안강망어업 | 180 | 180 |
| 근해연승어업 | 204 | 180 |
| 근해자망어업 | 268 | 248 |
| 근해채낚기어업 | 135 | 41 |
| 근해통발어업 | 95 | 75 |
| 대형선망어업 | 155 | 25 |
| 기선권현망어업 | 254 | 253 |
| 대형트롤어업 | 34 | 34 |
| 외끌이대형저인망어업 | 43 | 43 |
| **합계** | **1,368** | **1,079** |

### 스키마 통일
모든 ship_{mmsi}에서 `mmsi`, `rot`, `heading` 컬럼 DROP
+ `voyage_num` 컬럼 ADD
+ `datetime` PRIMARY KEY 추가 (병렬 7워커, 약 1시간)

```sql
CREATE TABLE ship_{mmsi} (
    datetime TIMESTAMP PRIMARY KEY,
    sog SMALLINT, lon INTEGER, lat INTEGER, cog SMALLINT,
    model_fishing_type SMALLINT, model_fishing_status SMALLINT,
    port_entering BOOLEAN, voyage_num INTEGER
);
```

### fetch_offshore.py
- 21번 dbYYYYMM (2012-01 ~ 2024-12) → 171 ship_{mmsi}
- **C+A 중복 방지**:
  - C: 기존 보유 월 스킵 (네트워크 절감)
  - A: ON CONFLICT (datetime) DO NOTHING
- port_entering 자동 계산 (수심 -10m 기준)
- voyage_num 부여 (YY×1000+순번, 30분 입항 지속)
- fishing_voyage 테이블에 항차 메타 자동 INSERT
- 7 워커 멀티프로세스

### fishing_voyage 테이블
- kfw_ebp_voyage 구조 복제 + 1,103,250행 복사
- PK: (mmsi, voyage_num, target_area)

### 진행 상황 (2026-05-02 기준)
| 업종 | 진행 |
|---|---|
| 대형트롤어업 | ✅ 완료 |
| 대형선망어업 | ⏳ 거의 (129/155) |
| 근해자망어업 | ⏳ 진행 중 (186/268) |
| 근해안강망어업 | ⏳ 진행 중 (92/180) |
| 근해채낚기어업 | ⏳ 진행 중 (94/135) |
| 근해통발어업 | ⏳ 진행 중 (20/95) |
| 기선권현망어업 | ⏳ 진행 중 (1/254) |
| 근해연승어업 | ❌ 미명령 (24/204) |
| 외끌이대형저인망어업 | ❌ 미명령 (0/43) |

### 실행 명령
```cmd
cd /d "K:\coding_project\해양수산 데이터 분석 플랫폼"
fetch_offshore.bat --fishing-type "대형선망어업,근해채낚기어업" --workers 7
```

---

## 7. 해양경찰청 어선제원 import

### 입력
- 파일: `해양경찰청 보유 선박제원(3.31.)_정리본.xlsx`
- 60,905척 × 38컬럼

### 결과: marine.fishing_shipinfo_kcg
- 영어 컬럼명 + 한국어 코멘트
- 인덱스: mmsi, registration_no, radio_ais_mmsi, radio_vhf_mmsi, fishing_type, shipname
- 28 MB

### 매핑 검증
| 등록번호 | 선명 | MMSI | 업종 |
|---|---|---:|---|
| 16060026501309 | **부국호** | 440135440 | 연안복합어업 |

→ 사용자가 찾던 어선번호 정상 매칭

---

## 8. CSV 추출 도구

### fetch_to_csv.py
21번 aisdb에서 특정 MMSI 항적을 CSV로 추출 (DB 적재 X)

```bash
# 단일 파일
python fetch_to_csv.py --mmsi "440181090,440702840"

# 년월별 분리
python fetch_to_csv.py --mmsi "440181090" --by-month
# → ship_440181090/ship_440181090_YYYYMM.csv (월당 1파일)
```

### 컬럼
`datetime, mmsi, lat, lon, sog_knots, cog_deg, heading_deg, nav_status`
- lat/lon, sog 모두 변환된 실제 단위로 저장

### 결과 (440181090 + 440702840)
- 20M행, 1.35GB
- 년월별 분리: 123개 파일 (4분 소요)

---

## 9. 보안 강화 (2026-04-30)

### 인증 추가
- `/api/v1/ships/*` → X-Api-Key 필수
- `/api/schema/*` → X-Api-Key 또는 JWT 필수
- `/schema` 페이지 → 진입 시 로그인 모달

### 포트 변경
- 8005 → **8006** (phantom listener 회피)

---

## 10. GitHub 관리

### 저장소
https://github.com/setis0420/marine_api (Private)

### .gitignore
- pgdata/, mmsi_results/, 수심/ (대용량)
- *.csv, *.html, *.png, *.zip
- logs/, *.log
- .env, __pycache__/
- 예외: `!offshore_target.csv` (대상 리스트 포함)

### offshore_target.csv 자동 생성
스크립트가 CSV 없으면 marine.shipinfo에서 자동 추출

---

## 11. 환경 정보

### 서버
| 서버 | DB | 용도 |
|---|---|---|
| 203.253.202.21 | aisdb | AIS 월별 `dbYYYYMM` (2012-01~2024-12), 보조 shipinfo |
| 203.253.202.21 | fishery | 수산 통계 37개 테이블, shipinfo_ner |
| **203.253.202.171** | **marine** | **메인** — ship_{mmsi}, shipinfo, fishing_voyage, fishing_shipinfo_kcg, kfw_ebp_*, api_users 등 |

### 비밀번호 주의
- `prhkddlf0420` (171 postgres) 여러 .py에 하드코딩됨
- Anthropic API 키는 `ais_api/.env` (gitignore)
- repo 항상 **Private 유지**

---

## 12. 주요 스크립트 목록

| 파일 | 용도 |
|---|---|
| `fetch_offshore.py` + `.bat` | 근해 9업종 fetch (--fishing-type 콤마 다중) |
| `fetch_to_csv.py` | AIS → CSV 추출 (--by-month 옵션) |
| `import_kcg.py` | 해양경찰청 엑셀 → DB |
| `add_ship_pk_parallel.py` | ship_{mmsi} PK 일괄 추가 |
| `fishing_model/draw_all_voyages.py` | 항차 PNG 생성 (Selenium) |
| `fishing_model/process_voyage_model.py` | 조업 분류 (CNN-LSTM) |
| `ais_api/main.py` | API 서버 |

### Anthropic 키
공개 채팅에 노출됐으므로 폐기 후 재발급 필요 → `ais_api/.env`의 `ANTHROPIC_API_KEY=` 갱신

---

## 13. 다음 작업 (TODO)

- [ ] 근해연승어업 (204척) fetch 명령
- [ ] 외끌이대형저인망어업 (43척) fetch 명령
- [ ] 진행 중 업종들 완료 후 voyage 처리 확인
- [ ] 9개 업종 모두 끝나면 조업 분류 (model_1.0 / rule-based)
- [ ] 근해자망/근해통발/외끌이저인망 → rule-based 적용 검토
- [ ] DB 비밀번호 환경변수 분리 (보안)
- [ ] 회원가입 시 중복 ID 차단 (이미 register_user에 UniqueViolation 처리 있음, 메시지 강화)
