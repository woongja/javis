from harness.cli import main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "harness" in capsys.readouterr().out
