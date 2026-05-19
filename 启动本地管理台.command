#!/bin/zsh
cd "/Users/benjaminzhou/Documents/工厂管报" || exit 1
export PYTHONPATH="/Users/benjaminzhou/Documents/工厂管报"
echo "工厂管报本地管理台启动中..."
echo "访问地址：http://127.0.0.1:5050"
echo "关闭这个窗口会停止管理台。"
.venv/bin/python scripts/run_web.py
