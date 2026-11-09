@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py
if "%PY%"=="" where python >nul 2>nul && set PY=python
if "%PY%"=="" (
  echo.
  echo Python이 설치되어 있지 않습니다. https://www.python.org/downloads/ 에서 설치하세요.
  echo 설치 첫 화면의 "Add python.exe to PATH" 를 꼭 체크하세요.
  echo.
  pause
  exit /b
)
%PY% run_pc.py
echo.
pause
