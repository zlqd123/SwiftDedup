@echo off
REM SwiftDedup 以 HUB_PROFILE=base 完整 chromium 内核启动（风控对照/回退用）
cd /d %~dp0
set HUB_PROFILE=base
python main.py
