"""配置加载模块。从 .env 文件和环境变量中读取配置。"""

import os
from dotenv import load_dotenv

load_dotenv()


def load_config() -> dict:
    return {
        # AI 后端
        "backend_type": os.getenv("BACKEND_TYPE", "anthropic"),
        # Anthropic
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", ""),
        "anthropic_base_url": os.getenv("ANTHROPIC_BASE_URL", ""),
        "anthropic_model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        # 本地端点
        "local_endpoint_url": os.getenv("LOCAL_ENDPOINT_URL", "http://localhost:11434/v1/chat/completions"),
        "local_endpoint_format": os.getenv("LOCAL_ENDPOINT_FORMAT", "openai"),
        # Claude CLI
        "claude_cli_cmd": os.getenv("CLAUDE_CLI_CMD", 'claude -p "{prompt}" --print'),
        # 系统提示
        "system_prompt": os.getenv("SYSTEM_PROMPT", "你是一个有帮助的AI助手。"),
        # 微信
        "group_trigger": os.getenv("GROUP_TRIGGER", ""),
        "reset_key": os.getenv("RESET_KEY", "reset"),
        "max_history": int(os.getenv("MAX_HISTORY", "10")),
    }
