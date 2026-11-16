@echo off
chcp 65001 >nul
setlocal
rem RecSort をブラウザで開く（ダブルクリックで起動）。
rem 初回だけ、このフォルダに .venv（Python の仮想環境）を作り、必要な部品を入れる（インターネット接続が必要）。
rem サーバーが応答できるようになってから、既定のブラウザで http://127.0.0.1:8765 を開く。
rem すでに起動していれば、起動中の RecSort の画面を開くだけにする。
cd /d "%~dp0"
title RecSort

if exist ".venv\Scripts\python.exe" goto run

echo 初回のセットアップをしています（.venv を作成し、必要な部品を入れます）...
set "PYBOOT="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1 && set "PYBOOT=py -3"
if not defined PYBOOT (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1 && set "PYBOOT=python"
)
if not defined PYBOOT (
    echo.
    echo Python 3.12 以上が見つかりません。
    echo https://www.python.org/downloads/ から Python をインストールしてから、もう一度ダブルクリックしてください。
    echo （インストール時に「Add python.exe to PATH」にチェックを入れてください）
    pause
    exit /b 1
)
%PYBOOT% -m venv .venv
if errorlevel 1 (
    echo .venv を作成できませんでした。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-web.txt
if errorlevel 1 (
    echo.
    echo 必要な部品をインストールできませんでした。インターネット接続を確認して、もう一度ダブルクリックしてください。
    rmdir /s /q ".venv" >nul 2>&1
    pause
    exit /b 1
)
echo セットアップが終わりました。

:run
".venv\Scripts\python.exe" main.py %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo RecSort を起動できませんでした（上のメッセージを確認してください）。
    pause
)
exit /b %CODE%
