import json

import httpx
import respx

from newsletter_digest.ollama import OllamaClient


def valid_result() -> dict[str, object]:
    return {
        "source_id": "alphasignal",
        "newsletter_title": "AlphaSignal",
        "newsletter_date": None,
        "overview_zh_tw": "本日摘要",
        "items": [
            {
                "title": "Model release",
                "category": "AI_MODEL",
                "summary_zh_tw": "發布新模型。",
                "why_it_matters_zh_tw": "可改善工作流程。",
                "source_url": "https://example.com/a",
                "importance": 8,
                "confidence": 0.9,
                "tags": ["AI Model"],
            }
        ],
        "truncated_input": False,
    }


@respx.mock
def test_repairs_invalid_schema_once() -> None:
    route = respx.post("http://127.0.0.1:11434/api/chat").mock(
        side_effect=[
            httpx.Response(200, json={"message": {"content": "not json"}}),
            httpx.Response(200, json={"message": {"content": json.dumps(valid_result())}}),
        ]
    )
    result = OllamaClient().extract("alphasignal", "News https://example.com/a")
    assert result.items[0].importance == 8
    assert route.call_count == 2
    request_payload = json.loads(route.calls[0].request.content)
    assert "maxLength" not in json.dumps(request_payload["format"])
    repair_payload = json.loads(route.calls[1].request.content)
    assert repair_payload["messages"][-2] == {"role": "assistant", "content": "not json"}


@respx.mock
def test_normalizes_trusted_fields_and_limits_items() -> None:
    model_result = valid_result()
    model_result["source_id"] = "wrong-source"
    model_result["truncated_input"] = False
    model_result["items"] = [*model_result["items"], *model_result["items"]]
    respx.post("http://127.0.0.1:11434/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": json.dumps(model_result)}})
    )

    result = OllamaClient().extract(
        "alphasignal",
        "News https://example.com/a",
        truncated=True,
        max_items=1,
    )

    assert result.source_id == "alphasignal"
    assert result.truncated_input is True
    assert len(result.items) == 1


@respx.mock
def test_accepts_source_url_from_markdown_link() -> None:
    model_result = valid_result()
    model_result["items"][0]["source_url"] = "https://example.com"  # type: ignore[index]
    route = respx.post("http://127.0.0.1:11434/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": json.dumps(model_result)}})
    )

    result = OllamaClient().extract("alphasignal", "Read [article](https://example.com).")

    assert str(result.items[0].source_url) == "https://example.com/"
    assert route.call_count == 1
