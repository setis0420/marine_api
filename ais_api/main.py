"""
제주대학교 해양수산빅데이터 API

실행:
    pip install fastapi uvicorn psycopg2-binary jinja2 pyjwt passlib[bcrypt] python-multipart
    python main.py
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn
import os

from routers import ais, ships, auth, admin, fishery, schema, analyst
from services.auth_service import init_db
from config import API_HOST, API_PORT

app = FastAPI(
    title="제주대학교 해양수산빅데이터 API",
    description="연구자 및 학생을 위한 AIS 항적, 수산 데이터 API\n\n"
                "회원가입 후 API Key를 발급받아 사용하세요.\n\n"
                "문의: 제주대학교 해양수산빅데이터 연구실",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# DB 초기화 (사용자 테이블)
try:
    init_db()
except Exception as e:
    print(f"DB 초기화 경고: {e}")

# 라우터 등록
app.include_router(auth.router)
app.include_router(ais.router)
app.include_router(ships.router)
app.include_router(fishery.router)
app.include_router(admin.router)
app.include_router(schema.router)
app.include_router(analyst.router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def homepage():
    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'index.html')
    with open(template_path, 'r', encoding='utf-8') as f:
        return f.read()


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page():
    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'admin.html')
    with open(template_path, 'r', encoding='utf-8') as f:
        return f.read()


@app.get("/schema", response_class=HTMLResponse, include_in_schema=False)
def schema_page():
    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'schema.html')
    with open(template_path, 'r', encoding='utf-8') as f:
        return f.read()


@app.get("/analyst", response_class=HTMLResponse, include_in_schema=False)
def analyst_page():
    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'analyst.html')
    with open(template_path, 'r', encoding='utf-8') as f:
        return f.read()


@app.get("/health", tags=["시스템"], summary="서버 상태")
def health():
    """API 서버 및 DB 연결 상태 확인"""
    import psycopg2
    from config import AIS_DB, LOCAL_DB, FISHERY_DB
    status = {"api": "ok"}

    for name, db in [("ais_db", AIS_DB), ("local_db", LOCAL_DB), ("fishery_db", FISHERY_DB)]:
        try:
            if name == "local_db":
                conn = psycopg2.connect(host=db['host'], dbname=db['database'],
                                        user=db['user'], password=db['password'], port=db['port'])
            else:
                conn = psycopg2.connect(**db)
            conn.close()
            status[name] = "ok"
        except Exception as e:
            status[name] = f"error: {str(e)[:50]}"

    return status


if __name__ == '__main__':
    uvicorn.run("main:app", host=API_HOST, port=API_PORT, reload=True)
