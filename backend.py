"""AI 后端模块。支持 Anthropic API、本地 HTTP 端点、Claude CLI 三种后端。"""

from abc import ABC, abstractmethod
import subprocess
import logging

import requests

logger = logging.getLogger(__name__)


class AIBackend(ABC):
    """AI 后端抽象基类。"""

    @abstractmethod
    def chat(self, messages: list[dict]) -> str:
        """发送消息列表给 AI，返回回复文本。"""


# ============================================================
# Anthropic API 后端
# ============================================================

class AnthropicBackend(AIBackend):
    def __init__(self, api_key: str, model: str, base_url: str = ""):
        import anthropic
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model

    def chat(self, messages: list[dict]) -> str:
        system = ""
        chat_messages = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                chat_messages.append({"role": m["role"], "content": m["content"]})

        kwargs = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": chat_messages,
        }
        if system:
            kwargs["system"] = [{"type": "text", "text": system}]

        resp = self.client.messages.create(**kwargs)

        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Anthropic API: model=%s input_tokens=%s output_tokens=%s",
                        resp.model, resp.usage.input_tokens, resp.usage.output_tokens,
                    )
                return text

        return ""


# ============================================================
# 本地 HTTP 端点后端
# ============================================================

class LocalEndpointBackend(AIBackend):
    def __init__(self, url: str, format_type: str):
        self.url = url
        self.format_type = format_type

    def chat(self, messages: list[dict]) -> str:
        if self.format_type == "anthropic":
            body = self._build_anthropic_body(messages)
        else:
            body = self._build_openai_body(messages)

        resp = requests.post(self.url, json=body, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        return self._parse_response(data)

    def _build_openai_body(self, messages: list[dict]) -> dict:
        return {
            "messages": messages,
            "max_tokens": 4096,
        }

    def _build_anthropic_body(self, messages: list[dict]) -> dict:
        system = ""
        chat_messages = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                chat_messages.append({"role": m["role"], "content": m["content"]})
        body = {
            "messages": chat_messages,
            "max_tokens": 4096,
        }
        if system:
            body["system"] = system
        return body

    def _parse_response(self, data: dict) -> str:
        if self.format_type == "anthropic":
            return data["content"][0]["text"]
        # OpenAI 兼容格式
        return data["choices"][0]["message"]["content"]


# ============================================================
# Claude CLI 后端
# ============================================================

class ClaudeCLIBackend(AIBackend):
    def __init__(self, cmd_template: str):
        self.cmd_template = cmd_template

    def chat(self, messages: list[dict]) -> str:
        # 把完整对话历史打包进 prompt
        parts = []
        for m in messages:
            role = m["role"]
            content = m["content"]
            if role == "system":
                parts.append(f"System: {content}")
            elif role == "user":
                parts.append(f"User: {content}")
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
        full_prompt = "\n\n".join(parts)

        import shlex
        safe_prompt = shlex.quote(full_prompt)
        cmd = self.cmd_template.replace("{prompt}", safe_prompt)

        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=300,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI 执行失败: {result.stderr.strip()}")

        return result.stdout.strip()


# ============================================================
# 工厂函数
# ============================================================

def create_backend(config: dict) -> AIBackend:
    backend_type = config.get("backend_type", "anthropic")
    logger.info("初始化 AI 后端: %s", backend_type)

    if backend_type == "anthropic":
        return AnthropicBackend(
            api_key=config["anthropic_api_key"],
            model=config.get("anthropic_model", "claude-sonnet-4-6"),
            base_url=config.get("anthropic_base_url", ""),
        )
    elif backend_type == "local":
        return LocalEndpointBackend(
            url=config["local_endpoint_url"],
            format_type=config.get("local_endpoint_format", "openai"),
        )
    elif backend_type == "claude-cli":
        return ClaudeCLIBackend(
            cmd_template=config.get("claude_cli_cmd", 'claude -p "{prompt}" --print'),
        )
    else:
        raise ValueError(f"未知的 BACKEND_TYPE: {backend_type}")
