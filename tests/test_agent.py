"""
Unit tests for agent/agent.py — the Agent class.

LLM calls are mocked throughout so tests run without a running model server.
"""
import os
from unittest.mock import MagicMock, patch
import pytest

from agent.agent import Agent
from agent.schemas import ChatMessage, Role, AgentResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_SYSTEM_PROMPT = "You are a memory agent."
SIMPLE_REPLY_RESPONSE = "<reply>Here is your answer.</reply>"
THINK_AND_REPLY_RESPONSE = (
    "<think>I should check the user file.</think>\n"
    "<reply>Your name is Test User.</reply>"
)
PYTHON_THEN_REPLY = (
    "<think>I need to read user.md.</think>\n"
    "<python>result = read_file('user.md')</python>"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agent(tmp_path):
    """
    Create an Agent instance with mocked LLM client and system prompt loading.
    Memory path is a real temporary directory.
    """
    with (
        patch("agent.agent.load_system_prompt", return_value=FAKE_SYSTEM_PROMPT),
        patch("agent.agent.create_openai_client", return_value=MagicMock()),
        patch("agent.agent.create_vllm_client", return_value=MagicMock()),
    ):
        a = Agent(memory_path=str(tmp_path), predetermined_memory_path=False)
        a.memory_path = str(tmp_path)  # ensure absolute path is the tmp dir
        return a


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

class TestAgentInit:
    def test_system_prompt_is_first_message(self, agent):
        assert len(agent.messages) == 1
        assert agent.messages[0].role == Role.SYSTEM
        assert agent.messages[0].content == FAKE_SYSTEM_PROMPT

    def test_memory_path_is_absolute(self, agent):
        assert os.path.isabs(agent.memory_path)

    def test_default_max_tool_turns(self, agent):
        assert agent.max_tool_turns > 0


# ---------------------------------------------------------------------------
# _add_message
# ---------------------------------------------------------------------------

class TestAddMessage:
    def test_add_chat_message(self, agent):
        msg = ChatMessage(role=Role.USER, content="Hello")
        agent._add_message(msg)
        assert agent.messages[-1].role == Role.USER
        assert agent.messages[-1].content == "Hello"

    def test_add_dict_message(self, agent):
        agent._add_message({"role": "user", "content": "Hi from dict"})
        assert agent.messages[-1].content == "Hi from dict"

    def test_invalid_type_raises(self, agent):
        with pytest.raises(ValueError):
            agent._add_message(42)


# ---------------------------------------------------------------------------
# extract_response_parts
# ---------------------------------------------------------------------------

class TestExtractResponseParts:
    def test_extracts_reply(self, agent):
        thoughts, reply, code = agent.extract_response_parts(SIMPLE_REPLY_RESPONSE)
        assert reply == "Here is your answer."
        assert code is None or code == ""

    def test_extracts_think_and_reply(self, agent):
        thoughts, reply, code = agent.extract_response_parts(THINK_AND_REPLY_RESPONSE)
        assert "check" in thoughts.lower() or thoughts  # thoughts present
        assert reply == "Your name is Test User."

    def test_extracts_python_block(self, agent):
        thoughts, reply, code = agent.extract_response_parts(PYTHON_THEN_REPLY)
        assert code is not None
        assert "read_file" in code

    def test_empty_response_returns_empty_strings(self, agent):
        thoughts, reply, code = agent.extract_response_parts("")
        # Should not raise; values may be None or empty
        assert reply is None or reply == ""


# ---------------------------------------------------------------------------
# chat — end-to-end with mocked LLM
# ---------------------------------------------------------------------------

class TestChat:
    def test_simple_chat_returns_agent_response(self, agent):
        with patch("agent.agent.get_model_response", return_value=SIMPLE_REPLY_RESPONSE):
            response = agent.chat("Hello")

        assert isinstance(response, AgentResponse)
        assert response.reply == "Here is your answer."

    def test_user_message_added_to_history(self, agent):
        with patch("agent.agent.get_model_response", return_value=SIMPLE_REPLY_RESPONSE):
            agent.chat("What is my name?")

        user_messages = [m for m in agent.messages if m.role == Role.USER]
        assert any("What is my name?" in m.content for m in user_messages)

    def test_assistant_message_added_to_history(self, agent):
        with patch("agent.agent.get_model_response", return_value=SIMPLE_REPLY_RESPONSE):
            agent.chat("Hello")

        assistant_messages = [m for m in agent.messages if m.role == Role.ASSISTANT]
        assert len(assistant_messages) >= 1

    def test_tool_turn_loop_runs_when_no_reply(self, agent):
        """
        When the first response has no <reply>, the agent should loop and
        call get_model_response again, feeding back tool results.
        """
        responses = iter([
            # First call: python code, no reply yet
            "<python>x = 1 + 1</python>",
            # Second call: reply
            SIMPLE_REPLY_RESPONSE,
        ])

        with (
            patch("agent.agent.get_model_response", side_effect=lambda **_: next(responses)),
            patch("agent.agent.execute_sandboxed_code", return_value=({"x": 2}, "")),
            patch("agent.agent.create_memory_if_not_exists"),
        ):
            response = agent.chat("What is 1+1?")

        assert response.reply == "Here is your answer."

    def test_save_conversation_creates_file(self, agent, tmp_path):
        with patch("agent.agent.get_model_response", return_value=SIMPLE_REPLY_RESPONSE):
            agent.chat("Hello")

        save_dir = str(tmp_path / "conversations")
        agent.save_conversation(save_folder=save_dir)

        saved_files = os.listdir(save_dir)
        assert len(saved_files) == 1
        assert saved_files[0].endswith(".json")
