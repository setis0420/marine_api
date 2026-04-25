# Marine Database Setup

## PostgreSQL 서버 정보

| 항목 | 값 |
|------|-----|
| PostgreSQL 버전 | 17.9 |
| 호스트 | localhost (외부 접속 허용) |
| 포트 | 5432 |
| 사용자 | postgres |
| 비밀번호 | prhkddlf0420! |
| 데이터베이스 | marine |
| 데이터 경로 | K:/coding_project/해양수산 데이터 분석 플랫폼/pgdata |

## 접속 방법

```bash
PGPASSWORD='prhkddlf0420!' psql -U postgres -d marine -h localhost
```

## 설정 파일 위치

- `K:/coding_project/해양수산 데이터 분석 플랫폼/pgdata/postgresql.conf` - 서버 설정
- `K:/coding_project/해양수산 데이터 분석 플랫폼/pgdata/pg_hba.conf` - 인증 설정

## 외부 접속 설정

- `listen_addresses = '*'` (모든 IP 수신)
- `pg_hba.conf`에 `0.0.0.0/0` 및 `::/0` 허용 (md5 인증)
- Windows 방화벽 인바운드 규칙: `PostgreSQL 5432` (TCP 5432 허용)

## 서버 관리 명령어

```bash
# 시작
pg_ctl -D "K:/coding_project/해양수산 데이터 분석 플랫폼/pgdata" -l "K:/coding_project/해양수산 데이터 분석 플랫폼/pgdata/postgresql.log" start

# 중지
pg_ctl -D "K:/coding_project/해양수산 데이터 분석 플랫폼/pgdata" stop

# 재시작
pg_ctl -D "K:/coding_project/해양수산 데이터 분석 플랫폼/pgdata" restart

# 설정 리로드 (재시작 없이)
pg_ctl -D "K:/coding_project/해양수산 데이터 분석 플랫폼/pgdata" reload
```
