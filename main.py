#!/usr/bin/env python3
"""
TG 消息转发机器人 - 启动脚本

默认模式（推荐）：python main.py
热重载模式：python main.py --reload
指定端口：python main.py --port 8888
组合使用：python main.py --reload --port 8888
"""
import uvicorn

import argparse


def main():
    # 1. 创建参数解析器
    parser = argparse.ArgumentParser(description="TG 消息转发机器人启动脚本")

    # 2. 添加命令行参数
    parser.add_argument(
        "--reload",
        action="store_true",
        help="启用热重载模式（修改代码后自动重启）"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,  # 指定默认端口
        help="服务监听的端口号，默认值为 8000"
    )

    # 3. 解析命令行参数
    args = parser.parse_args()

    # 根据参数决定启动信息
    if args.reload:
        print("🚀 启动服务器（热重载模式）...")
        print(f"📡 正在监听端口: {args.port}")
        print("⚠️  注意：热重载模式下退出较慢，需要按 2-3 次 Ctrl+C")
    else:
        print("🚀 启动服务器...")
        print(f"📡 正在监听端口: {args.port}")
        print("💡 提示：按 Ctrl+C 一次即可退出")
        print("💡 提示：如需热重载，使用 python main.py --reload")

    # 4. 在 uvicorn.run 中使用解析到的端口
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=args.port,  # 使用动态获取的端口
        reload=args.reload,
        timeout_graceful_shutdown=10 if args.reload else 3,
        log_level="info",
    )


if __name__ == "__main__":
    main()