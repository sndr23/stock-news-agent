# filepath: launcher.py
"""
A股资讯监测 Agent 一键启动器
自动在后台拉起 FastAPI 服务，等待服务就绪后自动拉起默认浏览器，并支持退出时优雅关闭后台进程
"""
import subprocess
import webbrowser
import time
import socket
import sys

# 依赖完整性预检
try:
    import uvicorn
    import fastapi
    import langgraph
    import akshare
    import pandas
    import requests
    import dotenv
except ImportError as e:
    print(f"\n[错误] 缺少项目运行依赖: {e}")
    print("请先安装项目依赖。您可以在当前目录下的命令行终端运行以下指令:")
    print("pip install -r requirements.txt\n")
    sys.exit(1)

def is_port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def main():
    port = 8000
    print(f"正在启动 A股资讯监测 Agent 后端服务 (端口: {port})...")
    
    # 启动 uvicorn 进程
    # 使用 sys.executable 确保使用相同的 python 解释器环境
    cmd = [sys.executable, "-m", "uvicorn", "src.api.main:app", "--host", "127.0.0.1", "--port", str(port)]
    process = subprocess.Popen(cmd)
    
    # 等待端口开放
    print("正在等待服务响应，即将自动打开浏览器...")
    max_retries = 30
    for _ in range(max_retries):
        if is_port_open(port):
            print("服务已就绪！正在拉起浏览器页面...")
            webbrowser.open(f"http://127.0.0.1:{port}")
            break
        time.sleep(0.5)
    else:
        print("警告: 等待服务启动超时。请尝试手动刷新浏览器页面：http://127.0.0.1:8000")
        webbrowser.open(f"http://127.0.0.1:{port}")

    try:
        # 持续等待进程结束
        process.wait()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        process.terminate()
        process.wait()
        print("服务已关闭。")

if __name__ == "__main__":
    main()
