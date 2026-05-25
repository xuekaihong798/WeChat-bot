"""Tests for OpenAIBackend and OpenAIAgentBackend."""
import os
import sys
import base64
import pytest
from unittest.mock import patch, Mock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestOpenAIBackend:
    """Tests for the simple OpenAIBackend (no tools)."""

    @patch("openai.OpenAI")
    def test_text_only_chat(self, mock_openai_cls):
        from backend import OpenAIBackend

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello, world!"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.model = "gpt-4o"
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_client

        backend = OpenAIBackend(api_key="sk-test", model="gpt-4o")
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi there"},
        ]
        reply = backend.chat(messages)

        assert reply == "Hello, world!"
        call_args = mock_client.chat.completions.create.call_args
        assert call_args[1]["model"] == "gpt-4o"
        sent_messages = call_args[1]["messages"]
        assert sent_messages[0]["role"] == "system"
        assert sent_messages[0]["content"] == "You are helpful."

    @patch("openai.OpenAI")
    def test_message_content_passthrough(self, mock_openai_cls):
        """Content is passed through to OpenAI API as-is (string or list)."""
        from backend import OpenAIBackend

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "I see a cat"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_client

        backend = OpenAIBackend(api_key="sk-test", model="gpt-4o")

        content = [
            {"type": "text", "text": "What's this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]
        messages = [{"role": "user", "content": content}]
        reply = backend.chat(messages)

        assert reply == "I see a cat"
        sent_content = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"]
        assert sent_content == content

    @patch("openai.OpenAI")
    def test_empty_response(self, mock_openai_cls):
        from backend import OpenAIBackend

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_client

        backend = OpenAIBackend(api_key="sk-test", model="gpt-4o")
        reply = backend.chat([{"role": "user", "content": "test"}])
        assert reply == ""


class TestOpenAIAgentBackend:
    """Tests for OpenAIAgentBackend with tool calling."""

    @patch("openai.OpenAI")
    def test_text_only_chat_no_tools_called(self, mock_openai_cls):
        from backend import OpenAIAgentBackend

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Sure, I can help!"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_client

        backend = OpenAIAgentBackend(api_key="sk-test", model="gpt-4o")
        messages = [{"role": "user", "content": "Hello"}]
        reply = backend.chat(messages)

        assert reply == "Sure, I can help!"

    @patch("openai.OpenAI")
    def test_system_prompt_augmentation(self, mock_openai_cls):
        from backend import OpenAIAgentBackend

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "OK"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_client

        backend = OpenAIAgentBackend(api_key="sk-test", model="gpt-4o")
        messages = [{"role": "system", "content": "Be helpful."}]
        backend.chat(messages)

        sent_messages = mock_client.chat.completions.create.call_args[1]["messages"]
        system_msg = sent_messages[0]
        assert system_msg["role"] == "system"
        assert "Be helpful." in system_msg["content"]
        assert "运行环境" in system_msg["content"]  # platform blurb added

    @patch("openai.OpenAI")
    def test_single_tool_call_round(self, mock_openai_cls):
        from backend import OpenAIAgentBackend

        mock_client = MagicMock()

        # First call: returns a tool_call
        tool_call = MagicMock()
        tool_call.id = "call_123"
        tool_call.type = "function"
        tool_call.function.name = "run_bash"
        tool_call.function.arguments = '{"command":"echo hello"}'

        choice1 = MagicMock()
        choice1.message.content = None
        choice1.message.tool_calls = [tool_call]
        choice1.finish_reason = "tool_calls"

        # Second call: final response
        choice2 = MagicMock()
        choice2.message.content = "Done!"
        choice2.message.tool_calls = None
        choice2.finish_reason = "stop"

        mock_client.chat.completions.create.side_effect = [
            MagicMock(choices=[choice1], model="gpt-4o"),
            MagicMock(choices=[choice2], model="gpt-4o"),
        ]
        mock_openai_cls.return_value = mock_client

        backend = OpenAIAgentBackend(api_key="sk-test", model="gpt-4o")
        reply = backend.chat([{"role": "user", "content": "Run echo hello"}])

        assert reply == "Done!"
        assert mock_client.chat.completions.create.call_count == 2

    @patch("openai.OpenAI")
    def test_tools_included_in_request(self, mock_openai_cls):
        from backend import OpenAIAgentBackend

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "No tools needed"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_client

        backend = OpenAIAgentBackend(api_key="sk-test", model="gpt-4o")
        backend.chat([{"role": "user", "content": "test"}])

        call_args = mock_client.chat.completions.create.call_args
        assert "tools" in call_args[1]
        tools = call_args[1]["tools"]
        tool_names = [t["function"]["name"] for t in tools]
        assert "run_bash" in tool_names
        assert "read_file" in tool_names
        assert "write_file" in tool_names
