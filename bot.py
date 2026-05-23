#!/usr/bin/env python3
"""
本地 Claude/Codex 智能体。

模式:
  python bot.py            → 微信 ClawBot 模式（扫码登录）
  python bot.py --server   → HTTP Server 模式（浏览器 http://localhost:8765）
"""

import sys
import time
import json
import base64
import threading
import logging
import signal
import io

import requests

from config import load_config
from backend import create_backend

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")

config = load_config()

# 两个后端：Web 用配置的后端，ClawBot 强制用本地 Claude Code（有工具/Agent 能力）
web_backend = create_backend(config)
clawbot_config = dict(config)
clawbot_config["backend_type"] = "agent"
clawbot_backend = create_backend(clawbot_config)

MAX_HISTORY = config["max_history"]
SYSTEM_PROMPT = config["system_prompt"]
RESET_KEY = config["reset_key"]

# ClawBot 状态（Server 模式下扫码接入）
clawbot_state = {"qrcode": "", "status": "", "bot": None, "thread": None, "started": False}
clawbot_lock = threading.Lock()


# ── 对话管理 ────────────────────────────────────────

def _make_chat_handler(backend_inst):
    """创建绑定到指定后端的对话处理器。"""
    conversations: dict[str, list[dict]] = {}
    lock = threading.Lock()

    def handle(user_id: str, text: str) -> str:
        text = text.strip()

        if text.lower() == RESET_KEY.lower():
            with lock:
                conversations.pop(user_id, None)
            return "✅ 上下文已重置"

        if text.startswith("/code "):
            text = f"请只输出代码，不要解释：\n{text[6:]}"
        elif text.startswith("/ask "):
            text = text[5:]

        with lock:
            history = list(conversations.get(user_id, []))[-MAX_HISTORY * 2:]

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        logger.info("📩 [%s] %s", type(backend_inst).__name__[:8], text[:100])

        start = time.time()
        try:
            reply = backend_inst.chat(messages)
        except Exception as e:
            logger.error("AI 失败: %s", e)
            reply = "抱歉，AI 服务调用失败。"

        elapsed = time.time() - start
        logger.info("📤 (%.1fs) %s", elapsed, reply[:100])

        with lock:
            hist = conversations.get(user_id, [])
            hist.append({"role": "user", "content": text})
            hist.append({"role": "assistant", "content": reply})
            max_msgs = MAX_HISTORY * 2
            if len(hist) > max_msgs:
                hist = hist[-max_msgs:]
            conversations[user_id] = hist

        return reply

    return handle


# Web 聊天（API 后端，快速响应）
chat_web = _make_chat_handler(web_backend)

# Web Agent（API + 工具调用，可执行本地操作）
web_agent_config = dict(config)
web_agent_config["backend_type"] = "agent"
web_agent_backend = create_backend(web_agent_config)
chat_web_agent = _make_chat_handler(web_agent_backend)

# 微信 ClawBot（Agent 模式，可操作电脑）
chat_agent = _make_chat_handler(clawbot_backend)


# ── ClawBot 扫码接入 ────────────────────────────────

ILINK_BASE = "https://ilinkai.weixin.qq.com"


def _gen_qrcode_base64(url: str) -> str:
    """生成 QR Code PNG，返回 base64 data URL。"""
    import qrcode
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make()
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def clawbot_get_qrcode() -> dict:
    """获取 ClawBot 登录二维码，返回 {qrcode_token, qrcode_img}。"""
    resp = requests.get(f"{ILINK_BASE}/ilink/bot/get_bot_qrcode?bot_type=3", timeout=15)
    data = resp.json()
    url = data.get("qrcode_img_content", "") or data.get("qrcode", "")
    if url and not url.startswith("http"):
        url = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={data['qrcode']}&bot_type=3"
    return {
        "qrcode_token": data["qrcode"],
        "qrcode_img": _gen_qrcode_base64(url),
    }


def clawbot_poll_status(qrcode_token: str) -> str:
    """轮询扫码状态。返回 wait / scaned / confirmed / expired。"""
    resp = requests.get(
        f"{ILINK_BASE}/ilink/bot/get_qrcode_status",
        params={"qrcode": qrcode_token},
        timeout=15,
    )
    data = resp.json()
    return data.get("status", data.get("state", "wait"))


def _run_clawbot_thread(token: str, bot_id: str, user_id: str):
    """后台线程：运行 ClawBot 消息轮询。"""
    from clawbot import ClawBot
    bot = ClawBot(ai_chat_fn=chat_agent)
    bot.token = token
    bot.bot_id = bot_id
    bot.user_id = user_id
    with clawbot_lock:
        clawbot_state["bot"] = bot
        clawbot_state["status"] = "connected"
    logger.info("✅ ClawBot 已接入: %s", bot_id)
    try:
        bot.poll_loop()
    finally:
        with clawbot_lock:
            clawbot_state["started"] = False
            clawbot_state["status"] = "disconnected"
            clawbot_state["bot"] = None
        logger.info("🔌 ClawBot 已断开: %s", bot_id)


