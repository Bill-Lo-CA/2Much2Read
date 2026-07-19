import httpx
import respx

from two_read_runtime.discord import chunk_text, deliver


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
