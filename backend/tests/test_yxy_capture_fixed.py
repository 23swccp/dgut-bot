import io
import json

import yxy_capture_fixed as capture


def test_capture_logs_headers_and_body_verbatim(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(capture, "log_fp", output)
    secret = "PRIVATE_TOKEN_SENTINEL_123456"
    body = json.dumps({
        "userId": 13245976,
        "studentId": "2024411080104",
        "cellphone": "13415602574",
        "status": 0,
    }, ensure_ascii=False)

    capture.log_headers("headers", {
        "Authorization": secret,
        "Cookie": f"AUTHORIZATION={secret}; token={secret}",
        "Content-Type": "application/json",
    })
    capture.log_body("body", body, "application/json")

    logged = output.getvalue()
    assert secret in logged
    assert "2024411080104" in logged
    assert "13415602574" in logged
    assert body in logged
    assert "已隐藏" not in logged and "已脱敏" not in logged


def test_request_event_logs_complete_url_without_redaction(monkeypatch):
    secret = "PRIVATE_TOKEN_SENTINEL_123456"
    tail = "pageSize=999&order=0&lang=zh&diagnosticMarker=complete-url-tail"
    url = (
        "https://lms.dgut.edu.cn/courseapi/users/13245976/activity?"
        "classroomId=438674&uaToken=" + secret + "&" + tail
    )
    lines = []
    monkeypatch.setattr(capture, "sessions", {"session": {"target_id": "target-id", "url": url}})
    monkeypatch.setattr(capture, "session_request_maps", {"session": {}})
    monkeypatch.setattr(capture, "log", lambda message="", fp=None: lines.append(str(message)))

    capture._handle_session_message("session", {
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": "request-1",
            "type": "Fetch",
            "request": {"url": url, "method": "GET", "headers": {"Authorization": secret}},
        },
    })

    logged = "\n".join(lines)
    assert len(url) > 120
    assert url in logged
    assert secret in logged
    assert logged.count("diagnosticMarker=complete-url-tail") >= 2


def test_static_filter_uses_url_extension_when_cdp_type_is_missing(monkeypatch):
    monkeypatch.setattr(capture, "SHOW_ALL_REQUESTS", False)
    assert not capture.should_log_http_resource(
        "https://application.dgut.edu.cn/static/js/chunk-1234567890abcdef.js", "",
    )
    assert not capture.should_log_http_resource(
        "https://lms.dgut.edu.cn/classroom/css/student-score.css?v=1", "Other",
    )
    assert capture.should_log_http_resource(
        "https://application.dgut.edu.cn/classroomapi/wisdomClassroom/student/classroomActivitys", "Fetch",
    )
    monkeypatch.setattr(capture, "SHOW_ALL_REQUESTS", True)
    assert capture.should_log_http_resource(
        "https://application.dgut.edu.cn/static/js/chunk.js", "Script",
    )


def test_websocket_payload_is_logged_verbatim_without_length_limit(monkeypatch):
    secret = "PRIVATE_TOKEN_SENTINEL_123456"
    payload = '420["authorize",{"appkey":"' + secret + '","data":"' + ("x" * 20_000) + '"}]'
    lines = []
    monkeypatch.setattr(capture, "sessions", {"session": {"target_id": "target-id", "url": ""}})
    monkeypatch.setattr(capture, "session_request_maps", {
        "session": {"ws-1": {"url": "wss://1hangzhou.goeasy.io/socket.io/?token=" + secret}},
    })
    monkeypatch.setattr(capture, "log", lambda message="", fp=None: lines.append(str(message)))

    capture._handle_session_message("session", {
        "method": "Network.webSocketFrameReceived",
        "params": {"requestId": "ws-1", "response": {"payloadData": payload}},
    })

    assert len(lines) == 1
    assert secret in lines[0]
    assert payload in lines[0]
    assert "已截断" not in lines[0]
