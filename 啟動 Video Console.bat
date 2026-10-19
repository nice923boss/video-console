@echo off
title 啟動 Video Console
cd /d "%~dp0"
set "VC_PY="
set "VC_PYW="
where py 1>nul 2>nul
if not errorlevel 1 (
  set "VC_PY=py -3"
  set "VC_PYW=pyw -3"
) else (
  where python 1>nul 2>nul
  if not errorlevel 1 (
    set "VC_PY=python"
    set "VC_PYW=pythonw"
  )
)
if not defined VC_PY goto nopython
%VC_PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 1>nul 2>nul
if errorlevel 1 goto oldpython
start "" %VC_PYW% "%~dp0app\main.py"
exit /b 0

:nopython
echo.
echo 找不到 Python。請先安裝 Python 3.10 以上版本，安裝時勾選 Add python.exe to PATH，再重新執行。
echo 下載網址：https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:oldpython
echo.
echo 目前的 Python 版本太舊，Video Console 需要 Python 3.10 以上：
%VC_PY% --version
echo.
pause
exit /b 1
