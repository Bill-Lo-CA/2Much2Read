import httpx
import respx

from common.discord import chunk_text, deliver


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


@respx.mock
def test_disables_mentions() -> None:
    route = respx.post("https://discord.example/webhook").mock(return_value=httpx.Response(200, json={"id": "123"}))
    assert deliver("https://discord.example/webhook", "hello", "2much2read") == ["123"]
    assert route.calls[0].request.read()
    assert b'"allowed_mentions":{"parse":[]}' in route.calls[0].request.content
