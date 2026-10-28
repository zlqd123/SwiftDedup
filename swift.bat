@echo off
REM SwiftDedup launcher (Windows, default shell profile) - live console output
REM NOTE: keep ASCII-only (cmd parses .bat in ANSI codepage; UTF-8 breaks it)

cd /d %~dp0
python main.py
pause
