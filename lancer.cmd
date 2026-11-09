@echo off
rem Double-cliquer pour lancer la veille Leboncoin dans une fenetre visible.
cd /d "%~dp0"
echo === Veille Leboncoin (Ctrl+C pour arreter) ===
".venv\Scripts\lbc-alertes.exe" -v run
echo.
echo Arret du programme.
pause
