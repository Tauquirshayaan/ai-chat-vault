import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from unittest import mock

from chatvault import archive
from chatvault.archive import BackupError


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mtime:
        os.utime(path, (mtime, mtime))


def make_env(base):
    env = {"HOME": os.path.join(base, "home"), "APPCFG": os.path.join(base, "appdata")}
    os.makedirs(env["HOME"])
    os.makedirs(env["APPCFG"])
    return env


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = self.temp.name
        self.env = make_env(os.path.join(self.base, "old"))
        h, a = self.env["HOME"], self.env["APPCFG"]
        write(os.path.join(h, ".gemini", "antigravity-ide", "brain", "c1", "plan.md"),
              "# Plan\n", mtime=1_700_000_000)
        write(os.path.join(h, ".gemini", "antigravity-ide", "conversations", "c1.pb"), "opaque")
        write(os.path.join(h, ".gemini", "antigravity-browser-profile", "Cookies"), "secret")
        write(os.path.join(h, ".gemini", "antigravity-ide", "browser_recordings", "r.webm"), "video")
        write(os.path.join(h, ".codex", "sessions", "2026", "01", "rollout-x.jsonl"), "{}\n")
        write(os.path.join(h, ".codex", "auth.json"), '{"token": "x"}')
        write(os.path.join(h, ".codex", "cache", "big.bin"), "cache")
        write(os.path.join(h, ".claude", "projects", "g--p", "s.jsonl"), "{}\n")
        write(os.path.join(h, ".claude", ".credentials.json"), "{}")
        write(os.path.join(h, ".claude.json"), '{"mcpServers": {}}')
        # A live WAL database whose newest row is still only in the -wal file.
        self.state = os.path.join(a, "Antigravity IDE", "User", "globalStorage", "state.vscdb")
        os.makedirs(os.path.dirname(self.state))
        self.live = sqlite3.connect(self.state)
        self.live.execute("PRAGMA journal_mode=WAL")
        self.live.execute("PRAGMA wal_autocheckpoint=0")
        self.live.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        self.live.execute("INSERT INTO ItemTable VALUES ('k', 'committed-in-wal')")
        self.live.commit()
        self.dest = os.path.join(self.base, "backups")
        self.no_apps = mock.patch.object(archive, "running_apps", return_value=[])
        self.no_apps.start()

    def tearDown(self):
        self.no_apps.stop()
        self.live.close()
        self.temp.cleanup()

    def backup(self, **kwargs):
        plan = archive.plan_backup(self.env, **kwargs)
        path, manifest = archive.write_backup(plan, self.dest, readable=False)
        return path, manifest

    def names(self, path):
        with zipfile.ZipFile(path) as zf:
            return set(zf.namelist())

    def test_backup_excludes_logins_caches_and_browser_profile(self):
        path, manifest = self.backup()
        names = self.names(path)
        self.assertIn("files/antigravity-data/antigravity-ide/brain/c1/plan.md", names)
        self.assertIn("files/codex/sessions/2026/01/rollout-x.jsonl", names)
        self.assertIn("files/claude-config/.claude.json", names)
        for unwanted in ("files/codex/auth.json", "files/claude/.credentials.json",
                         "files/codex/cache/big.bin",
                         "files/antigravity-data/antigravity-browser-profile/Cookies"):
            self.assertNotIn(unwanted, names)
        self.assertFalse(any(n.endswith("-wal") or n.endswith("-shm") for n in names))
        self.assertTrue(archive.verify_backup(path)[0])

    def test_logins_only_when_requested_and_recordings_optional(self):
        plan = archive.plan_backup(self.env, include_logins=True)
        self.assertGreater(archive.optional_sizes(plan)["recordings"], 0)
        archive.drop_optional(plan, "recordings")
        path, _ = archive.write_backup(plan, self.dest, readable=False)
        names = self.names(path)
        self.assertIn("files/codex/auth.json", names)
        self.assertFalse(any("browser_recordings" in n for n in names))

    def test_sqlite_snapshot_contains_rows_still_in_the_wal_file(self):
        self.assertTrue(os.path.exists(self.state + "-wal"))
        path, manifest = self.backup()
        entry = next(e for e in manifest["files"] if e["rel"].endswith("state.vscdb"))
        self.assertEqual(entry["method"], "sqlite-snapshot")
        out = os.path.join(self.base, "check.db")
        with zipfile.ZipFile(path) as zf, open(out, "wb") as f:
            f.write(zf.read("files/antigravity-ide-app/globalStorage/state.vscdb"))
        conn = sqlite3.connect(out)
        self.assertEqual(conn.execute("SELECT value FROM ItemTable").fetchone()[0],
                         "committed-in-wal")
        conn.close()

    def test_verify_detects_a_damaged_file(self):
        path, _ = self.backup()
        damaged = os.path.join(self.base, "damaged.zip")
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(damaged, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename.endswith("plan.md"):
                    data = b"# tampered\n"
                dst.writestr(item, data)
        ok, problems, _ = archive.verify_backup(damaged)
        self.assertFalse(ok)
        self.assertTrue(any("plan.md" in p for p in problems))

    def test_restore_to_new_machine_round_trip(self):
        path, manifest = self.backup()
        new_env = make_env(os.path.join(self.base, "new"))
        # The fresh install already has its own state database and stale WAL.
        fresh = os.path.join(new_env["APPCFG"], "Antigravity IDE", "User", "globalStorage",
                             "state.vscdb")
        write(fresh, "fresh-install")
        write(fresh + "-wal", "stale wal")
        write(os.path.join(new_env["HOME"], ".claude", "projects", "new", "n.jsonl"), "new\n")

        summary = archive.restore_backup(path, env=new_env)

        plan_md = os.path.join(new_env["HOME"], ".gemini", "antigravity-ide", "brain", "c1",
                               "plan.md")
        with open(plan_md, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# Plan\n")
        self.assertEqual(int(os.path.getmtime(plan_md)), 1_700_000_000)
        conn = sqlite3.connect(fresh)
        self.assertEqual(conn.execute("SELECT value FROM ItemTable").fetchone()[0],
                         "committed-in-wal")
        conn.close()
        self.assertFalse(os.path.exists(fresh + "-wal"))
        # Files created after reinstalling are kept.
        self.assertTrue(os.path.exists(
            os.path.join(new_env["HOME"], ".claude", "projects", "new", "n.jsonl")))
        self.assertEqual(summary["replaced"], 1)
        self.assertTrue(summary["warnings"])  # different home folder

        # The replaced file and stale WAL went into a restorable safety backup.
        safety = summary["safety_backup"]
        self.assertTrue(archive.verify_backup(safety)[0])
        names = self.names(safety)
        self.assertIn("files/antigravity-ide-app/globalStorage/state.vscdb", names)
        self.assertIn("files/antigravity-ide-app/globalStorage/state.vscdb-wal", names)

        again = archive.restore_backup(path, env=new_env)
        self.assertEqual(again["new"] + again["replaced"], 0)
        self.assertIsNone(again["safety_backup"])

    def test_restore_only_selected_tool_and_dry_run(self):
        path, _ = self.backup()
        new_env = make_env(os.path.join(self.base, "new"))
        preview = archive.restore_backup(path, env=new_env, tools=["claude"], dry_run=True)
        self.assertEqual(preview["new"], 2)
        self.assertFalse(os.path.exists(os.path.join(new_env["HOME"], ".claude.json")))
        archive.restore_backup(path, env=new_env, tools=["claude"])
        self.assertTrue(os.path.exists(os.path.join(new_env["HOME"], ".claude.json")))
        self.assertFalse(os.path.exists(os.path.join(new_env["HOME"], ".codex")))

    def test_restore_refuses_while_apps_run_and_on_damaged_zip(self):
        path, _ = self.backup()
        new_env = make_env(os.path.join(self.base, "new"))
        with mock.patch.object(archive, "running_apps", return_value=["Antigravity"]):
            with self.assertRaises(BackupError):
                archive.restore_backup(path, env=new_env)
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        manifest["files"][0]["sha256"] = "0" * 64
        bad = os.path.join(self.base, "bad.zip")
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(bad, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "manifest.json":
                    data = json.dumps(manifest).encode()
                dst.writestr(item, data)
        with self.assertRaises(BackupError):
            archive.restore_backup(bad, env=new_env)
        self.assertEqual(os.listdir(new_env["HOME"]), [])

    def test_backup_refuses_when_destination_is_too_small(self):
        plan = archive.plan_backup(self.env)
        with mock.patch.object(archive.shutil, "disk_usage",
                               return_value=mock.Mock(free=10)):
            with self.assertRaises(BackupError):
                archive.write_backup(plan, self.dest, readable=False)


if __name__ == "__main__":
    unittest.main()
