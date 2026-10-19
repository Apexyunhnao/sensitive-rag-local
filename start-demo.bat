@echo off
REM sensitive-rag-demo local services
REM :8090 = permission gateway, :8092 = demo app
REM Use the project venv: it is the only interpreter with uvicorn/fastapi/pdfplumber.
cd /d %~dp0
if not exist logs mkdir logs
start "gateway-8090" /MIN cmd /c ".venv\Scripts\python.exe scripts\gateway.py > logs\gateway-8090.log 2>&1"
start "app-8092" /MIN cmd /c ".venv\Scripts\python.exe scripts\app.py > logs\app-8092.log 2>&1"
exit /b 0
