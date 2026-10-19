@echo off
title 安裝相依套件
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
echo 安裝 imageio-ffmpeg（內附 ffmpeg，用來輸出影片、擷取畫面與產生縮圖）。
echo 已經裝過的套件會略過，不會強制升級。
echo.
%VC_PY% -m pip install imageio-ffmpeg
if errorlevel 1 goto failed
echo.
echo 安裝完成。可以執行「啟動 Video Console.bat」。
echo.
pause
exit /b 0

:failed
echo.
echo 安裝沒有完成。請確認網路連線，或把上面的訊息截圖保存以便排查。
echo.
pause
exit /b 1

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
