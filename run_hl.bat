@echo off
REM Hyperliquid copy-trader auto-restart launcher (double-clickable).
REM Runs the PAPER engine (no real money) and relaunches on exit/crash.
REM Close this window to stop.
cd /d "%~dp0"
echo Hyperliquid copy-trader (paper) — auto-restart. Close window to stop.
echo Logs appended to hl_copytrader.out.log
echo.
:loop
echo [%date% %time%] starting >> hl_copytrader.out.log
python -m copytrader hl-paper >> hl_copytrader.out.log 2>&1
echo [%date% %time%] exited, restarting in 10s >> hl_copytrader.out.log
timeout /t 10 /nobreak >nul
goto loop
