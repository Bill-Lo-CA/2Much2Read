import json

import httpx
import pytest
import respx

from two_read_runtime import discord
from two_read_runtime.discord import (
    DiscordDeliveryError,
    chunk_text,
    configured_destinations,
    deliver,
    deliver_resumable,
    parse_message_ids,
)


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


@respx.mock
def test_bot_delivery_uses_v10_without_enabling_mentions() -> None:
    destination = configured_destinations("bot", "", "secret-token", "123")[0]
    route = respx.post("https://discord.com/api/v10/channels/123/messages").mock(
        return_value=httpx.Response(200, json={"id": "456"})
    )

    assert deliver(destination, "hello", "ignored") == ["456"]
    assert route.calls[0].request.headers["Authorization"] == "Bot secret-token"
    assert json.loads(route.calls[0].request.content) == {"content": "hello", "allowed_mentions": {"parse": []}}


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(401, "DISCORD_BOT_UNAUTHORIZED"), (403, "DISCORD_BOT_FORBIDDEN"), (404, "DISCORD_BOT_CHANNEL_NOT_FOUND")],
)
@respx.mock
def test_bot_delivery_classifies_configuration_errors(status_code: int, code: str) -> None:
    destination = configured_destinations("bot", "", "secret-token", "123")[0]
    respx.post("https://discord.com/api/v10/channels/123/messages").mock(return_value=httpx.Response(status_code))

    with pytest.raises(DiscordDeliveryError, match=code) as error:
        deliver(destination, "hello", "ignored")

    assert "secret-token" not in str(error.value)


def test_uses_environment_proxy_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    client_options: list[dict[str, object]] = []

    class Client:
        def __init__(self, **options: object) -> None:
            client_options.append(options)

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def post(self, *args: object, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"id": "123"})

    monkeypatch.setattr(discord.httpx, "Client", Client)

    response = discord._request(configured_destinations("bot", "", "token", "123")[0], "hello", "ignored", {"parse": []})

    assert response.status_code == 200
    assert client_options == [{"timeout": 30}]


def test_does_not_retry_ambiguous_post_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def request(*args: object) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(discord, "_request", request)

    with pytest.raises(DiscordDeliveryError, match="DISCORD_DELIVERY_FAILED"):
        deliver(configured_destinations("bot", "", "token", "123")[0], "hello", "ignored")

    assert attempts == 1


@pytest.mark.parametrize(
    ("mode", "webhook_url", "bot_token", "channel_id"),
    [
        ("webhook", "", "", ""),
        ("bot", "", "", "123"),
        ("bot", "", "token", "bad"),
        ("both", "url", "token", "123"),
        ("invalid", "url", "token", "123"),
    ],
)
def test_rejects_incomplete_delivery_destinations(mode: str, webhook_url: str, bot_token: str, channel_id: str) -> None:
    with pytest.raises(DiscordDeliveryError, match="DISCORD_CONFIG_INVALID"):
        configured_destinations(mode, webhook_url, bot_token, channel_id)
