"""更新实验室的故障契约直接通过产品适配层与原生 SDK 验证。"""
import json
from pathlib import Path

import pytest

from tools.update_lab import CASES, LabManager, prepare, run_case


@pytest.mark.parametrize("case", CASES)
def test_update_lab_scenario(case, tmp_path):
    result = run_case(case, tmp_path)
    assert result["passed"], result
    report = json.loads((Path(result["root"]) / "report.json").read_text(encoding="utf-8"))
    assert report["passed"]
    assert report["requests"]
    if case in {"timeout", "missing", "invalid", "truncated", "checksum"}:
        assert report["stages"][0]["stage"].startswith("expected-")


def test_network_lab_cannot_install_and_rejects_external_source(tmp_path):
    root = tmp_path / "fixture"
    prepare(root)
    manager = LabManager(root, "http://127.0.0.1:12345")
    assert not manager.install()["ok"]
    with pytest.raises(AssertionError, match="网络场景"):
        manager._install_worker()
    for origin in ("https://example.com", "http://127.0.0.1:80@evil.test", "http://127.0.0.1:80/path"):
        with pytest.raises(AssertionError, match="测试更新源"):
            LabManager(root, origin)
