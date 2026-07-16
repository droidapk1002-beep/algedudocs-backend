@echo off
title AlgEduDocs Server
echo ============================================
echo  AlgEduDocs — Algerian Education Documents
echo  DzExams / Eddirasa / Ency-Education
echo ============================================
echo.

REM === Installer les dépendances ===
echo [1/3] Verification des dependances...

pip install flask requests python-dotenv pycryptodome pypdf
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
pip install mega.py==1.0.6 --no-deps
pip install "tenacity>=8.0.0"
pip install dropbox
pip install msal
pip install playwright
python -m playwright install chromium 2>nul

echo.
echo [2/3] Demarrage du serveur...
echo.
echo  Acces: http://127.0.0.1:5000
echo  Arret:  Ctrl+C
echo.
echo ============================================

REM === Lancer le serveur ===
python app.py
pause
