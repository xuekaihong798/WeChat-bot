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

# 两个后端：Web 用配置的后端，ClawBot 用 Agent 模式（有工具/Agent 能力）
web_backend = create_backend(config)
clawbot_config = dict(config)
# ClawBot 优先用 OpenAI Agent（支持图片识别），否则用 Anthropic Agent
if clawbot_config.get("openai_api_key"):
    clawbot_config["backend_type"] = "openai-agent"
else:
    clawbot_config["backend_type"] = "agent"
clawbot_backend = create_backend(clawbot_config)

MAX_HISTORY = config["max_history"]
SYSTEM_PROMPT = config["system_prompt"]
RESET_KEY = config["reset_key"]

# ClawBot 状态（Server 模式下扫码接入）
clawbot_state = {"qrcode": "", "status": "", "bot": None, "thread": None, "started": False}
clawbot_lock = threading.Lock()


# ── 对话管理 ────────────────────────────────────────

def _make_chat_handler(backend_inst, vision_backend=None):
    """创建绑定到指定后端的对话处理器。

    backend_inst -- 主后端（聊天/推理，如 Anthropic/DeepSeek）
    vision_backend -- 可选视觉后端（图片识别，如 GPT-4o）
                       有图片时先由 vision 识别，结果喂给主后端回复。
    """
    conversations: dict[str, list[dict]] = {}
    lock = threading.Lock()

    def handle(user_id: str, text: str, images: list[str] | None = None) -> str:
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

        # ── 两阶段管线：图片 → 视觉识别 → 主后端聊天 ──
        user_content_for_history = text  # 存入历史的内容

        if images and vision_backend:
            vision_content = [
                {"type": "text", "text": text or "请详细描述这张图片的内容，包含所有关键信息和细节。"},
            ]
            for img_path in images:
                vision_content.append({"type": "image_path", "image_path": img_path})
            try:
                description = vision_backend.chat([{"role": "user", "content": vision_content}])
                logger.info("👁️ 图片识别完成 (%d chars)", len(description))
            except Exception as e:
                logger.error("图片识别失败: %s", e)
                description = "(图片识别失败)"

            augmented = f"{text}\n\n[图片描述]\n{description}" if text else description
            user_content_for_history = augmented
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages.extend(history)
            messages.append({"role": "user", "content": augmented})

        elif images:
            content = [{"type": "text", "text": text}]
            for img_path in images:
                content.append({"type": "image_path", "image_path": img_path})
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages.extend(history)
            messages.append({"role": "user", "content": content})

        else:
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
            hist.append({"role": "user", "content": user_content_for_history})
            hist.append({"role": "assistant", "content": reply})
            max_msgs = MAX_HISTORY * 2
            if len(hist) > max_msgs:
                hist = hist[-max_msgs:]
            conversations[user_id] = hist

        return reply

    return handle


# 视觉后端（图片识别 → GPT-4o，作为主后端的"眼睛"）
vision_backend = None
if config.get("openai_api_key"):
    vision_config = dict(config)
    vision_config["backend_type"] = "openai"
    vision_backend = create_backend(vision_config)
    logger.info("👁️ 视觉后端已启用: %s", vision_config.get("openai_model", "gpt-4o"))

# Web 聊天（API 后端，快速响应）
chat_web = _make_chat_handler(web_backend, vision_backend)

# Web Agent（API + 工具调用，可执行本地操作）
web_agent_config = dict(config)
web_agent_config["backend_type"] = "agent"
web_agent_backend = create_backend(web_agent_config)
chat_web_agent = _make_chat_handler(web_agent_backend, vision_backend)

# 微信 ClawBot（Agent 模式，可操作电脑）
chat_agent = _make_chat_handler(clawbot_backend, vision_backend)


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
    from socketserver import ThreadingMixIn

    def _decode_client_images(images_b64: list[str]) -> list[str]:
        """将前端发来的 base64 data URL 列表转为临时文件路径列表。"""
        from image_utils import save_temp_image
        paths = []
        for b64 in images_b64:
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            try:
                raw = base64.b64decode(b64)
                paths.append(save_temp_image(raw))
            except Exception:
                pass
        return paths

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """多线程 HTTP Server，并发处理请求。"""
        daemon_threads = True

    HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WeChat-bot</title>
