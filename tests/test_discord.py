import json

import httpx
import pytest
import respx

from two_read_runtime.discord import chunk_text, deliver, deliver_resumable, parse_message_ids


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, []), ("[]", []), ('["one", "two"]', ["one", "two"])],
)
def test_parses_stored_message_ids(value: object, expected: list[str]) -> None:
    assert parse_message_ids(value) == expected


@pytest.mark.parametrize("value", ["not json", "{}", "[1]"])
def test_rejects_corrupt_stored_message_ids(value: str) -> None:
    with pytest.raises(ValueError, match="DISCORD_MESSAGE_IDS_CORRUPT"):
        parse_message_ids(value)


def test_chunk_text_balances_long_fenced_blocks() -> None:
    content = "```text\n" + "\n".join(f"09:{index:02d} | Event {index}" for index in range(20)) + "\n```"

    chunks = chunk_text(content, limit=80)

    assert len(chunks) > 1
    assert all(len(chunk) <= 80 for chunk in chunks)
    assert all(chunk.startswith(f"({index}/{len(chunks)}) ```text\n") for index, chunk in enumerate(chunks, 1))
    assert all(chunk.endswith("\n```") and chunk.count("```") == 2 for chunk in chunks)
    assert "\n".join("\n".join(chunk.splitlines()[1:-1]) for chunk in chunks) == content.removeprefix("```text\n").removesuffix(
        "\n```"
    )


def test_chunk_text_keeps_fences_when_links_follow() -> None:
    body = "\n".join(f"09:{index:02d} | Event {index}" for index in range(20))
    content = f"```text\n{body}\n```\n<https://calendar.example/event>"

    chunks = chunk_text(content, limit=80)
    fenced_chunks = chunks[:-1]

    assert len(chunks) > 2
    assert all(len(chunk) <= 80 for chunk in chunks)
    assert all(chunk.startswith(f"({index}/{len(chunks)}) ```text\n") for index, chunk in enumerate(fenced_chunks, 1))
    assert all(chunk.endswith("\n```") and chunk.count("```") == 2 for chunk in fenced_chunks)
    assert chunks[-1].endswith("<https://calendar.example/event>")
    assert "\n".join("\n".join(chunk.splitlines()[1:-1]) for chunk in fenced_chunks) == body


def test_chunk_text_keeps_long_link_footers_outside_fences() -> None:
    links = "\n".join(f"<https://calendar.example/event/{index}>" for index in range(100))
    chunks = chunk_text(f"```text\n09:00 | Event\n```\n{links}")

    assert all(len(chunk) <= 2000 for chunk in chunks)
    assert chunks[0].endswith("\n```")
    assert "\n".join(chunk.removeprefix(f"({index}/{len(chunks)}) ") for index, chunk in enumerate(chunks[1:], 2)) == links


@respx.mock
def test_disables_mentions() -> None:
    route = respx.post("https://discord.example/webhook").mock(return_value=httpx.Response(200, json={"id": "123"}))
    assert deliver("https://discord.example/webhook", "hello", "2much2read") == ["123"]
    assert route.calls[0].request.read()
    assert b'"allowed_mentions":{"parse":[]}' in route.calls[0].request.content


@respx.mock
def test_allows_only_explicit_user_mentions() -> None:
    route = respx.post("https://discord.example/webhook").mock(return_value=httpx.Response(200, json={"id": "123"}))

    assert deliver("https://discord.example/webhook", "<@123> hello", "2bored1made", allowed_user_ids=["123"]) == ["123"]

    assert json.loads(route.calls[0].request.content)["allowed_mentions"] == {"parse": [], "users": ["123"]}


@respx.mock
def test_keeps_many_mention_tokens_intact() -> None:
    user_ids = [f"{index:019d}" for index in range(87)]
    route = respx.post("https://discord.example/webhook").mock(return_value=httpx.Response(200, json={"id": "123"}))

    assert deliver(
        "https://discord.example/webhook",
        "body",
        "2bored1made",
        allowed_user_ids=user_ids,
        mention_user_ids=user_ids,
    ) == ["123", "123"]

    contents = [str(json.loads(call.request.content)["content"]) for call in route.calls]
    assert all(len(content) <= 2000 for content in contents)
    assert {token for content in contents for token in content.split() if token.startswith("<@") and token.endswith(">")} == {
        f"<@{user_id}>" for user_id in user_ids
    }


@respx.mock
def test_resumes_after_saved_chunk_progress() -> None:
    route = respx.post("https://discord.example/webhook").mock(return_value=httpx.Response(200, json={"id": "2"}))
    progress: list[list[str]] = []

    message_ids = deliver(
        "https://discord.example/webhook",
        "x" * 3000,
        "2much2read",
        message_ids=["1"],
        on_progress=progress.append,
    )

    assert message_ids == ["1", "2"]
    assert progress == [["1", "2"]]
    assert route.call_count == 1


def test_deliver_resumable_restores_checkpoint_and_records_success() -> None:
    progress: list[list[str]] = []
    delivered: list[list[str]] = []

    def sender(
        webhook_url: str,
        content: str,
        username: str,
        message_ids: list[str] | None,
        on_progress: object,
    ) -> list[str]:
        assert (webhook_url, content, username, message_ids) == (
            "https://discord.example/webhook",
            "content",
            "2much2read",
            ["one"],
        )
        assert callable(on_progress)
        on_progress(["one", "two"])
        return ["one", "two"]

    assert deliver_resumable(
        "https://discord.example/webhook", "content", "2much2read", '["one"]', progress.append, delivered.append, sender=sender
    ) == ["one", "two"]
    assert progress == [["one", "two"]]
    assert delivered == [["one", "two"]]
