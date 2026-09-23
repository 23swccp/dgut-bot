import subprocess
from pathlib import Path

from scripts.publish_gitee_release import DEFAULT_ASSET_NAME, _curl_upload, publish_release


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text or ("json" if data is not None else "")
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = {}
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._call("DELETE", url, **kwargs)


def test_missing_token_skips_without_reading_asset():
    assert (
        publish_release(
            token="",
            owner="owner",
            repo="repo",
            tag="v1.0.0",
            target="main",
            asset=Path("missing.exe"),
        )
        == ""
    )


def test_creates_release_and_uploads_installer(tmp_path):
    asset = tmp_path / "Setup.exe"
    asset.write_bytes(b"installer")
    session = FakeSession(
        [
            FakeResponse(404, {"message": "not found"}),
            FakeResponse(201, {"id": 12}),
            FakeResponse(200, []),
            FakeResponse(201, {"browser_download_url": "https://example/setup.exe"}),
        ]
    )

    result = publish_release(
        token="secret",
        owner="owner",
        repo="repo",
        tag="v1.0.0",
        target="main",
        asset=asset,
        session=session,
    )

    assert result == "https://example/setup.exe"
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET", "POST"]
    upload = session.calls[-1][2]["files"]["file"]
    assert upload[0] == DEFAULT_ASSET_NAME


def test_creates_release_when_gitee_returns_null_for_missing_tag(tmp_path):
    asset = tmp_path / "Setup.exe"
    asset.write_bytes(b"installer")
    session = FakeSession(
        [
            FakeResponse(200, None, "null"),
            FakeResponse(201, {"id": 12}),
            FakeResponse(200, []),
            FakeResponse(201, {}),
        ]
    )

    publish_release(
        token="secret",
        owner="owner",
        repo="repo",
        tag="v1.0.0",
        target="main",
        asset=asset,
        session=session,
    )

    assert [call[0] for call in session.calls] == ["GET", "POST", "GET", "POST"]


def test_replaces_same_named_attachment(tmp_path):
    asset = tmp_path / "Setup.exe"
    asset.write_bytes(b"new installer")
    session = FakeSession(
        [
            FakeResponse(200, {"id": 12}),
            FakeResponse(200, [{"id": 34, "name": DEFAULT_ASSET_NAME}]),
            FakeResponse(200, {}),
            FakeResponse(201, {"browser_download_url": "https://example/new.exe"}),
        ]
    )

    publish_release(
        token="secret",
        owner="owner",
        repo="repo",
        tag="v1.0.0",
        target="main",
        asset=asset,
        session=session,
    )

    assert [call[0] for call in session.calls] == ["GET", "GET", "DELETE", "POST"]
    assert session.calls[2][1].endswith("/attach_files/34")


def test_curl_upload_uses_configured_timeout_without_exposing_token(tmp_path, monkeypatch):
    asset = tmp_path / "Setup.exe"
    asset.write_bytes(b"installer")
    monkeypatch.setenv("GITEE_UPLOAD_MAX_TIME", "3600")

    def fake_run(args, **kwargs):
        assert args[args.index("--max-time") + 1] == "3600"
        assert "secret" not in " ".join(args)
        assert "Authorization: Bearer secret" in kwargs["input"]
        Path(args[args.index("--output") + 1]).write_text(
            '{"browser_download_url":"https://example/setup.exe"}', encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="HTTP 201", stderr="")

    monkeypatch.setattr("scripts.publish_gitee_release.subprocess.run", fake_run)
    result = _curl_upload("https://example/upload", "secret", asset, DEFAULT_ASSET_NAME)
    assert result["browser_download_url"] == "https://example/setup.exe"
