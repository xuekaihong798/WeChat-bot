"""AI 后端模块。支持 Anthropic API、本地 HTTP 端点、Claude CLI 三种后端。"""

from abc import ABC, abstractmethod
import json
import re
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

        resp = self.client.messages.create(**kwargs, timeout=120)

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
# Agent 后端（API + 工具调用）
# ============================================================

class AgentBackend(AIBackend):
    """Agent 后端，通过 Anthropic API + 工具调用执行本地操作。

    不依赖 claude CLI 二进制文件，直接在 Python 中实现 Agent 回路。
    """

    TOOLS = [
        {
            "name": "run_bash",
            "description": "Execute a bash shell command. Use for: launching apps, file ops, git, running scripts, system info.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command"}
                },
                "required": ["command"],
            },
        },
        {
            "name": "read_file",
            "description": "Read the complete contents of a file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"}
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Write or overwrite a file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    ]

    DANGEROUS = [
        "rm -rf /", "sudo rm", "mkfs", "dd if=", ":(){", "chmod 777 /",
        "> /dev/sda", "format c:", "del /f /s",
    ]

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        import anthropic
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self._platform_blurb = self._detect_platform()
        self._tools = list(self.TOOLS)
        # 集成 Skill 系统
        from skill_loader import registry as skill_registry
        self._skill_registry = skill_registry
        if self._skill_registry.list_names():
            self._tools.append({
                "name": "use_skill",
                "description": "Load a skill's full instructions by name. Use when a task matches an available skill's description. Available skills: " + ", ".join(self._skill_registry.list_names()),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Skill name to load"}
                    },
                    "required": ["name"],
                },
            })

    @staticmethod
    def _detect_platform() -> str:
        import platform
        import os
        import socket
        sysname = platform.system()
        return (
            f"运行环境：{sysname} {platform.release()} ({platform.machine()})\n"
            f"主机：{socket.gethostname()}  用户：{os.getenv('USER', '?')}\n"
            f"工作目录：{os.getcwd()}\n"
            f"注意：你在 {sysname} 系统上。macOS 用 'open -a AppName' 打开应用，"
            f"Linux 用应用名或 xdg-open。"
        )

    def _execute(self, name: str, inp: dict) -> str:
        if name == "use_skill":
            skill = self._skill_registry.get(inp["name"])
            if not skill:
                return f"Skill not found: {inp['name']}. Available: " + ", ".join(self._skill_registry.list_names())
            return f"SKILL LOADED: {skill['name']}\n\n{skill['body']}"
        if name == "run_bash":
            cmd = inp["command"]
            for pattern in self.DANGEROUS:
                if pattern in cmd.lower():
                    return f"BLOCKED: dangerous pattern '{pattern}'"
            try:
                r = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=30
                )
                return f"RC={r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
            except subprocess.TimeoutExpired:
                return "Error: timeout (30s)"
            except Exception as e:
                return f"Error: {e}"
        elif name == "read_file":
            try:
                with open(inp["path"]) as f:
                    return f.read()
            except Exception as e:
                return f"Error: {e}"
        elif name == "write_file":
            try:
                with open(inp["path"], "w") as f:
                    f.write(inp["content"])
                return f"OK: wrote {inp['path']}"
            except Exception as e:
                return f"Error: {e}"
        return f"Unknown tool: {name}"

    def chat(self, messages: list[dict]) -> str:
        user_system = ""
        chat_msgs = []
        for m in messages:
            if m["role"] == "system":
                user_system = m["content"]
            else:
                chat_msgs.append({"role": m["role"], "content": m["content"]})

        system = f"{user_system}\n\n{self._platform_blurb}" if user_system else self._platform_blurb
        skill_meta = self._skill_registry.metadata_text()
        if skill_meta:
            system += "\n" + skill_meta

        for _ in range(10):
            kwargs = {
                "model": self.model,
                "max_tokens": 2048,
                "messages": chat_msgs,
                "tools": self._tools,
            }
            if system:
                kwargs["system"] = [{"type": "text", "text": system}]

            resp = self.client.messages.create(**kwargs, timeout=120)

            tool_uses = []
            texts = []
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    texts.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)

            if resp.stop_reason == "end_turn":
                return texts[0] if texts else "(empty response)"

            if not tool_uses:
                return texts[0] if texts else "(no tool calls)"

            # Rebuild assistant content — must preserve thinking blocks for DeepSeek
            assistant_content = []
            for block in resp.content:
                if block.type == "thinking":
                    assistant_content.append({
                        "type": "thinking",
                        "thinking": block.thinking,
                        "signature": block.signature,
                    })
                elif block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            chat_msgs.append({"role": "assistant", "content": assistant_content})

            tool_results = []
            for tu in tool_uses:
                logger.info("🔧 %s: %s", tu.name, tu.input)
                result = self._execute(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })

            chat_msgs.append({"role": "user", "content": tool_results})

        return "(max tool rounds reached)"


