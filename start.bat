@echo off
rem サブPC用起動スクリプト（__pycache__ を作らない -B 付きで起動）
cd /d %~dp0
python -B main.py %*
pause
