# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from pathlib import Path

from oslab.fuzz import FuzzCampaign, mutate


def test_seeded_mutation_and_resume(tmp_path: Path) -> None:
    assert mutate(b"abc", 7) == mutate(b"abc", 7)
    campaign = FuzzCampaign("fuzz-1", 11, tmp_path / "corpus")
    first = campaign.next_input([b"abc"])
    assert campaign.record_finding("fingerprint", {"input": first.hex()})
    assert not campaign.record_finding("fingerprint", {})
    checkpoint = tmp_path / "checkpoint.json"
    campaign.checkpoint(checkpoint)
    resumed = FuzzCampaign.resume(checkpoint)
    assert resumed.iterations == 1
    assert resumed.unique_findings == campaign.unique_findings
