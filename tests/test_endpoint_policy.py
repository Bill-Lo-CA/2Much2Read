import pytest

from two_read_runtime.endpoint_policy import (
    DISCORD_WEBHOOK_INVALID,
    OLLAMA_ENDPOINT_INVALID,
    OLLAMA_INSECURE_REMOTE_NOT_ALLOWED,
    OLLAMA_REMOTE_NOT_ALLOWED,
    EndpointPolicyError,
    classify_ollama_endpoint,
    validate_discord_webhook,
    validate_ollama_endpoint,
)


@pytest.mark.parametrize(
    "value",
    ["http://127.0.0.1:11434", "http://[::1]:11434", "http://localhost", "https://localhost:11434"],
)
def test_allows_local_ollama_endpoints(value: str) -> None:
    endpoint = validate_ollama_endpoint(value)

    assert endpoint.classification == "local"
    assert classify_ollama_endpoint(value) == "local"


def test_remote_ollama_requires_https_and_opt_in() -> None:
    with pytest.raises(EndpointPolicyError, match=OLLAMA_REMOTE_NOT_ALLOWED):
        validate_ollama_endpoint("https://ollama.example")
    with pytest.raises(EndpointPolicyError, match=OLLAMA_INSECURE_REMOTE_NOT_ALLOWED):
        validate_ollama_endpoint("http://ollama.example:11434", allow_remote=True)

    endpoint = validate_ollama_endpoint("https://ollama.example", allow_remote=True)

    assert endpoint.classification == "remote_https"


@pytest.mark.parametrize(
    "value",
    [
        "not a URL",
        "http://user:password@127.0.0.1:11434",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:11434#fragment",
    ],
)
def test_rejects_invalid_ollama_endpoints(value: str) -> None:
    with pytest.raises(EndpointPolicyError, match=OLLAMA_ENDPOINT_INVALID):
        validate_ollama_endpoint(value)


@pytest.mark.parametrize(
    "host",
    ["discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"],
)
def test_allows_official_discord_webhooks(host: str) -> None:
    value = f"https://{host}/api/webhooks/123456789012345678/test-token"

    assert validate_discord_webhook(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "http://discord.com/api/webhooks/123/secret-token",
        "https://example.com/api/webhooks/123/secret-token",
        "https://user:password@discord.com/api/webhooks/123/secret-token",
        "https://discord.com:444/api/webhooks/123/secret-token",
        "https://discord.com/api/webhooks/123",
        "https://discord.com/api/webhooks/123/secret-token#fragment",
    ],
)
def test_rejects_invalid_discord_webhooks_without_leaking_tokens(value: str) -> None:
    with pytest.raises(EndpointPolicyError, match=DISCORD_WEBHOOK_INVALID) as error:
        validate_discord_webhook(value)

    assert "secret-token" not in str(error.value)
