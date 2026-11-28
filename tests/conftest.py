import pytest


@pytest.fixture(autouse=True)
def _no_user_advisor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user-level POWERPUFF_ADVISOR must not leak into the suite; tests that need it set it themselves."""
    monkeypatch.delenv("POWERPUFF_ADVISOR", raising=False)


@pytest.fixture(autouse=True)
def _sequential_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's queue scenarios assume one unit at a time; the default (3) is tested on its own."""
    monkeypatch.setenv("POWERPUFF_PARALLEL", "1")
    monkeypatch.setenv("POWERPUFF_POOL", "0")
