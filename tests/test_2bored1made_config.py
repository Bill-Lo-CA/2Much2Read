import pytest

from two_bored_one_made.config import Settings


def test_allowed_mention_ids_are_numeric_and_trimmed() -> None:
    assert Settings(discord_allowed_mention_ids="123, 456").allowed_mention_ids == {"123", "456"}

    with pytest.raises(ValueError, match="numeric"):
        Settings(discord_allowed_mention_ids="123, anyone")
