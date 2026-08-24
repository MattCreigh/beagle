"""Tests for CrewAI runtime bridge."""

from __future__ import annotations

from unittest.mock import MagicMock


class TestBeagleCrewAILLM:
    def test_llm_constructs_with_defaults(self):
        from beagle.bridges.crewai.llm import BeagleCrewAILLM

        llm = BeagleCrewAILLM()
        assert llm.model != ""
        assert llm.temperature == 0.7

    def test_llm_constructs_with_custom_model(self):
        from beagle.bridges.crewai.llm import BeagleCrewAILLM

        llm = BeagleCrewAILLM(model="glm-5:cloud")
        assert llm.model == "glm-5:cloud"

    def test_messages_to_prompt(self):
        from beagle.bridges.crewai.llm import BeagleCrewAILLM

        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        prompt = BeagleCrewAILLM._messages_to_prompt(messages)
        assert "[user]: Hello" in prompt
        assert "[assistant]: Hi there" in prompt
        assert "system" not in prompt.lower()

    def test_messages_to_prompt_empty(self):
        from beagle.bridges.crewai.llm import BeagleCrewAILLM

        prompt = BeagleCrewAILLM._messages_to_prompt([])
        assert prompt == ""


class TestBeagleCrewAITool:
    def test_tool_constructs(self):
        from beagle.bridges.crewai.tools import BeagleCrewAITool

        tool = BeagleCrewAITool(
            name="test_tool",
            description="A test tool",
            func=lambda x: f"result: {x}",
        )
        assert tool.name == "test_tool"
        assert tool.description == "A test tool"

    def test_tool_run_sync(self):
        from beagle.bridges.crewai.tools import BeagleCrewAITool

        tool = BeagleCrewAITool(
            name="echo",
            description="Echo input",
            func=lambda x: f"echo: {x}",
        )
        result = tool._run("hello")
        assert result == "echo: hello"


class TestBeagleCrewAIAgent:
    def test_agent_constructs_with_role(self):
        from beagle.bridges.crewai.agent import BeagleCrewAIAgent

        agent = BeagleCrewAIAgent(
            role="researcher",
            goal="Find information",
            backstory="Expert researcher",
        )
        assert agent.role == "researcher"
        assert agent.goal == "Find information"

    def test_agent_from_recipe(self):
        from beagle.bridges.crewai.agent import BeagleCrewAIAgent

        # Should not crash even if recipe doesn't exist
        agent = BeagleCrewAIAgent.from_recipe("nonexistent-recipe")
        assert agent is not None

    def test_agent_execute_task(self):
        from beagle.bridges.crewai.agent import BeagleCrewAIAgent

        agent = BeagleCrewAIAgent(
            role="tester",
            goal="Test things",
            backstory="I test things",
        )
        agent.llm = MagicMock()
        agent.llm.call = MagicMock(return_value="Task done")

        task = MagicMock()
        task.description = "Write a test"
        task.expected_output = "A passing test"
        task._context_results = []

        result = agent.execute_task(task)
        assert result == "Task done"
        agent.llm.call.assert_called_once()


class TestBeagleCrewAITask:
    def test_task_constructs(self):
        from beagle.bridges.crewai.task import BeagleCrewAITask

        task = BeagleCrewAITask(
            description="Do research",
            expected_output="A report",
        )
        assert task.description == "Do research"
        assert task.expected_output == "A report"
        assert task.output == ""

    def test_task_default_expected_output(self):
        from beagle.bridges.crewai.task import BeagleCrewAITask

        task = BeagleCrewAITask(description="Do something")
        assert task.expected_output == "Complete the task successfully."

    def test_task_from_workflow_phase(self):
        from beagle.bridges.crewai.task import BeagleCrewAITask

        phase = {"prompt_template": "Analyze X", "expected_output": "Analysis"}
        task = BeagleCrewAITask.from_workflow_phase(phase)
        assert task.description == "Analyze X"

    def test_set_context_results(self):
        from beagle.bridges.crewai.task import BeagleCrewAITask

        task = BeagleCrewAITask(description="Summarize")
        task.set_context_results(["previous result 1", "previous result 2"])
        assert len(task._context_results) == 2


