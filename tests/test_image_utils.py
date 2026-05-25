"""image_utils 模块测试 — CDN下载、格式检测、临时文件、data URL。"""
import os, sys, io, base64, struct, time, hashlib
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from image_utils import (
    save_temp_image, image_path_to_data_url, guess_mime_type,
    cleanup_temp_images, _detect_image_format,
)

RAW_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 96
RAW_PNG  = b"\x89PNG\r\n\x1a\n" + b"\x00" * 96
RAW_GIF  = b"GIF89a" + b"\x00" * 96
RAW_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 96


class TestDetectImageFormat:
    def test_jpeg(self):   assert _detect_image_format(RAW_JPEG) == ".jpg"
    def test_png(self):    assert _detect_image_format(RAW_PNG)  == ".png"
    def test_gif89a(self): assert _detect_image_format(RAW_GIF)  == ".gif"
    def test_webp(self):   assert _detect_image_format(RAW_WEBP) == ".webp"
    def test_default(self):
        assert _detect_image_format(b"\x00" * 64) == ".jpg"


class TestGuessMimeType:
    def test_jpg(self):  assert guess_mime_type("a.jpg")  == "image/jpeg"
    def test_png(self):  assert guess_mime_type("a.png")  == "image/png"
    def test_gif(self):  assert guess_mime_type("a.gif")  == "image/gif"
    def test_webp(self): assert guess_mime_type("a.webp") == "image/webp"


class TestSaveTempImage:
    def test_saves_and_returns_path(self):
        path = save_temp_image(RAW_PNG)
        assert os.path.isfile(path)
        assert path.endswith(".png")

    def test_dedup_same_content(self):
        a = save_temp_image(RAW_JPEG)
        b = save_temp_image(RAW_JPEG)
        assert a == b

    def test_different_content(self):
        a = save_temp_image(RAW_JPEG)
        b = save_temp_image(RAW_PNG)
        assert a != b


class TestImagePathToDataUrl:
    def test_valid(self):
        path = save_temp_image(RAW_PNG)
        url = image_path_to_data_url(path)
        assert url.startswith("data:image/png;base64,")

    def test_missing_file(self):
        assert image_path_to_data_url("/tmp/no_such_file_xyz.xxx") == ""


class TestCleanupTempImages:
    def test_removes_old_files(self):
        p = save_temp_image(RAW_JPEG)
        # Set mtime to 25 hours ago
        old = time.time() - 25 * 3600
        os.utime(p, (old, old))
        n = cleanup_temp_images(max_age_hours=24)
        assert n >= 1
        assert not os.path.isfile(p)

    def test_keeps_recent_files(self):
        p = save_temp_image(RAW_JPEG)
        n = cleanup_temp_images(max_age_hours=24)
        assert os.path.isfile(p)
