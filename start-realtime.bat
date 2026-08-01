@echo off
chcp 65001 >nul
title 实时重要资讯推送
cd /d %~dp0
echo ============================================
echo  实时重要资讯推送守护进程
echo  轮询间隔: 120 秒（可改 .ENV 的 RT_POLL_SECONDS）
echo  Ctrl+C 退出
echo ============================================
echo.
echo  [注意] 若云端 GitHub Actions 定时任务也在运行，建议只保留一端，
echo         双端同跑浪费 LLM 额度且可能偶发重复推送。
echo.
python scripts/real_time_push.py --loop
pause
