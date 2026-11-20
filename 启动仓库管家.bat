@echo off
chcp 65001 >nul
title 仓库管家 v2
echo 正在启动仓库管家...
"C:\Users\38237\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "%~dp0app.py"
pause
