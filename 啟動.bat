@echo off
chcp 65001 >nul
title 化材系畢業學分檢核系統

if not exist .venv (
    echo [錯誤] 尚未安裝，請先點兩下「安裝.bat」。
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo 系統啟動中，瀏覽器會自動開啟 http://127.0.0.1:8000
echo 若要關閉系統，直接關掉這個黑色視窗即可（或按 Ctrl+C）。
echo.

start "" cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:8000/"

python -m uvicorn main:app --host 127.0.0.1 --port 8000

pause
