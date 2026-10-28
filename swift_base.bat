@echo off
REM SwiftDedup launcher with HUB_PROFILE=base (full chromium, risk-control fallback)
REM NOTE: keep ASCII-only (cmd parses .bat in ANSI codepage; UTF-8 breaks it)

cd /d %~dp0
set HUB_PROFILE=base
python main.py
pause