def clawbot_start_polling(qrcode_token: str):
    """扫码确认后，获取 token 并启动后台轮询线程。"""
    data = clawbot_poll_status(qrcode_token)
    resp = requests.get(
        f"{ILINK_BASE}/ilink/bot/get_qrcode_status",
        params={"qrcode": qrcode_token},
        timeout=15,
    )
    info = resp.json()
    token = info.get("bot_token", "")
    bot_id = info.get("ilink_bot_id", "")
    user_id = info.get("ilink_user_id", "")

    if not token:
        return

    t = threading.Thread(target=_run_clawbot_thread, args=(token, bot_id, user_id), daemon=True)
    t.start()
    with clawbot_lock:
        clawbot_state["thread"] = t


# ── 模式 1: 微信 ClawBot ─────────────────────────────

def run_clawbot():
    from clawbot import ClawBot

    bot = ClawBot(ai_chat_fn=chat_agent)

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
#input-area{display:flex;padding:12px 16px;gap:8px;border-top:1px solid #0f3460;background:#16213e;flex-wrap:wrap}
#input{flex:1;min-width:150px;padding:10px 14px;border-radius:8px;border:1px solid #0f3460;background:#1a1a2e;color:#eee;font-size:14px;outline:none;resize:none;min-height:40px;max-height:120px}
#input:focus{border-color:#533483}
#send,#wechat-btn{padding:10px 16px;border:none;border-radius:8px;color:#fff;font-size:14px;cursor:pointer;font-weight:600}
#send{background:#533483}#send:hover{background:#6b44a0}#send:disabled{opacity:.5;cursor:not-allowed}
#wechat-btn{background:#07c160}#wechat-btn:hover{background:#06ad56}#wechat-btn:disabled{opacity:.5;cursor:not-allowed}
#wechat-btn.connected{background:#555;cursor:default}
#agent-toggle{display:flex;align-items:center;gap:6px;font-size:13px;color:#aaa;cursor:pointer;user-select:none;white-space:nowrap}
#agent-toggle input{accent-color:#533483;width:16px;height:16px;cursor:pointer}
#agent-toggle.checked{color:#e4bf7a}
#status{font-size:11px;color:#888;padding:4px 16px 8px}
#qrmodal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);z-index:10;justify-content:center;align-items:center}
#qrmodal.show{display:flex}
#qrbox{background:#1a1a2e;border-radius:12px;padding:24px;text-align:center;max-width:340px}
#qrbox img{width:240px;height:240px;border-radius:8px;margin:12px 0}
#qrbox .qr-title{font-size:16px;font-weight:600;margin-bottom:4px}
#qrbox .qr-hint{font-size:12px;color:#888;margin-bottom:8px}
#qrbox .qr-status{font-size:14px;color:#07c160;margin-top:8px}
#qrbox .qr-close{margin-top:12px;padding:8px 24px;border:1px solid #888;border-radius:6px;background:transparent;color:#888;cursor:pointer;font-size:13px}
</style>
</head>
<body>
<header>&#129302; 本地 Claude/Codex 智能体</header>
<div id="chat"><div class="msg system">发送消息开始对话 · /code 需求 · reset 重置</div></div>
<div id="input-area">
  <textarea id="input" placeholder="输入消息..." rows="1" autofocus></textarea>
  <button id="send" onclick="send()">发送</button>
  <button id="wechat-btn" onclick="startWechat()">微信bot</button>
  <label id="agent-toggle" onclick="toggleAgent()"><input type="checkbox" id="agent-cb"> 执行</label>
</div>
<div id="status">AI 后端: {backend}</div>

<div id="qrmodal">
  <div id="qrbox">
    <div class="qr-title">&#128241; 微信扫码接入</div>
    <div class="qr-hint">用微信扫描二维码，确认后即可在微信中对话</div>
    <img id="qrimg" src="" alt="QR Code">
    <div class="qr-status" id="qrstatus">等待扫码...</div>
    <button class="qr-close" onclick="closeQR()">关闭</button>
  </div>
</div>

<script>
const c=document.getElementById('chat'),i=document.getElementById('input'),b=document.getElementById('send'),
      wb=document.getElementById('wechat-btn'),uid='web_'+Date.now(),cb=document.getElementById('agent-cb');
let qrTimer=null;

