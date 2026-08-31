import pytest
from hypothesis import given
from hypothesis import strategies as st

from oslab.state_machine import TRANSITIONS, SupervisorState, reaches_terminal, validate_transition


@given(st.sampled_from(list(SupervisorState)))
def test_every_nonterminal_state_reaches_terminal(state: SupervisorState) -> None:
    assert reaches_terminal(state)


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_transition(SupervisorState.SELECT_TASK, SupervisorState.COMPLETE)


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in (SupervisorState.COMPLETE, SupervisorState.BLOCKED, SupervisorState.CANCELLED):
        assert not TRANSITIONS[state]
