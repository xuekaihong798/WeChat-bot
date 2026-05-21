#!/usr/bin/env python3
"""
本地 Claude/Codex 智能体。

模式:
  python bot.py            → 微信 ClawBot 模式（扫码登录）
  python bot.py --server   → HTTP Server 模式（浏览器 http://localhost:8765）
"""

import sys
import time
import threading
import logging
import signal

from config import load_config
from backend import create_backend

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")

config = load_config()
backend = create_backend(config)

conversations: dict[str, list[dict]] = {}
conv_lock = threading.Lock()

MAX_HISTORY = config["max_history"]
SYSTEM_PROMPT = config["system_prompt"]
RESET_KEY = config["reset_key"]


# ── 对话管理 ────────────────────────────────────────

def get_history(user_id: str) -> list[dict]:
    with conv_lock:
        return list(conversations.get(user_id, []))


def save_turn(user_id: str, user_msg: str, assistant_msg: str):
    with conv_lock:
        hist = conversations.get(user_id, [])
        hist.append({"role": "user", "content": user_msg})
        hist.append({"role": "assistant", "content": assistant_msg})
        max_msgs = MAX_HISTORY * 2
        if len(hist) > max_msgs:
            hist = hist[-max_msgs:]
        conversations[user_id] = hist


def reset_user(user_id: str):
    with conv_lock:
        conversations.pop(user_id, None)


# ── AI 调用 ─────────────────────────────────────────

def chat(user_id: str, text: str) -> str:
    text = text.strip()

    if text.lower() == RESET_KEY.lower():
        reset_user(user_id)
        return "✅ 上下文已重置"

    if text.startswith("/code "):
        text = f"请只输出代码，不要解释：\n{text[6:]}"
    elif text.startswith("/ask "):
        text = text[5:]

    history = get_history(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-MAX_HISTORY * 2:])
    messages.append({"role": "user", "content": text})

    logger.info("📩 %s", text[:100])

    start = time.time()
    try:
        reply = backend.chat(messages)
    except Exception as e:
        logger.error("AI 失败: %s", e)
        reply = "抱歉，AI 服务调用失败。"

    elapsed = time.time() - start
    logger.info("📤 (%.1fs) %s", elapsed, reply[:100])

    save_turn(user_id, text, reply)
    return reply


# ── 模式 1: 微信 ClawBot ─────────────────────────────

def run_clawbot():
    from clawbot import ClawBot

    bot = ClawBot(ai_chat_fn=chat)

    if not bot.login():
        print("登录失败，请重试。")
        sys.exit(1)

    def shutdown(sig, frame):
        print("\n退出中...")
        bot.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("\n✅ 已就绪，等待微信消息... (Ctrl+C 退出)\n")
    bot.poll_loop()


# ── 模式 2: HTTP Server ─────────────────────────────

def run_server():
    import json
    from http.server import HTTPServer, BaseHTTPRequestHandler

    HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude/Codex 智能体</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#1a1a2e;color:#eee;height:100vh;display:flex;flex-direction:column}
header{background:#16213e;padding:12px 20px;font-size:16px;font-weight:600;border-bottom:1px solid #0f3460;display:flex;align-items:center;gap:8px}
#chat{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:80%;padding:10px 14px;border-radius:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word;font-size:14px}
.msg.user{align-self:flex-end;background:#0f3460}
.msg.assistant{align-self:flex-start;background:#533483}
.msg.system{align-self:center;background:transparent;color:#888;font-size:12px;padding:4px 8px}
#input-area{display:flex;padding:12px 16px;gap:8px;border-top:1px solid #0f3460;background:#16213e}
#input{flex:1;padding:10px 14px;border-radius:8px;border:1px solid #0f3460;background:#1a1a2e;color:#eee;font-size:14px;outline:none;resize:none;min-height:40px;max-height:120px}
#input:focus{border-color:#533483}
#send{padding:10px 20px;border:none;border-radius:8px;background:#533483;color:#fff;font-size:14px;cursor:pointer;font-weight:600}
#send:hover{background:#6b44a0}#send:disabled{opacity:.5;cursor:not-allowed}
#status{font-size:11px;color:#888;padding:4px 16px 8px}
</style>
</head>
<body>
<header>&#129302; 本地 Claude/Codex 智能体</header>
<div id="chat"><div class="msg system">发送消息开始对话 · /code 需求 · reset 重置</div></div>
<div id="input-area">
  <textarea id="input" placeholder="输入消息..." rows="1" autofocus></textarea>
  <button id="send" onclick="send()">发送</button>
</div>
<div id="status">AI 后端: {backend}</div>
<script>
const c=document.getElementById('chat'),i=document.getElementById('input'),b=document.getElementById('send'),uid='web_'+Date.now();
function add(role,t){const d=document.createElement('div');d.className='msg '+role;d.textContent=t;c.appendChild(d);c.scrollTop=c.scrollHeight}
function send(){
  const t=i.value.trim();if(!t)return;
  add('user',t);i.value='';b.disabled=true;
  fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({{message:t,user_id:uid}})})
    .then(r=>r.json()).then(d=>{add('assistant',d.reply||d.error)})
    .catch(e=>add('system','请求失败: '+e.message)).finally(()=>{b.disabled=false;i.focus()});
}
i.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
</script>
</body>
</html>""".replace("{backend}", config["backend_type"])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._html(HTML)
            elif self.path == "/health":
                self._json({"status": "ok", "backend": config["backend_type"]})
            else:
                self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            if self.path == "/api/chat":
                msg = body.get("message", "").strip()
                uid = body.get("user_id", "default")
                if not msg:
                    return self._json({"error": "message 不能为空"}, 400)
                start = time.time()
                reply = chat(uid, msg)
                elapsed = time.time() - start
                self._json({"reply": reply, "elapsed": round(elapsed, 2)})
            elif self.path == "/api/reset":
                reset_user(body.get("user_id", "default"))
                self._json({"status": "ok"})
            else:
                self.send_error(404)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _html(self, c, s=200):
            self.send_response(s); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(c.encode())

        def _json(self, d, s=200):
            self.send_response(s); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
            self.wfile.write(json.dumps(d, ensure_ascii=False).encode())

        def log_message(self, f, *a):
            logger.debug("%s %s", self.address_string(), f % a)

    host, port = "0.0.0.0", 8765
    server = HTTPServer((host, port), Handler)
    def shutdown(sig, frame):
        print("\n退出中..."); server.shutdown(); sys.exit(0)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"""
╔══════════════════════════════════════════╗
║  🤖 本地 Claude/Codex 智能体           ║
║  Web 界面: http://localhost:{port}        ║
║  API:  http://localhost:{port}/api/chat    ║
║  按 Ctrl+C 退出                         ║
╚══════════════════════════════════════════╝
""")
    server.serve_forever()


# ── 入口 ────────────────────────────────────────────

def main():
    print(f"\n🤖 本地 Claude/Codex 智能体 | 后端: {config['backend_type']}\n")

    if "--server" in sys.argv:
        run_server()
    else:
        run_clawbot()


if __name__ == "__main__":
    main()