class TestBeagleCrewAICrew:
    def test_crew_sequential_execution(self):
        from beagle.bridges.crewai.agent import BeagleCrewAIAgent
        from beagle.bridges.crewai.crew import BeagleCrewAICrew
        from beagle.bridges.crewai.task import BeagleCrewAITask

        agent = BeagleCrewAIAgent(role="test", goal="test", backstory="test")
        agent.llm = MagicMock()
        agent.llm.call = MagicMock(return_value="Task completed")

        task1 = BeagleCrewAITask(description="Do thing 1", agent=agent)
        task2 = BeagleCrewAITask(description="Do thing 2", agent=agent)

        crew = BeagleCrewAICrew(agents=[agent], tasks=[task1, task2])
        result = crew.kickoff()

        assert result.raw == "Task completed"
        assert len(result.tasks_output) == 2
        assert agent.llm.call.call_count == 2

    def test_crew_with_inputs(self):
        from beagle.bridges.crewai.agent import BeagleCrewAIAgent
        from beagle.bridges.crewai.crew import BeagleCrewAICrew
        from beagle.bridges.crewai.task import BeagleCrewAITask

        agent = BeagleCrewAIAgent(role="test", goal="test", backstory="test")
        agent.llm = MagicMock()
        agent.llm.call = MagicMock(return_value="Done")

        task = BeagleCrewAITask(description="Research {topic}", agent=agent)
        crew = BeagleCrewAICrew(agents=[agent], tasks=[task])
        crew.kickoff(inputs={"topic": "AI safety"})

        # Verify input substitution happened
        call_args = agent.llm.call.call_args[0][0]  # messages list
        prompt_text = str(call_args)
        assert "AI safety" in prompt_text

    def test_crew_hierarchical(self):
        from beagle.bridges.crewai.agent import BeagleCrewAIAgent
        from beagle.bridges.crewai.crew import BeagleCrewAICrew
        from beagle.bridges.crewai.task import BeagleCrewAITask

        agent = BeagleCrewAIAgent(role="manager", goal="manage", backstory="I manage")
        agent.llm = MagicMock()
        agent.llm.call = MagicMock(return_value="Summary of all tasks")

        task1 = BeagleCrewAITask(description="Task 1", agent=agent)
        crew = BeagleCrewAICrew(agents=[agent], tasks=[task1], process="hierarchical")
        result = crew.kickoff()
        assert "Summary" in result.raw

    def test_crew_output_type(self):
        from beagle.bridges.crewai.crew import CrewOutput

        output = CrewOutput(raw="test", tasks_output=[{"a": 1}])
        assert output.raw == "test"
        assert len(output.tasks_output) == 1


class TestConverter:
    def test_crewai_to_beagle(self):
        from beagle.bridges.crewai.converter import (
            crewai_to_beagle_workflow,
        )

        agents = [{"role": "Researcher", "goal": "Find stuff"}]
        tasks = [
            {"description": "Research AI", "agent_role": "Researcher"},
            {"description": "Write report", "agent_role": "Researcher"},
        ]
        workflow = crewai_to_beagle_workflow(agents, tasks)
        assert len(workflow["phases"]) == 2
        assert workflow["phases"][1].get("depends_on") == ["phase_1"]

    def test_roundtrip_conversion(self):
        from beagle.bridges.crewai.converter import (
            beagle_workflow_to_crewai,
            crewai_to_beagle_workflow,
        )

        agents = [{"role": "Coder", "goal": "Write code"}]
        tasks = [{"description": "Build feature", "agent_role": "Coder"}]
        workflow = crewai_to_beagle_workflow(agents, tasks)
        agents_back, tasks_back = beagle_workflow_to_crewai(workflow)
        assert len(agents_back) >= 1
        assert len(tasks_back) == 1

    def test_beagle_to_crewai(self):
        from beagle.bridges.crewai.converter import (
            beagle_workflow_to_crewai,
        )

        workflow = {
            "phases": [
                {"agent": "researcher", "prompt_template": "Search"},
                {"agent": "writer", "prompt_template": "Write"},
            ]
        }
        agents, tasks = beagle_workflow_to_crewai(workflow)
        assert len(agents) == 2  # researcher + writer
        assert len(tasks) == 2
