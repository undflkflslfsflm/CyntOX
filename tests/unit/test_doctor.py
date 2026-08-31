from oslab.doctor import _normalize_command_text


def test_command_text_normalization_removes_windows_nuls() -> None:
    assert _normalize_command_text("W\x00S\x00L\x00 \x00v\x002\x00\r\n") == "WSL v2"
