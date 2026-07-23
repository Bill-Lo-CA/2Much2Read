import pytest

from two_much_two_read.config import Source
from two_much_two_read.subscription_operations import subscription_candidates, subscription_identity


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"list-id": "<weekly.example.com>"}, ("weekly.example.com", "weekly.example.com", "weekly.example.com")),
        (
            {"from": "Digest <news@example.com>", "x-emailoctopus-list-id": "opaque"},
            ("emailoctopus:6d229884c1268bb0", None, "Digest"),
        ),
        ({"from": "Digest <news@example.com>"}, ("from:digest <news@example.com>", None, "Digest")),
    ],
)
def test_subscription_identity_uses_stable_header_precedence(
    headers: dict[str, str], expected: tuple[str, str | None, str]
) -> None:
    assert subscription_identity(headers) == expected


def test_subscription_candidates_handle_shared_senders_and_source_id_collisions() -> None:
    def message(list_id: str) -> dict[str, object]:
        return {
            "labelIds": [],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Digest <news@example.com>"},
                    {"name": "List-ID", "value": f"<{list_id}>"},
                    {"name": "List-Unsubscribe", "value": "<mailto:unsubscribe@example.com>"},
                ]
            },
        }

    configured = [Source(id="alpha-example-com", name="Existing", gmail_query="from:existing@example.com")]
    candidates = subscription_candidates([message("alpha.example.com"), message("beta.example.com")], configured, set(), {})

    assert [candidate.id for candidate in candidates] == ["alpha-example-com-2", "beta-example-com"]
    assert all(candidate.query_ambiguous for candidate in candidates)
    assert all(candidate.base_query == 'from:news@example.com from:"Digest"' for candidate in candidates)


def test_subscription_candidates_reserve_non_gmail_source_ids() -> None:
    message = {
        "labelIds": [],
        "payload": {
            "headers": [
                {"name": "From", "value": "Digest <news@example.com>"},
                {"name": "List-ID", "value": "<hn-best>"},
                {"name": "List-Unsubscribe", "value": "<mailto:unsubscribe@example.com>"},
            ]
        },
    }

    candidates = subscription_candidates([message], [], set(), {}, {"hn-best"})

    assert candidates[0].id == "hn-best-2"
