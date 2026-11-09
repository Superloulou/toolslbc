@echo off
rem Double-cliquer pour lancer l'interface web (http://127.0.0.1:8760).
cd /d "%~dp0"
echo === Interface Leboncoin (fermer cette fenetre pour arreter) ===
".venv\Scripts\lbc-alertes.exe" -v ui
echo.
echo Arret du programme.
pause
