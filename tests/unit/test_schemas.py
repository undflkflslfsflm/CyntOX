# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from oslab.schemas import Outcome, ResultEnvelope


def test_result_envelope_rejects_reverse_time() -> None:
    start = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ResultEnvelope(
            status=Outcome.PASS,
            run_id="run",
            tool="test.run",
            started_at=start,
            ended_at=start - timedelta(seconds=1),
            duration_ms=0,
        )


def test_outcomes_include_evaluator_classifications() -> None:
    assert Outcome.EVALUATOR_EXPLOIT != Outcome.FAIL
    assert Outcome.INVALID_SOLUTION != Outcome.TEST_ERROR
