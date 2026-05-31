# CLAUDE.md

## 项目概述

**WeChat-bot** — 本地 AI 智能体，用户发消息 → Claude/Codex 处理 → 返回回复。

- **语言**: Python 3.14
- **依赖**: `anthropic`, `requests`, `qrcode`, `Pillow`, `pywebview`（详见 `requirements.txt`）
- **API**: Anthropic Messages API（`backend.py`），兼容 DeepSeek/OpenAI 格式
- **微信接入**: 腾讯 iLink Bot API（`clawbot.py`）
- **部署**: Docker（`docker-compose.yml`）或直接 `python bot.py`

## 对 Claude 的要求

1. 先完成测试再编码，禁止直接完成编码。
2. 有问题尽可能多确认——你给方案我来选，不要写完代码再说问题。

## 运行命令

```bash
source venv/bin/activate

# HTTP Server 模式（浏览器 + API）
python bot.py --server          # http://localhost:8765

# 微信 ClawBot 模式（终端扫码，微信对话）
python bot.py                   # 需要 TTY 显示二维码

# Docker
docker compose up -d            # HTTP Server 模式

# 运行所有测试
pytest tests/ -v

# 运行特定测试
pytest tests/test_backend_openai.py -v
pytest tests/test_image_utils.py -v
```

## 目录结构

```
.
├── bot.py              # 入口：_make_chat_handler() 工厂 + run_server() + run_clawbot()
├── backend.py           # AIBackend 抽象基类 → AnthropicBackend / AgentBackend / LocalEndpointBackend
├── clawbot.py           # ClawBot：微信扫码登录 → 长轮询 → 收发消息
├── config.py            # .env + 环境变量解析
├── image_utils.py        # 图片下载 / 临时存储
├── skill_loader.py      # Skill 系统加载器
├── app.py               # macOS 桌面 App（py2app 打包入口）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── skills/              # 可用 Skill 定义（Markdown）
│   └── deploy-to-domain/
├── tests/
│   ├── test_backend_openai.py
│   └── test_image_utils.py
└── venv/
```

## 架构

```
Web 浏览器 → chat_web()  → BACKEND_TYPE（默认 anthropic，快速聊天）
微信消息   → chat_agent() → AgentBackend（Agent 模式，可操作电脑）
```

两入口通过 `_make_chat_handler(backend)` 各自创建独立对话管理，按 `user_id` 隔离，内存存储，上限 `MAX_HISTORY` 轮。Server 模式可通过 Web 界面「微信bot」按钮扫码接入。

## 关键约束

- **context_token**: ClawBot 发送消息必须携带收到的 `context_token`，否则消息被静默丢弃。
- **getupdates 游标**: 空 buf 为首次请求；超时/AbortError 复用同一游标重试；`errcode: -14` = 需重新登录。
- **ThinkingBlock 兼容**: `AnthropicBackend.chat()` 必须遍历 `resp.content` 找 `type=="text"` 的 block，不可硬取 `[0]`。
- **Agent 回路 DeepSeek**: API 返回 `tool_use` → Python 本地执行 → `tool_result` 回传。使用 DeepSeek API 时需保留 `thinking` 块回传。
- **.env**: 不提交（gitignore）。API Key 通过环境变量注入，`ANTHROPIC_API_KEY` 后备到 `DEEPSEEK_API_KEY`。