# ============================================================
# 代理检测
# ============================================================

def _get_proxy_http_client():
    """检测 macOS 系统 SOCKS5 代理，返回 httpx.Client 或 None。"""
    import subprocess as sp
    try:
        result = sp.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5)
        text = result.stdout
        if "SOCKSEnable : 1" not in text:
            return None
        host_m = re.search(r"SOCKSProxy : (.+)", text)
        port_m = re.search(r"SOCKSPort : (\d+)", text)
        if not host_m or not port_m:
            return None
        host = host_m.group(1).strip()
        port = int(port_m.group(1).strip())
        import httpx
        return httpx.Client(proxy=f"socks5://{host}:{port}", timeout=120)
    except Exception:
        return None


def _normalize_content_for_openai(content):
    """将中性 image_path 块转换为 OpenAI image_url 块。"""
    if isinstance(content, str):
        return content
    result = []
    for block in content:
        if block.get("type") == "image_path":
            from image_utils import image_path_to_data_url
            data_url = image_path_to_data_url(block["image_path"])
            if data_url:
                result.append({
                    "type": "image_url",
                    "image_url": {"url": data_url},
                })
        else:
            result.append(block)
    return result


# ============================================================
# OpenAI API 后端
# ============================================================

class OpenAIBackend(AIBackend):
    """OpenAI 后端，支持 GPT-4o 图片识别。"""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        import openai
        http_client = _get_proxy_http_client()
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if http_client:
            kwargs["http_client"] = http_client
        self.client = openai.OpenAI(**kwargs)
        self.model = model

    def chat(self, messages: list[dict]) -> str:
        openai_messages = []
        for m in messages:
            openai_messages.append({
                "role": m["role"],
                "content": _normalize_content_for_openai(m["content"]),
            })

        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=4096,
            messages=openai_messages,
            timeout=120,
        )
        content = resp.choices[0].message.content
        return content or ""


