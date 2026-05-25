"""图片工具：CDN 下载、格式检测、临时文件、base64 转换。"""

import os
import time
import hashlib
import logging

import requests

logger = logging.getLogger(__name__)

_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
TEMP_DIR = os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "wechat_bot_images"
)

_IMAGE_SIGNATURES = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
    (b"BM", ".bmp"),
]


def _ensure_temp_dir():
    os.makedirs(TEMP_DIR, exist_ok=True)


# ---- format detection ----

def _detect_image_format(raw: bytes) -> str:
    """从魔术字节检测图片格式，返回扩展名（含点）。"""
    for sig, ext in _IMAGE_SIGNATURES:
        if raw[: len(sig)] == sig:
            if ext == ".webp" and len(raw) > 12 and raw[8:12] != b"WEBP":
                continue
            return ext
    return ".jpg"


def guess_mime_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "image/png")


# ---- CDN download ----

def download_ilink_image(encrypt_query_param: str, aes_key_b64: str) -> bytes | None:
    """从微信 CDN 下载图片，必要时 AES-128-ECB 解密。"""
    from Crypto.Cipher import AES

    url = f"{_CDN_BASE}/download?encrypted_query_param={encrypt_query_param}"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            logger.warning("CDN 下载失败: HTTP %s", resp.status_code)
            return None
    except requests.RequestException as e:
        logger.warning("CDN 下载失败: %s", e)
        return None

    raw = resp.content

    # 如果已经是有效图片则无需解密
    if _detect_image_format(raw) != ".jpg" or raw[:2] == b"\xff\xd8":
        return raw

    # AES-128-ECB 解密（兼容旧版加密）
    try:
        key = base64.b64decode(aes_key_b64)
        cipher = AES.new(key, AES.MODE_ECB)
        plaintext = cipher.decrypt(raw)
        pad_len = plaintext[-1]
        if 1 <= pad_len <= 16:
            plaintext = plaintext[:-pad_len]
        else:
            plaintext = plaintext.rstrip(b"\x00")
        return plaintext
    except Exception as e:
        logger.warning("AES 解密失败: %s", e)
        return None


# ---- temp file management ----

def save_temp_image(raw: bytes) -> str:
    """SHA256 去重保存到临时目录，扩展名从魔术字节检测。"""
    _ensure_temp_dir()
    digest = hashlib.sha256(raw).hexdigest()[:16]
    ext = _detect_image_format(raw)
    path = os.path.join(TEMP_DIR, f"{digest}{ext}")
    if not os.path.isfile(path):
        with open(path, "wb") as f:
            f.write(raw)
    return path


def image_path_to_data_url(path: str) -> str:
    """读取本地图片返回 'data:image/...;base64,...'，文件不存在返空字符串。"""
    if not os.path.isfile(path):
        return ""
    with open(path, "rb") as f:
        raw = f.read()
    import base64
    mime = guess_mime_type(path)
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def cleanup_temp_images(max_age_hours: int = 24) -> int:
    """清理超过 max_age_hours 的临时图片，返回删除数量。"""
    if not os.path.isdir(TEMP_DIR):
        return 0
    deadline = time.time() - max_age_hours * 3600
    removed = 0
    for name in os.listdir(TEMP_DIR):
        p = os.path.join(TEMP_DIR, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < deadline:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed
