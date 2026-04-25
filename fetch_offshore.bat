@echo off
REM === fetch_offshore 실행 (어디서든 가능) ===
REM 사용법:
REM   fetch_offshore.bat                                  - 전체 1,368척
REM   fetch_offshore.bat --fishing-type 대형트롤어업      - 특정 업종
REM   fetch_offshore.bat --mmsi 440137010                 - 단일 선박
REM   fetch_offshore.bat --workers 7                      - 워커 수 지정

setlocal
set PYTHON=C:\Python313\python.exe
set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%fetch_offshore.py

if not exist "%PYTHON%" (
    echo [ERROR] Python not found: %PYTHON%
    exit /b 1
)
if not exist "%SCRIPT%" (
    echo [ERROR] Script not found: %SCRIPT%
    exit /b 1
)

"%PYTHON%" "%SCRIPT%" %*
