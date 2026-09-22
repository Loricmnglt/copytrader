@echo off
REM copytrader auto-restart launcher (double-clickable).
REM Relaunches the bot if it exits/crashes. Close this window to stop.
REM Optional argument = subcommand (default: paper).  e.g.  run.bat paper
cd /d "%~dp0"
set CMD=%1
if "%CMD%"=="" set CMD=paper
echo copytrader launcher: python -m copytrader %CMD%  (auto-restart)
echo Logs appended to copytrader.out.log
echo Close this window to stop.
echo.
:loop
echo [%date% %time%] starting >> copytrader.out.log
python -m copytrader %CMD% >> copytrader.out.log 2>&1
echo [%date% %time%] exited, restarting in 10s >> copytrader.out.log
timeout /t 10 /nobreak >nul
goto loop
