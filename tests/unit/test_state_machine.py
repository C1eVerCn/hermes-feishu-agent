"""Unit tests for mock_api/state_machine.py — reservation FSM."""
from mock_api.state_machine import VALID_TRANSITIONS, can_transition


def test_pending_can_go_to_approved_rejected_cancelled():
    assert set(VALID_TRANSITIONS[0]) == {1, 2, 3}


def test_approved_can_only_complete():
    assert VALID_TRANSITIONS[1] == [4]


def test_terminal_states_have_no_transitions():
    for s in (2, 3, 4):
        assert VALID_TRANSITIONS[s] == []


def test_can_transition_true_for_valid():
    assert can_transition(0, 1) is True
    assert can_transition(1, 4) is True


def test_can_transition_false_for_invalid():
    assert can_transition(1, 0) is False
    assert can_transition(4, 1) is False
    assert can_transition(0, 4) is False
