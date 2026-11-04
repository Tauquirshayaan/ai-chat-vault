"""Smoke test: the window builds, every page opens, and it closes cleanly."""

import sys
import unittest
from unittest import mock


class GuiSmokeTest(unittest.TestCase):
    def setUp(self):
        self.stdout, self.stderr = sys.stdout, sys.stderr
        try:
            import tkinter
            root = tkinter.Tk()
            root.destroy()
        except Exception as error:  # no display (e.g. headless Linux CI) or no tkinter
            self.skipTest(f"no display: {error}")

    def tearDown(self):
        sys.stdout, sys.stderr = self.stdout, self.stderr

    def test_every_page_opens(self):
        from chatvault import gui
        with mock.patch.object(gui, "running_apps", return_value=[]), \
                mock.patch.object(gui.BackupPage, "rescan"), \
                mock.patch.object(gui, "_pick_backup_values", return_value=[]):
            app = gui.App(start_page="about")
            try:
                for key, _label, _cls in app.PAGES:
                    app.show_page(key)
                    app.update()
                    self.assertEqual(app.current, key)
                app.toggle_details()
                app.log("hello\n")
                app.update()
                self.assertIn("hello", app.log_text.get("1.0", "end"))
                page = app.pages["check"]
                page.result.show("success", "done", "detail", [("Act", lambda: None)])
                app.update()
            finally:
                app.destroy()


if __name__ == "__main__":
    unittest.main()