<style>
:root{
  --bg:#F2F2F7;--card:#FFFFFF;--text:#1C1C1E;--text2:#8E8E93;--bubble-user:#007AFF;--bubble-user-text:#FFFFFF;
  --bubble-assistant:#E5E5EA;--bubble-assistant-text:#1C1C1E;--border:rgba(60,60,67,0.08);--shadow:0 1px 3px rgba(0,0,0,0.06);
  --blur:blur(20px) saturate(180%);--accent:#007AFF;--green:#34C759;--red:#FF3B30;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#1C1C1E;--card:#2C2C2E;--text:#FFFFFF;--text2:#98989D;
    --bubble-assistant:#2C2C2E;--bubble-assistant-text:#FFFFFF;--border:rgba(255,255,255,0.08);--shadow:0 1px 4px rgba(0,0,0,0.3);
  }
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Helvetica Neue',sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
/* ── Header ── */
header{background:var(--card);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);padding:14px 20px;font-size:17px;font-weight:590;text-align:center;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:center;gap:6px}
/* ── Chat ── */
#chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px;scroll-behavior:smooth}
.msg{max-width:76%;padding:10px 15px;border-radius:20px;line-height:1.45;white-space:pre-wrap;word-break:break-word;font-size:15px;box-shadow:var(--shadow);animation:slideIn .25s ease-out}
@keyframes slideIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.msg.user{align-self:flex-end;background:var(--bubble-user);color:var(--bubble-user-text);border-bottom-right-radius:6px}
.msg.assistant{align-self:flex-start;background:var(--bubble-assistant);color:var(--bubble-assistant-text);border-bottom-left-radius:6px}
.msg.system{align-self:center;background:transparent;color:var(--text2);font-size:12px;padding:6px 10px;box-shadow:none;animation:none}
/* ── Input Area ── */
#input-area{display:flex;padding:10px 12px 16px;gap:8px;align-items:flex-end;background:var(--card);border-top:1px solid var(--border);position:sticky;bottom:0;backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur)}
#input{flex:1;padding:11px 18px;border-radius:20px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:15px;font-family:inherit;outline:none;resize:none;min-height:42px;max-height:120px;line-height:1.35;transition:border-color .2s,border-radius .2s}
#input:focus{border-color:var(--accent);border-radius:22px}
#send,#wechat-btn,#img-btn{width:42px;height:42px;border-radius:50%;border:none;cursor:pointer;font-size:18px;display:flex;align-items:center;justify-content:center;transition:transform .15s,opacity .15s;flex-shrink:0}
#send{background:var(--accent);color:#fff}#send:hover{transform:scale(1.06)}#send:active{transform:scale(0.96)}#send:disabled{opacity:.35;transform:none}
#wechat-btn{background:var(--green);color:#fff;font-size:14px}#wechat-btn:hover{transform:scale(1.06)}#wechat-btn:disabled{opacity:.35;transform:none}
#wechat-btn.connected{background:var(--text2)}
#img-btn{background:var(--bg);color:var(--text2);border:1px solid var(--border);font-size:16px}#img-btn:hover{color:var(--accent);border-color:var(--accent)}
/* ── Image Previews ── */
#img-previews{display:flex;gap:6px;padding:4px 14px 0;flex-wrap:wrap;background:var(--card)}
.img-pv{position:relative;width:52px;height:52px;border-radius:10px;overflow:hidden;border:1px solid var(--border);flex-shrink:0}
.img-pv img{width:100%;height:100%;object-fit:cover}
.img-pv .img-rm{position:absolute;top:2px;right:2px;width:18px;height:18px;border-radius:50%;background:rgba(0,0,0,0.55);color:#fff;border:none;cursor:pointer;font-size:11px;line-height:18px;text-align:center;padding:0}
/* ── Toolbar ── */
#toolbar{display:flex;align-items:center;gap:4px;padding:0 14px 6px;background:var(--card)}
#agent-toggle{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--text2);cursor:pointer;user-select:none;padding:4px 10px;border-radius:14px;background:var(--bg);border:1px solid var(--border);transition:all .2s}
#agent-toggle input{accent-color:var(--accent);width:13px;height:13px;cursor:pointer}
#agent-toggle.active{color:var(--accent);border-color:var(--accent);background:rgba(0,122,255,0.08)}
/* ── Status ── */
#status{font-size:11px;color:var(--text2);text-align:center;padding:6px 16px 10px;background:var(--card)}
/* ── QR Modal ── */
#qrmodal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.45);z-index:20;justify-content:center;align-items:center;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
#qrmodal.show{display:flex}
#qrbox{background:var(--card);border-radius:20px;padding:28px 24px;text-align:center;max-width:320px;box-shadow:0 8px 32px rgba(0,0,0,0.18)}
#qrbox img{width:220px;height:220px;border-radius:12px;margin:16px 0}
#qrbox .qr-title{font-size:17px;font-weight:590}
#qrbox .qr-hint{font-size:13px;color:var(--text2);margin-bottom:4px}
#qrbox .qr-status{font-size:14px;color:var(--accent);margin-top:6px}
#qrbox .qr-close{margin-top:14px;padding:8px 28px;border:1px solid var(--border);border-radius:20px;background:transparent;color:var(--text2);cursor:pointer;font-size:14px;font-family:inherit;transition:all .2s}
#qrbox .qr-close:hover{background:var(--bg)}
</style>
</head>
<body>
<header><span>WeChat-bot</span></header>
<div id="chat"><div class="msg system">发送消息开始对话</div></div>
<div id="toolbar"><label id="agent-toggle" onclick="toggleAgent()"><input type="checkbox" id="agent-cb"> 执行模式</label></div>
<div id="img-previews"></div>
<div id="input-area">
  <button id="wechat-btn" onclick="startWechat()" title="微信接入">👤</button>
  <button id="img-btn" onclick="document.getElementById('img-input').click()" title="添加图片">🖼</button>
  <textarea id="input" placeholder="消息..." rows="1" autofocus></textarea>
  <button id="send" onclick="send()">↑</button>
</div>
<input type="file" id="img-input" accept="image/*" multiple style="display:none" onchange="addFiles(this.files)">
<div id="status">AI 后端: {backend}</div>

<div id="qrmodal">
  <div id="qrbox">
    <div class="qr-title">微信扫码接入</div>
    <div class="qr-hint">用微信扫描二维码确认后即可在微信中对话</div>
    <img id="qrimg" src="" alt="QR Code">
    <div class="qr-status" id="qrstatus">等待扫码...</div>
    <button class="qr-close" onclick="closeQR()">关闭</button>
  </div>
</div>

<script>
const c=document.getElementById('chat'),i=document.getElementById('input'),b=document.getElementById('send'),
      wb=document.getElementById('wechat-btn'),uid='web_'+Date.now(),cb=document.getElementById('agent-cb'),
      pv=document.getElementById('img-previews');
let qrTimer=null,images=[];

function add(role,t,imgUrls){
  const d=document.createElement('div');d.className='msg '+role;
  if(imgUrls&&imgUrls.length){
    const imgs=imgUrls.map(u=>`<img src="${u}" style="max-width:200px;max-height:200px;border-radius:10px;margin:4px 0;display:block">`).join('');
    d.innerHTML='<div>'+t.replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</div>'+imgs;
  }else{
    d.textContent=t;
  }
  c.appendChild(d);
  requestAnimationFrame(()=>{c.scrollTop=c.scrollHeight});
}
function addFiles(files){
  for(let f of files){
    const r=new FileReader();
    r.onload=function(){images.push(r.result);renderPreviews()};
    r.readAsDataURL(f);
  }
  document.getElementById('img-input').value='';
}
function removeImg(idx){images.splice(idx,1);renderPreviews()}
function renderPreviews(){
  pv.innerHTML=images.map((u,i)=>`<div class="img-pv"><img src="${u}"><button class="img-rm" onclick="removeImg(${i})">×</button></div>`).join('');
}
document.addEventListener('paste',function(e){
  const items=e.clipboardData&&e.clipboardData.items;
  if(!items)return;
  for(let item of items){
    if(item.type.startsWith('image/')){
      e.preventDefault();
      const f=item.getAsFile();
      const r=new FileReader();
      r.onload=function(){images.push(r.result);renderPreviews()};
      r.readAsDataURL(f);
    }
  }
});
function toggleAgent(){
  const on=cb.checked;
  document.getElementById('agent-toggle').className='agent-toggle'+(on?' active':'');
  i.placeholder=on?'输入指令，可执行本地操作…':'消息…';
}
function send(){
  const t=i.value.trim();if(!t&&!images.length)return;
  const agent=cb.checked;
  const imgs=images.slice();
  add('user',t||'(图片)',imgs);i.value='';images=[];renderPreviews();b.disabled=true;
  const ep=agent?'/api/agent':'/api/chat';
  fetch(ep,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:t||'描述这张图片',user_id:uid,images:imgs})})
    .then(r=>r.json()).then(d=>{add('assistant',d.reply||d.error)})
    .catch(e=>add('system','请求失败: '+e.message)).finally(()=>{b.disabled=false;i.focus()});
}
i.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
i.addEventListener('input',()=>{i.style.height='auto';i.style.height=Math.min(i.scrollHeight,120)+'px'});

