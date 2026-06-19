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
