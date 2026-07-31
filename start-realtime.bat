@echo off
chcp 65001 >nul
title 实时重要资讯推送
cd /d %~dp0
echo ============================================
echo  实时重要资讯推送守护进程
echo  轮询间隔: 120 秒（可改 .ENV 的 RT_POLL_SECONDS）
echo  Ctrl+C 退出
echo ============================================
python scripts/real_time_push.py --loop
pause
