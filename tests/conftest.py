import pytest


@pytest.fixture(autouse=True)
def _no_user_advisor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user-level POWERPUFF_ADVISOR must not leak into the suite; tests that need it set it themselves."""
    monkeypatch.delenv("POWERPUFF_ADVISOR", raising=False)
