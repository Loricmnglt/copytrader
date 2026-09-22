@echo off
REM Hyperliquid REAL-TIME (WebSocket) copy-trader — paper, auto-restart.
REM Runs alongside run_hl.bat (separate book: hl_ws.sqlite). Close to stop.
cd /d "%~dp0"
echo Hyperliquid WebSocket copy-trader (paper, temps reel). Fermer pour arreter.
echo Logs -> hl_ws.out.log
echo.
:loop
echo [%date% %time%] starting >> hl_ws.out.log
python -m copytrader hl-ws-paper >> hl_ws.out.log 2>&1
echo [%date% %time%] exited, restarting in 10s >> hl_ws.out.log
timeout /t 10 /nobreak >nul
goto loop
