"""macOS 桌面 App — pywebview 原生窗口包装。

启动方式: python app.py
"""

import os
import subprocess
import threading
import time

import webview


def _load_env():
    """从 ~/.zshrc 加载环境变量（GUI 应用不继承 shell 环境）。"""
    try:
        result = subprocess.run(
            ["zsh", "-c", "source ~/.zshrc 2>/dev/null && env"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "=" in line:
                key, _, val = line.partition("=")
                if key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


def main():
    _load_env()

    # 在 daemon 线程启动 HTTP Server
    from bot import run_server

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # 等待 server 就绪
    time.sleep(0.5)

    window = webview.create_window(
        "WeChat-bot",
        "http://localhost:8765",
        width=420,
        height=720,
        min_size=(360, 480),
        resizable=True,
    )
    webview.start()


if __name__ == "__main__":
    main()