function add(role,t){const d=document.createElement('div');d.className='msg '+role;d.textContent=t;c.appendChild(d);c.scrollTop=c.scrollHeight}
function toggleAgent(){
  const on=cb.checked;
  document.getElementById('agent-toggle').className=on?'checked':'';
  i.placeholder=on?'输入指令，AI 可执行本地操作...':'输入消息...';
}
function send(){
  const t=i.value.trim();if(!t)return;
  const agent=cb.checked;
  add('user',(agent?'[执行] ':'')+t);i.value='';b.disabled=true;
  const ep=agent?'/api/agent':'/api/chat';
  fetch(ep,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:t,user_id:uid})})
    .then(r=>r.json()).then(d=>{add('assistant',d.reply||d.error)})
    .catch(e=>add('system','请求失败: '+e.message)).finally(()=>{b.disabled=false;i.focus()});
}
i.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});

function startWechat(){
  if(wb.classList.contains('connected')) return;
  wb.disabled=true; wb.textContent='获取中...';
  document.getElementById('qrmodal').classList.add('show');
  document.getElementById('qrstatus').textContent='获取二维码...';

  fetch('/api/wechat/qrcode',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(r=>r.json()).then(d=>{
      if(d.error){alert(d.error);closeQR();return;}
      document.getElementById('qrimg').src=d.qrcode_img;
      document.getElementById('qrstatus').textContent='等待扫码...';
      pollQR(d.qrcode_token);
    }).catch(e=>{alert('获取二维码失败');closeQR()});
}

function pollQR(token){
  qrTimer=setInterval(()=>{
    fetch('/api/wechat/status?qrcode='+encodeURIComponent(token))
      .then(r=>r.json()).then(d=>{
        if(d.status==='scaned') document.getElementById('qrstatus').textContent='已扫描，请在手机上确认...';
        else if(d.status==='confirmed'){
          document.getElementById('qrstatus').textContent='✅ 已接入！微信中即可对话';
          wb.textContent='微信已接入'; wb.classList.add('connected');
          clearInterval(qrTimer);
        }else if(d.status==='expired'){
          document.getElementById('qrstatus').textContent='❌ 二维码已过期';
          clearInterval(qrTimer);
        }
      });
  },2000);
}

function closeQR(){
  clearInterval(qrTimer);
  document.getElementById('qrmodal').classList.remove('show');
  wb.disabled=false;
  if(!wb.classList.contains('connected')) wb.textContent='微信bot';
}
</script>
</body>
</html>""".replace("{backend}", config["backend_type"])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._html(HTML)
            elif self.path == "/health":
                self._json({"status": "ok", "backend": config["backend_type"]})
            elif self.path.startswith("/api/wechat/status"):
                params = {}
                if "?" in self.path:
                    from urllib.parse import parse_qs, urlparse
                    params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                qrcode_token = params.get("qrcode", "")
                if not qrcode_token:
                    self._json({"error": "缺少 qrcode 参数"}, 400)
                    return
                try:
                    stat = clawbot_poll_status(qrcode_token)
                    if stat == "confirmed":
                        with clawbot_lock:
                            if not clawbot_state["started"]:
                                clawbot_state["started"] = True
                            else:
                                stat = "already_started"
                        if stat == "confirmed":
                            threading.Thread(target=clawbot_start_polling, args=(qrcode_token,), daemon=True).start()
                    self._json({"status": stat})
                except Exception as e:
                    self._json({"error": str(e)}, 500)
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
                reply = chat_web(uid, msg)
                elapsed = time.time() - start
                self._json({"reply": reply, "elapsed": round(elapsed, 2)})
            elif self.path == "/api/agent":
                msg = body.get("message", "").strip()
                uid = body.get("user_id", "default")
                if not msg:
                    return self._json({"error": "message 不能为空"}, 400)
                start = time.time()
                reply = chat_web_agent(uid, msg)
                elapsed = time.time() - start
                self._json({"reply": reply, "elapsed": round(elapsed, 2)})
            elif self.path == "/api/reset":
                reset_user(body.get("user_id", "default"))
                self._json({"status": "ok"})
            elif self.path == "/api/wechat/qrcode":
                try:
                    data = clawbot_get_qrcode()
                    with clawbot_lock:
                        clawbot_state["qrcode"] = data["qrcode_token"]
                        clawbot_state["status"] = "wait"
                    self._json(data)
                except Exception as e:
                    logger.exception("获取二维码失败")
                    self._json({"error": str(e)}, 500)
            else:
                self.send_error(404)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _html(self, content, s=200):
            self.send_response(s); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(content.encode())

        def _json(self, d, s=200):
            self.send_response(s); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
            self.wfile.write(json.dumps(d, ensure_ascii=False).encode())

        def log_message(self, f, *a):
            logger.debug("%s %s", self.address_string(), f % a)

    host, port = "0.0.0.0", 8765
    server = HTTPServer((host, port), Handler)
    def shutdown(sig, frame):
        with clawbot_lock:
            if clawbot_state.get("bot"):
                clawbot_state["bot"].stop()
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
