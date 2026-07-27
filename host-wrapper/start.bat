@echo off
REM Run this on the HOST (not in Docker) — it needs the logged-in `claude` CLI
REM so Claude calls bill the subscription instead of API tokens.
pip install -r requirements.txt
python main.py
