from harness.paths import env_value


def test_env_value_parses_dotenv(tmp_path, monkeypatch):
    for key in ("BARE", "SINGLE_QUOTED", "DOUBLE_QUOTED", "MISSING"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "  # BARE=ignored\n"
        "  BARE =  first=second  \n"
        "SINGLE_QUOTED = 'value with spaces' \n"
        'DOUBLE_QUOTED = "\'inner quotes\'" \n'
        "# MISSING=ignored\n",
        encoding="utf-8",
    )

    assert env_value(tmp_path, "BARE") == "first=second"
    assert env_value(tmp_path, "SINGLE_QUOTED") == "value with spaces"
    assert env_value(tmp_path, "DOUBLE_QUOTED") == "'inner quotes'"
    assert env_value(tmp_path, "MISSING") is None
