# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from oslab.debug import CrashEvidence, fingerprint_crash, normalize_log


def test_unstable_addresses_do_not_change_fingerprint() -> None:
    one = CrashEvidence(
        "crash", "mm", "fault at 0x12345678", ("0xffffffffaabbccdd in foo",), "abc", "def"
    )
    two = CrashEvidence(
        "crash", "mm", "fault at 0x87654321", ("0x1111111122222222 in foo",), "abc", "def"
    )
    assert fingerprint_crash(one) == fingerprint_crash(two)
    assert "<addr>" in normalize_log(one.assertion or "")
