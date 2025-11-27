#!/usr/bin/env python3
"""
TG 消息转发机器人 - 启动脚本

默认模式（推荐）：python main.py
热重载模式：python main.py --reload （修改代码后自动重启，但退出较慢）
"""
import uvicorn
import sys

if __name__ == "__main__":
    # 检查是否开启热重载
    use_reload = "--reload" in sys.argv

    if use_reload:
        print("🚀 启动服务器（热重载模式）...")
        print("⚠️  注意：热重载模式下退出较慢，需要按 2-3 次 Ctrl+C")
        uvicorn.run(
            "app.main:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
            timeout_graceful_shutdown=10,
            log_level="info",
        )
    else:
        print("🚀 启动服务器...")
        print("💡 提示：按 Ctrl+C 一次即可退出")
        print("💡 提示：如需热重载，使用 python main.py --reload")
        uvicorn.run(
            "app.main:app",
            host="0.0.0.0",
            port=8000,
            reload=False,
            timeout_graceful_shutdown=3,
            log_level="info",
        )
