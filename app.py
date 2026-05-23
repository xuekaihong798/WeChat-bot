"""macOS 桌面 App — pywebview 原生窗口包装。

启动方式: python app.py
"""

import threading
import time

import webview


def main():
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
