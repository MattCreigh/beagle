"""Sanity check for deduplicated parse_condition."""

import pytest

from beagle.core.orchestrator_types import AgentState
from beagle.core.workflow_builder import parse_condition


@pytest.fixture
def state():
    st = AgentState()
    st.query = "hello world"
    return st


class TestParseConditionDedup:
    def test_always(self, state):
        assert parse_condition("always")(state) is True

    def test_never(self, state):
        assert parse_condition("never")(state) is False

    def test_contains(self, state):
        assert parse_condition("state.query contains 'hel'")(state) is True
        assert parse_condition("state.query contains 'xyz'")(state) is False

    def test_is_not_empty(self, state):
        assert parse_condition("state.query is not empty")(state) is True

    def test_is_empty(self, state):
        assert parse_condition("state.missing is empty")(state) is True

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unrecognized workflow condition"):
            parse_condition("unknown condition")
