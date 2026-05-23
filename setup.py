"""py2app 打包配置。"""

from setuptools import setup

APP = ["app.py"]
DATA_FILES = [
    ("skills/deploy-to-domain", ["skills/deploy-to-domain/SKILL.md"]),
    (".", [".env"]),
]
OPTIONS = {
    "argv_emulation": False,
    "packages": ["anthropic", "requests", "qrcode", "PIL", "webview", "dotenv"],
    "excludes": ["tkinter", "curses", "PySide", "PyQt"],
    "plist": {
        "CFBundleName": "WeChat-bot",
        "CFBundleDisplayName": "WeChat-bot",
        "CFBundleIdentifier": "cc.practices.wechat-bot",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "14.0",
    },
}

setup(
    name="WeChat-bot",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
