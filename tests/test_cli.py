import pytest

from relay.cli import main


def test_cli_plan_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["plan", "--help"])
    assert exc.value.code == 0


def test_cli_ask_requires_message() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["ask", "--no-dotenv"])
    assert exc.value.code == 2