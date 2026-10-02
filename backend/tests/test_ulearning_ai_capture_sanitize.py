from tools.ulearning_ai_capture_sanitize import response_body_shapes


def test_response_body_shapes_never_include_values():
    endpoint = "https://aijx.dgut.edu.cn/api/assistant/instruction/byAssistantId"
    lines = [
        f"[12:00:00]   [target] << 响应正文 {endpoint} (80 chars)",
        '[12:00:00] {"prompt":"do-not-leak","enabled":true,"items":[{"id":123}]}',
    ]
    result = response_body_shapes(lines, endpoint)
    shown = repr(result)
    assert "do-not-leak" not in shown
    assert result == [{
        "prompt": {"type": "string", "length": 11},
        "enabled": "boolean",
        "items": {
            "type": "array",
            "length": 1,
            "item": {"id": "number"},
        },
    }]
