# 本地 Claude/Codex 智能体

本地 HTTP 服务，接收消息并返回 Claude/Codex 回复。自带 Web 聊天界面。

> 微信 web 协议已于 2024 年被腾讯关闭，故改为本地 HTTP Server 模式，开箱即用。

## 快速开始

```bash
source venv/bin/activate
python bot.py
# 浏览器打开 http://localhost:8765
```

## API

```bash
# 发送消息
curl -X POST http://localhost:8765/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好", "user_id": "myuser"}'

# 重置上下文
curl -X POST http://localhost:8765/api/reset \
  -H "Content-Type: application/json" \
  -d '{"user_id": "myuser"}'

# 健康检查
curl http://localhost:8765/health
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `bot.py` | 主入口，HTTP Server + Web 界面 |
| `backend.py` | AI 后端：Anthropic API / 本地端点 / Claude CLI |
| `config.py` | 从 `.env` 加载配置 |
| `.env.example` | 配置模板 |

## 指令

- `/ask 问题` — 普通问答
- `/code 需求` — 只输出代码
- `reset` — 重置对话上下文
