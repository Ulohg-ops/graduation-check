@echo off
chcp 65001 >nul
title 化材系畢業學分檢核系統 - 安裝

echo ===============================
echo   化材系畢業學分檢核系統 - 安裝
echo ===============================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到 Python。
    echo 請先到 https://www.python.org/downloads/ 下載安裝，
    echo 安裝時務必勾選「Add python.exe to PATH」，裝完後再重新點兩下這個檔案。
    pause
    exit /b 1
)

echo 正在建立虛擬環境...
python -m venv .venv
if errorlevel 1 (
    echo [錯誤] 建立虛擬環境失敗。
    pause
    exit /b 1
)

echo 正在安裝套件（第一次會花一點時間，請耐心等候，需要網路連線）...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 (
    echo [錯誤] 套件安裝失敗，請確認網路連線正常後再試一次。
    pause
    exit /b 1
)

echo.
echo 安裝完成！之後要使用系統，請點兩下「啟動.bat」。
pause
