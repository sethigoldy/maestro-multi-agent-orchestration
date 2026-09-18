from pathlib import Path
from maestro.models import AgentState, Phase, Task, to_agent_state


def test_task_defaults_and_custom_fields():
    task = Task('t', 'title', 'request', Path('design'), Phase.DESIGNED)
    assert task.metadata == {} and task.codex_model is None and task.codex_effort is None
    task2 = Task('t2', 'title', 'r', Path('d'), Phase.COMPLETE, {'x': 1}, 'm', 'max')
    assert task2.metadata == {'x': 1} and task2.codex_model == 'm' and task2.codex_effort == 'max'


def test_task_multi_agent_fields_default_none():
    task = Task('t', 'title', 'request', Path('design'), Phase.DESIGNED)
    assert task.origin_agent is None and task.target_agent is None and task.parent_task_id is None


def test_task_multi_agent_fields_settable():
    task = Task('t', 'title', 'request', Path('design'), Phase.IMPLEMENTING,
                origin_agent='claude_code', target_agent='codex', parent_task_id='task-1')
    assert task.origin_agent == 'claude_code' and task.target_agent == 'codex'
    assert task.parent_task_id == 'task-1'


def test_agent_state_values():
    assert AgentState.SUBMITTED.value == 'submitted'
    assert AgentState.WORKING.value == 'working'
    assert AgentState.INPUT_REQUIRED.value == 'input-required'
    assert AgentState.COMPLETED.value == 'completed'
    assert AgentState.FAILED.value == 'failed'
    assert AgentState.CANCELED.value == 'canceled'


def test_phase_to_agent_state_covers_all_phases():
    expected = {
        Phase.DESIGNED: AgentState.SUBMITTED,
        Phase.IMPLEMENTING: AgentState.WORKING,
        Phase.VERIFYING: AgentState.WORKING,
        Phase.REVIEWING: AgentState.INPUT_REQUIRED,
        Phase.FIXING: AgentState.WORKING,
        Phase.COMPLETE: AgentState.COMPLETED,
        Phase.FAILED: AgentState.FAILED,
    }
    for phase, state in expected.items():
        assert to_agent_state(phase) is state
