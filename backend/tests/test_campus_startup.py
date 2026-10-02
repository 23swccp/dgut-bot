"""校园网开机登录的离线回归测试。"""

import unittest
from unittest.mock import patch

from dgutbot.app import campus_startup


class CampusStartupTests(unittest.TestCase):
    def test_frozen_startup_command_uses_silent_mode(self):
        with patch.object(campus_startup, "is_frozen", return_value=True), patch.object(
            campus_startup.sys, "executable", r"C:\Apps\DgutBot\current\dgut-bot.exe",
        ):
            command = campus_startup.campus_startup_command()
        self.assertIn("dgut-bot.exe\" --campus-login", command)

    def test_source_startup_command_uses_public_pythonw_alias(self):
        with patch.object(campus_startup, "is_frozen", return_value=False), patch.object(
            campus_startup.shutil, "which", return_value=r"C:\Users\tester\WindowsApps\pythonw.exe",
        ):
            command = campus_startup.campus_startup_command()
        self.assertTrue(command.startswith('"C:\\Users\\tester\\WindowsApps\\pythonw.exe"'))
        self.assertIn("campus_login_startup.pyw", command)

    def test_windows_startup_immediately_opens_fixed_login_url(self):
        with patch.object(campus_startup.os, "name", "nt"), patch.object(
            campus_startup.os, "startfile", create=True,
        ) as startfile:
            self.assertTrue(campus_startup.open_campus_login())
        startfile.assert_called_once_with(campus_startup.DGUT_LOGIN_URL)


if __name__ == "__main__":
    unittest.main()
