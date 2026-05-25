"""
ClawBot — 微信个人号 Bot，通过腾讯 iLink 官方 API 接入。
扫码登录，收发消息，自动调用 AI 后端回复。

iLink API 文档参考: github.com/SiverKing/weixin-ClawBot-API
"""

import time
import json
import struct
import base64
import random
import threading
import logging
import webbrowser
from io import BytesIO
from collections import deque

import requests

from image_utils import download_ilink_image, save_temp_image

logger = logging.getLogger("clawbot")

BASE_URL = "https://ilinkai.weixin.qq.com"


def _rand_uin() -> str:
    """生成 X-WECHAT-UIN：base64 编码的随机 uint32 十进制字符串。"""
    val = str(random.randint(1000000000, 9999999999))
    return base64.b64encode(val.encode()).decode()


def _auth_headers(token: str) -> dict:
    return {
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": _rand_uin(),
        "Content-Type": "application/json",
    }


def _client_id() -> str:
    return f"{int(time.time() * 1000)}_{random.randint(1000, 9999)}"


class ClawBot:
    def __init__(self, ai_chat_fn):
        self.chat_fn = ai_chat_fn
        self.token = ""
        self.bot_id = ""
        self.user_id = ""
        self.cursor = ""
        self.running = False
        self._seen_ids: deque[str] = deque(maxlen=1000)

    # ---- 登录 ----

    def login(self) -> bool:
        """获取二维码并等待用户扫码确认。"""
        # 1. 获取二维码
        resp = requests.get(f"{BASE_URL}/ilink/bot/get_bot_qrcode?bot_type=3", timeout=15)
        data = resp.json()
        qrcode_token = data["qrcode"]
        qrcode_img = data.get("qrcode_img_content", "")

        # 2. 显示二维码
        self._show_qrcode(qrcode_img)

        # 3. 轮询扫码状态
        print("\n⏳ 等待微信扫码...")
        status = "wait"
        while status not in ("confirmed", "expired"):
            time.sleep(2)
            resp = requests.get(
                f"{BASE_URL}/ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode_token},
                timeout=35,
            )
            data = resp.json()
            status = data.get("status", data.get("state", ""))
            logger.info("扫码状态: %s", status)

            if status == "scaned":
                print("📱 已扫描，请在手机上确认登录...")
            elif status == "confirmed":
                self.token = data["bot_token"]
                self.bot_id = data.get("ilink_bot_id", "")
                self.user_id = data.get("ilink_user_id", "")
                print(f"✅ 登录成功! Bot ID: {self.bot_id}")
                return True
            elif status == "expired":
                print("❌ 二维码已过期，请重新运行。")
                return False

        return False

    def _show_qrcode(self, qrcode_img: str):
        """在终端显示 ASCII 二维码，同时保存为 QR.png。"""
        if not qrcode_img:
            print("⚠️ 未获取到二维码，请重试。")
            return

        try:
            import qrcode
            qr = qrcode.QRCode(border=2)
            qr.add_data(qrcode_img)
            qr.make()
            # 保存 PNG
            img = qr.make_image(fill_color="black", back_color="white")
            img.save("QR.png")
            print("\n📱 二维码已保存为 QR.png，请用微信扫描。")
            print("   (有效期 2 分钟)\n")
            # 终端也显示
            qr.print_ascii(invert=True)
        except Exception:
            print(f"二维码链接: {qrcode_img}")
            try:
                webbrowser.open(qrcode_img)
            except Exception:
                pass

    # ---- 消息收发 ----

    def poll_loop(self):
        """长轮询消息循环。"""
        self.running = True
        self.cursor = ""

        while self.running:
            try:
                msgs = self._get_updates()
                if msgs is None:
                    # 连接过期
                    logger.warning("会话过期，重新登录...")
                    if self.login():
                        continue
                    else:
                        break

                for msg in msgs:
                    self._handle_message(msg)

            except requests.Timeout:
                continue
            except requests.ConnectionError:
                logger.warning("网络错误，3 秒后重试...")
                time.sleep(3)
            except Exception:
                logger.exception("轮询异常，5 秒后重试")
                time.sleep(5)

    def _get_updates(self) -> list[dict] | None:
        """长轮询获取新消息。返回 None 表示需要重登。"""
        headers = _auth_headers(self.token)
        body = {
            "get_updates_buf": self.cursor,
            "base_info": {"channel_version": "1.0.2"},
        }
        resp = requests.post(
            f"{BASE_URL}/ilink/bot/getupdates",
            headers=headers,
            json=body,
            timeout=40,
        )
        data = resp.json()

        errcode = data.get("errcode", 0)
        if errcode == -14:
            return None  # 会话过期
        if errcode != 0:
            logger.error("getupdates errcode=%s", errcode)
            return []

        # 更新 cursor
        new_cursor = data.get("get_updates_buf", "")
        if new_cursor:
            self.cursor = new_cursor

        return data.get("msgs", [])

    def _handle_message(self, msg: dict):
        """处理一条消息。"""
        try:
            context_token = msg.get("context_token", "")
            from_user = msg.get("from_user_id", "")
            msg_id = msg.get("msg_id", "")

            # 消息去重
            if msg_id:
                if msg_id in self._seen_ids:
                    logger.debug("跳过重复消息: %s", msg_id)
                    return
                self._seen_ids.append(msg_id)

            item_list = msg.get("item_list", [])
            text = ""
            msg_type = ""
            images = []
            for item in item_list:
                if item.get("type") == 1:
                    text = item.get("text_item", {}).get("text", "")
                elif item.get("type") == 2:
                    image_item = item.get("image_item", {})
                    encrypt_param = image_item.get("encrypt_query_param", "")
                    aes_key = image_item.get("aes_key", "")
                    if encrypt_param:
                        raw = download_ilink_image(encrypt_param, aes_key)
                        if raw:
                            path = save_temp_image(raw)
                            images.append(path)
                            msg_type = "📷" if not msg_type else msg_type
                            logger.info("📷 图片已保存: %s", path)
                elif item.get("type") == 3:
                    text = item.get("voice_item", {}).get("text", "")
                    msg_type = "🎤"

            if not text and not images:
                return

            if images:
                logger.info("📩 [%s] %s%s + %d 张图片", from_user[:20], msg_type, text[:100], len(images))
            else:
                logger.info("📩 [%s] %s%s", from_user[:20], msg_type, text[:100])

            # 调用 AI
            start = time.time()
            reply = self.chat_fn(from_user, text, images if images else None)
            elapsed = time.time() - start
            logger.info("📤 回复 (%.1fs): %s", elapsed, reply[:100])

            # 发送回复
            self.send_text(from_user, reply, context_token)

        except Exception:
            logger.exception("处理消息失败")

    def send_text(self, to_user: str, text: str, context_token: str):
        """发送文本消息。"""
        headers = _auth_headers(self.token)
        body = {
            "msg": {
                "to_user_id": to_user,
                "client_id": _client_id(),
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [
                    {"type": 1, "text_item": {"text": text}}
                ],
            },
            "base_info": {"channel_version": "1.0.2"},
        }
        resp = requests.post(
            f"{BASE_URL}/ilink/bot/sendmessage",
            headers=headers,
            json=body,
            timeout=30,
        )
        data = resp.json()
        if data.get("errcode", 0) != 0:
            logger.error("发送失败: %s", data)

    def stop(self):
        self.running = False