class OpenAIAgentBackend(AIBackend):
    """OpenAI Agent 后端，API + 工具调用，支持 GPT-4o 图片识别。"""

    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "run_bash",
                "description": "Execute a bash shell command. Use for: launching apps, file ops, git, running scripts, system info.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The bash command"}
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the complete contents of a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path"}
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write or overwrite a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path"},
                        "content": {"type": "string", "description": "Content to write"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
    ]

    DANGEROUS = [
        "rm -rf /", "sudo rm", "mkfs", "dd if=", ":(){", "chmod 777 /",
        "> /dev/sda", "format c:", "del /f /s",
    ]

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        import openai
        http_client = _get_proxy_http_client()
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if http_client:
            kwargs["http_client"] = http_client
        self.client = openai.OpenAI(**kwargs)
        self.model = model
        self._platform_blurb = self._detect_platform()
        self._tools = list(self.TOOLS)
        from skill_loader import registry as skill_registry
        self._skill_registry = skill_registry
        if self._skill_registry.list_names():
            self._tools.append({
                "type": "function",
                "function": {
                    "name": "use_skill",
                    "description": "Load a skill's full instructions by name. Available: " + ", ".join(self._skill_registry.list_names()),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Skill name to load"}
                        },
                        "required": ["name"],
                    },
                },
            })

    @staticmethod
    def _detect_platform() -> str:
        import platform
        import os
        import socket
        sysname = platform.system()
        return (
            f"运行环境：{sysname} {platform.release()} ({platform.machine()})\n"
            f"主机：{socket.gethostname()}  用户：{os.getenv('USER', '?')}\n"
            f"工作目录：{os.getcwd()}\n"
            f"注意：你在 {sysname} 系统上。macOS 用 'open -a AppName' 打开应用，"
            f"Linux 用应用名或 xdg-open。"
        )

    def _execute(self, name: str, inp: dict) -> str:
        if name == "use_skill":
            skill = self._skill_registry.get(inp["name"])
            if not skill:
                return f"Skill not found: {inp['name']}. Available: " + ", ".join(self._skill_registry.list_names())
            return f"SKILL LOADED: {skill['name']}\n\n{skill['body']}"
        if name == "run_bash":
            cmd = inp["command"]
            for pattern in self.DANGEROUS:
                if pattern in cmd.lower():
                    return f"BLOCKED: dangerous pattern '{pattern}'"
            try:
                r = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=30
                )
                return f"RC={r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
            except subprocess.TimeoutExpired:
                return "Error: timeout (30s)"
            except Exception as e:
                return f"Error: {e}"
        elif name == "read_file":
            try:
                with open(inp["path"]) as f:
                    return f.read()
            except Exception as e:
                return f"Error: {e}"
        elif name == "write_file":
            try:
                with open(inp["path"], "w") as f:
                    f.write(inp["content"])
                return f"OK: wrote {inp['path']}"
            except Exception as e:
                return f"Error: {e}"
        return f"Unknown tool: {name}"

    def chat(self, messages: list[dict]) -> str:
        system = ""
        chat_msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                chat_msgs.append({
                    "role": m["role"],
                    "content": _normalize_content_for_openai(m["content"]),
                })

        full_system = system + "\n\n" + self._platform_blurb if system else self._platform_blurb
        skill_meta = self._skill_registry.metadata_text()
        if skill_meta:
            full_system += "\n" + skill_meta

        if full_system:
            chat_msgs.insert(0, {"role": "system", "content": full_system})

        for _ in range(10):
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=2048,
                messages=chat_msgs,
                tools=self._tools,
                timeout=120,
            )

            choice = resp.choices[0]
            if choice.finish_reason == "stop":
                return choice.message.content or ""

            if choice.message.tool_calls:
                chat_msgs.append({
                    "role": "assistant",
                    "content": choice.message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in choice.message.tool_calls
                    ],
                })

                for tc in choice.message.tool_calls:
                    args = json.loads(tc.function.arguments)
                    logger.info("🔧 %s: %s", tc.function.name, args)
                    result = self._execute(tc.function.name, args)
                    chat_msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
            else:
                return choice.message.content or ""

        return "(max tool rounds reached)"


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
    elif backend_type == "agent":
        return AgentBackend(
            api_key=config["anthropic_api_key"],
            model=config.get("anthropic_model", "deepseek-v4-pro"),
            base_url=config.get("anthropic_base_url", ""),
        )
    elif backend_type == "openai":
        return OpenAIBackend(
            api_key=config["openai_api_key"],
            model=config.get("openai_model", "gpt-4o"),
            base_url=config.get("openai_base_url", ""),
        )
    elif backend_type == "openai-agent":
        return OpenAIAgentBackend(
            api_key=config["openai_api_key"],
            model=config.get("openai_model", "gpt-4o"),
            base_url=config.get("openai_base_url", ""),
        )
    else:
        raise ValueError(f"未知的 BACKEND_TYPE: {backend_type}")
