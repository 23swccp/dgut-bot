"""已移除的登录命令不再创建会话或访问凭据。"""

import pytest

from dgutbot.app import commands


@pytest.mark.parametrize("command", [
    "get_independent_login_status", "list_independent_login_sources",
    "login_independent_account", "import_independent_login",
    "clear_independent_login", "clear_independent_account",
])
def test_removed_login_commands_are_rejected(command):
    assert commands.handle(command, {}) == {"ok": False, "error": f"未知命令：{command}"}
    assert not hasattr(commands.backend, "independent_login")
