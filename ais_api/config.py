# === DB 설정 ===
AIS_DB = {
    'host': '203.253.202.21',
    'port': 5432,
    'database': 'aisdb',
    'user': 'fishery_readonly_2',
    'password': 'readonly',
}

FISHERY_DB = {
    'host': '203.253.202.21',
    'port': 5432,
    'database': 'fishery',
    'user': 'fishery_readonly_2',
    'password': 'readonly',
}

LOCAL_DB = {
    'host': '203.253.202.171',
    'port': 5432,
    'database': 'marine',
    'user': 'postgres',
    'password': 'prhkddlf0420',
}

# 171 aisdb (충돌 near-miss). readonly 계정만 사용
NEARMISS_DB = {
    'host': '203.253.202.171',
    'port': 5432,
    'database': 'aisdb',
    'user': 'nearmiss_ro',
    'password': 'nearmiss_readonly_2026',
}

# === Marine DB 허용 테이블 (조회용 화이트리스트) ===
MARINE_TABLES = {
    'shipinfo': {'description': '모든 선박 메타정보 55,225척', 'max_limit': 5000},
    'fishing_shipinfo': {'description': '한국 어선 정보 3,264척', 'max_limit': 5000},
    'fishing_shipinfo_kcg': {'description': '해양경찰청 어선제원 60,905척', 'max_limit': 5000},
    'fishing_voyage': {'description': '어선 항차 + 입항/수협 (1.47M행)', 'max_limit': 100000},
    'fishing_landing_info': {'description': '대형트롤 일자×수협×어종 위판 (9,205행)', 'max_limit': 50000},
    'fishing_spatio_temporal': {'description': '어선 그리드별 effort+어종 (12.1M행)', 'max_limit': 100000},
    'kfw_ebp_voyage': {'description': '내부 항차 분석', 'max_limit': 50000},
}

# === Near-miss(충돌위험) 테이블 화이트리스트 (171 aisdb) ===
NEARMISS_TABLES = {
    'near_miss_db_namhae4':    {'description': '남해 near-miss 분석 데이터 (962K행, 2022-12~2024-01)', 'max_limit': 50000},
    'near_miss_db_namhae':     {'description': '남해 near-miss v1 (362행)', 'max_limit': 5000},
    'near_miss_db_namhae2':    {'description': '남해 near-miss v2 (1,995행)', 'max_limit': 5000},
    'near_miss_db_namhae3':    {'description': '남해 near-miss v3 (3행, 테스트)', 'max_limit': 5000},
    'near_miss_db_tongyeong':  {'description': '통영 near-miss v1 (285행)', 'max_limit': 5000},
    'near_miss_db_tongyeong2': {'description': '통영 near-miss v2 (1,268행)', 'max_limit': 5000},
    'realtime_near_miss_db':   {'description': '실시간 near-miss (4행, 테스트)', 'max_limit': 5000},
    'vhf_log_namhae':          {'description': '남해 VHF 통신 로그 (705K행, 2024)', 'max_limit': 50000},
}

# === API 설정 ===
API_HOST = '0.0.0.0'
API_PORT = 8005
MAX_ROWS = 1000000
DEFAULT_LIMIT = 100000

# === JWT 설정 ===
JWT_SECRET = 'jnu-marine-bigdata-api-secret-key-2026-change-this'
JWT_ALGORITHM = 'HS256'
ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 30

# === 기본 할당량 ===
DEFAULT_DAILY_REQUESTS = 1000
DEFAULT_DAILY_DATA_MB = 500
DEFAULT_MONTHLY_REQUESTS = 20000

# === Fishery 테이블 허용 목록 ===
FISHERY_TABLES = {
    'fish_code': {'description': '어종 코드', 'max_limit': 10000},
    'fish_cpue': {'description': 'CPUE 데이터', 'max_limit': 10000},
    'fish_group_reference': {'description': '어종 그룹 참조', 'max_limit': 10000},
    'fish_landing_suhyub': {'description': '수협 양륙(위판) 데이터', 'max_limit': 5000},
    'fish_spatio_temporal_db': {'description': '시공간 어업 DB', 'max_limit': 5000},
    'fish_spatio_temp_db_gillnet': {'description': '자망 시공간 DB', 'max_limit': 5000},
    'fish_spatio_temp_db_gillnet2': {'description': '자망 시공간 DB2', 'max_limit': 5000},
    'fish_spatio_temp_db_longline': {'description': '연승 시공간 DB', 'max_limit': 5000},
    'fish_spatio_temp_db_longline2': {'description': '연승 시공간 DB2', 'max_limit': 5000},
    'fish_spatio_temp_db_purse': {'description': '선망 시공간 DB', 'max_limit': 5000},
    'fish_spatio_temp_db_purse2': {'description': '선망 시공간 DB2', 'max_limit': 5000},
    'fish_spatio_temp_db_squidjig2': {'description': '오징어채낚기 시공간 DB', 'max_limit': 5000},
    'fish_suhyub_location': {'description': '수협 관할구역 위치', 'max_limit': 10000},
    'fish_type_reference2': {'description': '어업 유형 참조', 'max_limit': 10000},
    'fishing_activity': {'description': '조업 활동', 'max_limit': 10000},
    'fishing_effort': {'description': '어획노력량', 'max_limit': 5000},
    'fishing_labels': {'description': '어업 라벨', 'max_limit': 10000},
    'fishing_shipinfo': {'description': '어선 정보', 'max_limit': 10000},
    'fishing_shiptype_code': {'description': '선종 코드', 'max_limit': 10000},
    'fishing_shiptype_reference': {'description': '선종 참조', 'max_limit': 10000},
    'fishing_tac': {'description': 'TAC(총허용어획량)', 'max_limit': 10000},
    'fishing_tac_month': {'description': 'TAC 월별', 'max_limit': 10000},
    'fishing_trj_category_month': {'description': '항적 카테고리 월별', 'max_limit': 10000},
    'fishship_sensor': {'description': '어선 센서 데이터', 'max_limit': 5000},
    'fishship_sensor2': {'description': '어선 센서 데이터2', 'max_limit': 5000},
    'fishship_sensor3': {'description': '어선 센서 데이터3', 'max_limit': 5000},
    'fishship_sensor_info': {'description': '어선 센서 정보', 'max_limit': 10000},
    'haegu_location': {'description': '해구 위치', 'max_limit': 10000},
    'marine_env_ship_observation': {'description': '해양환경 선박 관측', 'max_limit': 10000},
    'port_ship_entrance_info': {'description': '항구 입출항 정보', 'max_limit': 10000},
    'purse_fishing_activity': {'description': '선망 조업 활동', 'max_limit': 10000},
    'purse_fishing_ship_list': {'description': '선망 어선 목록', 'max_limit': 10000},
    'seabed_info': {'description': '해저 정보', 'max_limit': 10000},
    'shipinfo_ner': {'description': '선박 정보 NER', 'max_limit': 10000},
    'suhyub_entrance_count': {'description': '수협 입항 횟수', 'max_limit': 10000},
    'transit_time': {'description': '통항 시간', 'max_limit': 5000},
    'zooplankton': {'description': '동물플랑크톤', 'max_limit': 10000},
}
