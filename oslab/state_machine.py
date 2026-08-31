from __future__ import annotations

from enum import StrEnum


class SupervisorState(StrEnum):
    SELECT_TASK = "SELECT_TASK"
    PREPARE_WORKTREE = "PREPARE_WORKTREE"
    BUILD_BASELINE = "BUILD_BASELINE"
    BOOT_BASELINE = "BOOT_BASELINE"
    GENERATE_HYPOTHESES = "GENERATE_HYPOTHESES"
    RANK_HYPOTHESES = "RANK_HYPOTHESES"
    GENERATE_TEST = "GENERATE_TEST"
    EXECUTE_TEST = "EXECUTE_TEST"
    TRIAGE_RESULT = "TRIAGE_RESULT"
    REPRODUCE = "REPRODUCE"
    MINIMIZE = "MINIMIZE"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"
    PROPOSE_PATCHES = "PROPOSE_PATCHES"
    VERIFY_TARGETED_TEST = "VERIFY_TARGETED_TEST"
    RUN_REGRESSION = "RUN_REGRESSION"
    ADVERSARIAL_VERIFY = "ADVERSARIAL_VERIFY"
    RECORD_FINDING = "RECORD_FINDING"
    CLEANUP = "CLEANUP"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


TERMINAL = {
    SupervisorState.COMPLETE,
    SupervisorState.BLOCKED,
    SupervisorState.CANCELLED,
}

TRANSITIONS: dict[SupervisorState, frozenset[SupervisorState]] = {
    SupervisorState.SELECT_TASK: frozenset(
        {SupervisorState.PREPARE_WORKTREE, SupervisorState.BLOCKED, SupervisorState.CANCELLED}
    ),
    SupervisorState.PREPARE_WORKTREE: frozenset(
        {SupervisorState.BUILD_BASELINE, SupervisorState.BLOCKED, SupervisorState.CANCELLED}
    ),
    SupervisorState.BUILD_BASELINE: frozenset(
        {SupervisorState.BOOT_BASELINE, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.BOOT_BASELINE: frozenset(
        {SupervisorState.GENERATE_HYPOTHESES, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.GENERATE_HYPOTHESES: frozenset(
        {SupervisorState.RANK_HYPOTHESES, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.RANK_HYPOTHESES: frozenset(
        {SupervisorState.GENERATE_TEST, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.GENERATE_TEST: frozenset(
        {SupervisorState.EXECUTE_TEST, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.EXECUTE_TEST: frozenset(
        {SupervisorState.TRIAGE_RESULT, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.TRIAGE_RESULT: frozenset(
        {SupervisorState.REPRODUCE, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.REPRODUCE: frozenset(
        {SupervisorState.MINIMIZE, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.MINIMIZE: frozenset(
        {SupervisorState.ROOT_CAUSE_ANALYSIS, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.ROOT_CAUSE_ANALYSIS: frozenset(
        {SupervisorState.PROPOSE_PATCHES, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.PROPOSE_PATCHES: frozenset(
        {SupervisorState.VERIFY_TARGETED_TEST, SupervisorState.CLEANUP, SupervisorState.CANCELLED}
    ),
    SupervisorState.VERIFY_TARGETED_TEST: frozenset(
        {
            SupervisorState.RUN_REGRESSION,
            SupervisorState.PROPOSE_PATCHES,
            SupervisorState.CLEANUP,
            SupervisorState.CANCELLED,
        }
    ),
    SupervisorState.RUN_REGRESSION: frozenset(
        {
            SupervisorState.ADVERSARIAL_VERIFY,
            SupervisorState.PROPOSE_PATCHES,
            SupervisorState.CLEANUP,
            SupervisorState.CANCELLED,
        }
    ),
    SupervisorState.ADVERSARIAL_VERIFY: frozenset(
        {
            SupervisorState.RECORD_FINDING,
            SupervisorState.PROPOSE_PATCHES,
            SupervisorState.CLEANUP,
            SupervisorState.CANCELLED,
        }
    ),
    SupervisorState.RECORD_FINDING: frozenset({SupervisorState.CLEANUP, SupervisorState.CANCELLED}),
    SupervisorState.CLEANUP: frozenset(
        {SupervisorState.COMPLETE, SupervisorState.BLOCKED, SupervisorState.CANCELLED}
    ),
    SupervisorState.COMPLETE: frozenset(),
    SupervisorState.BLOCKED: frozenset(),
    SupervisorState.CANCELLED: frozenset(),
}


def validate_transition(source: SupervisorState, destination: SupervisorState) -> None:
    if destination not in TRANSITIONS[source]:
        raise ValueError(f"invalid supervisor transition: {source} -> {destination}")


def reaches_terminal(start: SupervisorState) -> bool:
    visited: set[SupervisorState] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current in TERMINAL:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(TRANSITIONS[current] - visited)
    return False
