"""Verify service migration and uninstall without touching the real user manager."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install


class ServiceInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.config = self.home / ".config"
        self.unit = self.config / "systemd/user" / install.SERVICE
        self.autostart = self.config / "autostart/ai-usage-tray.desktop"
        self.settings = self.home / ".claude/settings.json"
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({"theme": "dark"}))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True))
        self.enterContext(patch("install.shutil.which", return_value="/usr/bin/systemctl"))
        self.run = self.enterContext(patch("install.subprocess.run"))
        self.enterContext(patch("builtins.print"))

    def invoke(self, *args):
        with patch.object(sys, "argv", ["install.py", *args]):
            install.main()

    def test_migrates_autostart_and_starts_only_through_systemd(self):
        self.autostart.parent.mkdir(parents=True)
        self.autostart.write_text("old autostart")
        with patch.dict(os.environ, {"DISPLAY": ":1", "SECRET_TOKEN": "do-not-persist"}):
            self.invoke("--service", "--start")
        self.assertFalse(self.autostart.exists())
        unit = self.unit.read_text()
        self.assertIn("WantedBy=graphical-session.target", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("do-not-persist", unit)
        self.assertNotIn("DISPLAY", unit)
        self.run.assert_any_call(
            ["systemctl", "--user", "import-environment", "DISPLAY"], check=True
        )
        self.run.assert_any_call(
            ["systemctl", "--user", "restart", install.SERVICE], check=True
        )
        desktop = self.home / ".local/share/applications/ai-usage-tray.desktop"
        self.assertIn("Exec=systemctl --user start", desktop.read_text())

    def test_reinstall_preserves_service_and_uninstall_stops_it(self):
        self.invoke("--service")
        self.invoke()
        self.assertFalse(self.autostart.exists())
        self.assertTrue(self.unit.exists())
        self.invoke("--uninstall")
        self.run.assert_any_call(
            ["systemctl", "--user", "disable", "--now", install.SERVICE], check=True
        )
        self.assertFalse(self.unit.exists())
        self.assertFalse((self.home / ".local/bin/ai-usage-tray").exists())
        self.assertEqual(json.loads(self.settings.read_text()), {"theme": "dark"})

    def test_systemd_paths_escape_specifiers_and_variables(self):
        path = Path('/tmp/space " %name $HOME/ai-usage-tray')
        unit = install.service_unit(path)
        self.assertIn('ExecStart="/tmp/space \\" %%name $$HOME/ai-usage-tray"', unit)
        with patch.dict(os.environ, {"CODEX_HOME": "/tmp/%name/$literal"}):
            self.assertIn(
                'Environment="CODEX_HOME=/tmp/%%name/$literal"', install.service_unit(path)
            )


if __name__ == "__main__":
    unittest.main()
