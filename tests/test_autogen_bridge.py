"""Tests for AutoGen runtime bridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestBeagleAutoGenAgent:
    def test_agent_constructs(self):
        from beagle.bridges.autogen.agent import (
            BeagleAutoGenAgent,
        )

        agent = BeagleAutoGenAgent(name="test", system_message="Be helpful")
        assert agent.name == "test"
        assert agent._model != ""

    def test_agent_constructs_with_llm_config(self):
        from beagle.bridges.autogen.agent import (
            BeagleAutoGenAgent,
        )

        agent = BeagleAutoGenAgent(name="test", llm_config={"model": "glm-5:cloud"})
        assert agent._model == "glm-5:cloud"

    @pytest.mark.asyncio
    async def test_generate_reply(self):
        from beagle.bridges.autogen.agent import (
            BeagleAutoGenAgent,
        )

        agent = BeagleAutoGenAgent(name="test")
        with (
            patch(
                "beagle.utils.subprocess_pool.run_goose",
                new_callable=AsyncMock,
                return_value=("Hello from Beagle", ""),
            ),
            patch(
                "beagle.security.validation.validate_query_async",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
        ):
            reply = await agent.generate_reply(
                [
                    {"role": "user", "content": "Hello"},
                ]
            )
            assert reply == "Hello from Beagle"

    @pytest.mark.asyncio
    async def test_send_and_receive(self):
        from beagle.bridges.autogen.agent import (
            BeagleAutoGenAgent,
        )

        agent1 = BeagleAutoGenAgent(name="alice")
        agent2 = BeagleAutoGenAgent(name="bob")

        with (
            patch(
                "beagle.utils.subprocess_pool.run_goose",
                new_callable=AsyncMock,
                return_value=("Hi Alice!", ""),
            ),
            patch(
                "beagle.security.validation.validate_query_async",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
        ):
            # agent1 sends to agent2, agent2 receives and replies
            await agent1.send("Hello bob", agent2)
            # agent2 should have the message
            assert "alice" in agent2.chat_messages
            msgs = agent2.chat_messages["alice"]
            assert any("Hello bob" in m.get("content", "") for m in msgs)

    @pytest.mark.asyncio
    async def test_clear_history(self):
        from beagle.bridges.autogen.agent import (
            BeagleAutoGenAgent,
        )

        agent = BeagleAutoGenAgent(name="test")
        agent.chat_messages["bob"] = [{"role": "user", "content": "hi"}]
        agent.clear_history("bob")
        assert "bob" not in agent.chat_messages


class TestBeagleAutoGenAssistant:
    def test_assistant_constructs(self):
        from beagle.bridges.autogen.assistant import (
            BeagleAutoGenAssistant,
        )

        assistant = BeagleAutoGenAssistant(name="coder", system_message="Write code")
        assert assistant.name == "coder"
        assert assistant.system_message == "Write code"
        assert assistant._tools == []

    def test_assistant_with_tools(self):
        from beagle.bridges.autogen.assistant import (
            BeagleAutoGenAssistant,
        )

        tools = [{"name": "search", "description": "Search"}]
        assistant = BeagleAutoGenAssistant(name="coder", tools=tools)
        assert len(assistant._tools) == 1


class TestBeagleAutoGenUserProxy:
    def test_user_proxy_constructs(self):
        from beagle.bridges.autogen.user_proxy import (
            BeagleAutoGenUserProxy,
        )

        proxy = BeagleAutoGenUserProxy(name="user")
        assert proxy.name == "user"
        assert proxy.human_input_mode == "NEVER"

    @pytest.mark.asyncio
    async def test_user_proxy_no_reply(self):
        from beagle.bridges.autogen.user_proxy import (
            BeagleAutoGenUserProxy,
        )

        proxy = BeagleAutoGenUserProxy(name="user")
        reply = await proxy.generate_reply(
            [
                {"role": "user", "content": "Hello"},
            ]
        )
        assert reply == ""


class TestBeagleGroupChat:
    def test_group_chat_constructs(self):
        from beagle.bridges.autogen.agent import (
            BeagleAutoGenAgent,
        )
        from beagle.bridges.autogen.group_chat import (
            BeagleGroupChat,
        )

        agents = [
            BeagleAutoGenAgent(name="alice"),
            BeagleAutoGenAgent(name="bob"),
        ]
        chat = BeagleGroupChat(agents=agents, max_round=5)
        assert len(chat.agents) == 2
        assert chat.max_round == 5

    def test_speaker_selection_round_robin(self):
        from beagle.bridges.autogen.agent import (
            BeagleAutoGenAgent,
        )
        from beagle.bridges.autogen.group_chat import (
            BeagleGroupChat,
        )

        agents = [
            BeagleAutoGenAgent(name="a"),
            BeagleAutoGenAgent(name="b"),
            BeagleAutoGenAgent(name="c"),
        ]
        chat = BeagleGroupChat(agents=agents)
        assert chat._select_speaker(0).name == "a"
        assert chat._select_speaker(1).name == "b"
        assert chat._select_speaker(2).name == "c"
        assert chat._select_speaker(3).name == "a"  # Wraps around

    def test_speaker_selection_auto(self):
        from beagle.bridges.autogen.agent import (
            BeagleAutoGenAgent,
        )
        from beagle.bridges.autogen.group_chat import (
            BeagleGroupChat,
        )

        agents = [
            BeagleAutoGenAgent(name="alice"),
            BeagleAutoGenAgent(name="bob"),
        ]
        chat = BeagleGroupChat(
            agents=agents,
            speaker_selection_method="auto",
        )
        # Simulate last message from alice
        chat.messages = [
            {"role": "user", "content": "Start", "source": "system"},
            {"role": "assistant", "content": "Hi", "source": "alice"},
        ]
        # Should select bob (not alice who just spoke)
        speaker = chat._select_speaker(1)
        assert speaker.name == "bob"

    @pytest.mark.asyncio
    async def test_group_chat_terminates_on_keyword(self):
        from beagle.bridges.autogen.agent import (
            BeagleAutoGenAgent,
        )
        from beagle.bridges.autogen.group_chat import (
            BeagleGroupChat,
        )

        agents = [
            BeagleAutoGenAgent(name="alice"),
            BeagleAutoGenAgent(name="bob"),
        ]
        call_count = 0

        async def mock_goose(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                return "TERMINATE", ""
            return f"Response from round {call_count}", ""

        with (
            patch(
                "beagle.utils.subprocess_pool.run_goose",
                new_callable=AsyncMock,
                side_effect=mock_goose,
            ),
            patch(
                "beagle.security.validation.validate_query_async",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
        ):
            chat = BeagleGroupChat(agents=agents, max_round=5)
            result = await chat.run("Discuss AI safety")
            assert "TERMINATE" in result.summary

    @pytest.mark.asyncio
    async def test_group_chat_no_agents(self):
        from beagle.bridges.autogen.group_chat import (
            BeagleGroupChat,
        )

        chat = BeagleGroupChat(agents=[], max_round=5)
        result = await chat.run("Test task")
        assert "No agents" in result.summary


class TestMessages:
    def test_beagle_event_to_autogen(self):
        from beagle.bridges.autogen.messages import (
            beagle_event_to_autogen_message,
        )

        event = {
            "role": "assistant",
            "content": "Hello",
            "agent_name": "alice",
        }
        msg = beagle_event_to_autogen_message(event)
        assert msg["source"] == "alice"
        assert msg["content"] == "Hello"

    def test_autogen_message_to_beagle(self):
        from beagle.bridges.autogen.messages import (
            autogen_message_to_beagle_event,
        )

        msg = {"role": "user", "content": "Hi", "source": "bob"}
        event = autogen_message_to_beagle_event(msg)
        assert event["agent_name"] == "bob"
        assert event["event_type"] == "agent_message"