function startWechat(){
  if(wb.classList.contains('connected')) return;
  wb.disabled=true; wb.textContent='…';
  document.getElementById('qrmodal').classList.add('show');
  document.getElementById('qrstatus').textContent='获取二维码…';

  fetch('/api/wechat/qrcode',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(r=>r.json()).then(d=>{
      if(d.error){alert(d.error);closeQR();return;}
      document.getElementById('qrimg').src=d.qrcode_img;
      document.getElementById('qrstatus').textContent='等待扫码…';
      pollQR(d.qrcode_token);
    }).catch(e=>{alert('获取二维码失败');closeQR()});
}

function pollQR(token){
  qrTimer=setInterval(()=>{
    fetch('/api/wechat/status?qrcode='+encodeURIComponent(token))
      .then(r=>r.json()).then(d=>{
        if(d.status==='scaned') document.getElementById('qrstatus').textContent='已扫描，请在手机上确认…';
        else if(d.status==='confirmed'){
          document.getElementById('qrstatus').textContent='已接入！微信中即可对话';
          wb.textContent='✓'; wb.classList.add('connected');
          clearInterval(qrTimer);
        }else if(d.status==='expired'){
          document.getElementById('qrstatus').textContent='二维码已过期';
          clearInterval(qrTimer);
        }
      });
  },2000);
}

function closeQR(){
  clearInterval(qrTimer);
  document.getElementById('qrmodal').classList.remove('show');
  wb.disabled=false;
  if(!wb.classList.contains('connected')) wb.textContent='👤';
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
                client_images = body.get("images", [])
                if not msg:
                    return self._json({"error": "message 不能为空"}, 400)
                image_paths = _decode_client_images(client_images) if client_images else None
                start = time.time()
                reply = chat_web(uid, msg, image_paths)
                elapsed = time.time() - start
                self._json({"reply": reply, "elapsed": round(elapsed, 2)})
            elif self.path == "/api/agent":
                msg = body.get("message", "").strip()
                uid = body.get("user_id", "default")
                client_images = body.get("images", [])
                if not msg:
                    return self._json({"error": "message 不能为空"}, 400)
                image_paths = _decode_client_images(client_images) if client_images else None
                start = time.time()
                reply = chat_web_agent(uid, msg, image_paths)
                elapsed = time.time() - start
                self._json({"reply": reply, "elapsed": round(elapsed, 2)})
            elif self.path == "/api/reset":
                uid = body.get("user_id", "default")
                chat_web(uid, RESET_KEY)
                chat_web_agent(uid, RESET_KEY)
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
    try:
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
    except ValueError:
        pass  # 非主线程（如 app.py 模式）

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
