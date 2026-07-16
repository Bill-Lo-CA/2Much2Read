import httpx
import respx

from common.discord import deliver


@respx.mock
def test_disables_mentions() -> None:
    route = respx.post("https://discord.example/webhook").mock(return_value=httpx.Response(200, json={"id": "123"}))
    assert deliver("https://discord.example/webhook", "hello", "2much2read") == ["123"]
    assert route.calls[0].request.read()
    assert b'"allowed_mentions":{"parse":[]}' in route.calls[0].request.content
