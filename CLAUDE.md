# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本地 AI 智能体，接收消息 → Claude/Codex 处理 → 返回回复。支持两种运行模式和一个 Web 界面。

## 对Claude的要求
1.先完成测试再编码，禁止直接完成编码。
2.有问题尽可能多确认，你给方案我来选，不要写完代码再说问题。

## 运行命令

```bash
source venv/bin/activate

# HTTP Server 模式（浏览器 + API）
python bot.py --server          # http://localhost:8765

# 微信 ClawBot 模式（终端扫码，微信对话）
python bot.py                   # 需要 TTY 显示二维码

# Docker
docker compose up -d            # HTTP Server 模式
```

## 架构

```
Web 浏览器 → chat_web() → AnthropicBackend（快速聊天）
微信消息   → chat_agent() → AgentBackend（Agent 模式，可操作电脑）
```

**两个入口使用不同后端**，通过 `_make_chat_handler(backend)` 工厂各自创建独立的对话管理：

| 入口 | 函数 | 后端 | 能力 |
|------|------|------|------|
| HTTP Server | `chat_web` | `BACKEND_TYPE` 配置（默认 anthropic） | 快速对话 |
| ClawBot | `chat_agent` | 强制 `agent` | 读文件/执行命令/写代码 |

Server 模式通过 Web 界面「微信bot」按钮扫码接入，后台线程运行 ClawBot 轮询，与 Server 并存。

## 核心模块

- **`bot.py`** — `_make_chat_handler()` 工厂为不同后端创建独立对话管理。`run_server()` 内嵌 HTTP handler + Web UI HTML。`run_clawbot()` 为终端扫码模式。
- **`backend.py`** — `AIBackend` 抽象基类，四个实现：`AnthropicBackend`（含 `base_url` 支持 DeepSeek 兼容）、`AgentBackend`（API + 工具调用，替代 `claude-cli`，运行时自动检测平台）、`LocalEndpointBackend`（OpenAI/Anthropic 格式）、`ClaudeCLIBackend`（`shlex.quote` 注入 shell，已弃用）。工厂函数 `create_backend()`。
- **`clawbot.py`** — `ClawBot` 类，包装腾讯 iLink Bot API（`ilinkai.weixin.qq.com`）：`login()` → 长轮询 `getupdates`（游标式，空 buf 首次，超时复用，errcode:-14 重登）→ `sendmessage`（必带 `context_token`）。QR 码用 `qrcode` 库生成 PNG + ASCII。
- **`config.py`** — `load_config()` 从 `.env` + 环境变量读取。`ANTHROPIC_API_KEY` 后备到 `DEEPSEEK_API_KEY`。

## 关键细节

- 对话历史存内存 `conversations` dict，按 `user_id` 隔离，上限 `MAX_HISTORY` 轮。
- `AnthropicBackend.chat()` 兼容 ThinkingBlock：遍历 `resp.content` 找 `type=="text"` 的 block，而非硬取 `[0]`。
- ClawBot 消息发送必须带 `context_token`，来自收到的消息，丢失则消息被服务端静默丢弃。
- ClawBot getupdates 使用游标式长轮询，空 `get_updates_buf` 为首次请求，超时/AbortError 复用同一游标重试。`errcode: -14` 表示需重新登录。
- `AgentBackend` 通过 Anthropic API + 工具定义实现 Agent 回路：API 返回 `tool_use` → Python 本地执行 → `tool_result` 回传 API。不依赖外部二进制文件。DeepSeek API 需保留 `thinking` 块回传。
- `claude-cli` 后端（已弃用）通过 `shlex.quote()` 注入 shell 调用 `claude -p --print`。在部分环境中 `claude` 二进制被 SIGKILL，已由 `AgentBackend` 替代。
- `.env` 不提交（gitignore），API Key 通过环境变量注入。
