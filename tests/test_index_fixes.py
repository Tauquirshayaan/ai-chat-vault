"""Regression tests for the defects found in Antigravity Conversation Fix v1.07."""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from chatvault import antigravity_index as fix
from tests.helpers import (decode_index, encode_index, inner_blob, make_state_db,
                           read_state_value, workspace_field)


class WorkspacePreservationTests(unittest.TestCase):
    def setUp(self):
        self.ws = workspace_field("file:///g:/My%20Work/Demo%20App", "file:///g:/My%20Work")
        self.inner = inner_blob("Old title", self.ws)

    def test_hint_is_the_uri_not_the_raw_submessage(self):
        self.assertEqual(fix.extract_workspace_hint(self.inner),
                         "file:///g:/My%20Work/Demo%20App")

    def test_rebuild_keeps_encoded_workspace_byte_for_byte(self):
        entry = fix.build_trajectory_entry("cid", "New title", self.inner)
        info = next(fix.iter_length_delimited_fields(entry, 2))
        out = fix.base64.b64decode(next(fix.iter_length_delimited_fields(info, 1)))
        self.assertEqual(list(fix.iter_length_delimited_fields(out, 9)),
                         list(fix.iter_length_delimited_fields(self.ws, 9)))
        self.assertEqual(fix.extract_workspace_hint(out), "file:///g:/My%20Work/Demo%20App")
        self.assertEqual(next(fix.iter_length_delimited_fields(out, 1)), b"New title")

    def test_new_workspace_uri_matches_antigravity_encoding(self):
        self.assertEqual(fix.path_to_workspace_uri("D:\\Repos\\My Project"),
                         "file:///d:/Repos/My%20Project")
        self.assertEqual(fix.path_to_workspace_uri("vscode-remote://wsl+Ubuntu/home/a"),
                         "vscode-remote://wsl+Ubuntu/home/a")


class DatabaseWriteTests(unittest.TestCase):
    def test_same_file_listed_twice_is_written_once(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, "state.vscdb")
            make_state_db(db, None)
            alias = os.path.join(temp, ".", "state.vscdb")
            self.assertEqual(fix.unique_existing_files([db, alias, os.path.join(temp, "x")]), [db])

    def test_backups_never_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, "state.vscdb")
            make_state_db(db, "ORIGINAL")
            with mock.patch.object(fix.time, "strftime", return_value="20260101_000000"):
                first, _ = fix.backup_index(db, "antigravity", temp)
                fix.write_index_value(db, "REBUILT")
                second, _ = fix.backup_index(db, "antigravity", temp)
            self.assertNotEqual(first, second)
            with open(first, encoding="utf-8") as f:
                self.assertEqual(f.read(), "ORIGINAL")

    def test_failed_write_rolls_back_databases_already_written(self):
        with tempfile.TemporaryDirectory() as temp:
            dbs = []
            for name in ("a", "b"):
                os.makedirs(os.path.join(temp, name, "User", "globalStorage"))
                db = os.path.join(temp, name, "User", "globalStorage", "state.vscdb")
                make_state_db(db, "ORIGINAL-" + name)
                dbs.append(db)
            real_write = fix.write_index_value
            calls = []

            def flaky(db_path, value):
                calls.append(db_path)
                if db_path == dbs[1] and value == "NEW":
                    raise sqlite3.OperationalError("disk I/O error")
                real_write(db_path, value)

            with mock.patch.object(fix, "write_index_value", side_effect=flaky):
                with self.assertRaises(sqlite3.OperationalError):
                    fix.write_index_to_all(dbs, "NEW", temp)
            self.assertEqual(read_state_value(dbs[0]), "ORIGINAL-a")
            self.assertEqual(read_state_value(dbs[1]), "ORIGINAL-b")


class RepairRunTests(unittest.TestCase):
    def _env(self, temp, index_value):
        conv = os.path.join(temp, "conversations")
        os.makedirs(conv)
        for cid in ("on-disk",):
            with open(os.path.join(conv, cid + ".pb"), "wb") as f:
                f.write(b"opaque")
        db = os.path.join(temp, "Antigravity IDE", "User", "globalStorage", "state.vscdb")
        os.makedirs(os.path.dirname(db))
        make_state_db(db, index_value)
        patches = [
            mock.patch.object(fix, "is_antigravity_running", return_value=False),
            mock.patch.object(fix, "DB_PATHS", [db]),
            mock.patch.object(fix, "_ALL_CONV_DIRS", [conv]),
            mock.patch.object(fix, "_ALL_BRAIN_DIRS", [os.path.join(temp, "brain")]),
            mock.patch.object(fix, "WORKSPACE_STORAGE_DIR", os.path.join(temp, "none")),
            mock.patch.object(fix, "_backup_dir", return_value=temp),
        ]
        return db, patches

    def _run(self, patches, **kwargs):
        for p in patches:
            p.start()
        try:
            return fix.run_repair(interactive=False, workspace_mode="none", **kwargs)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_unreadable_index_aborts_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            db, patches = self._env(temp, "%%% not base64 %%%")
            self.assertEqual(self._run(patches), 1)
            self.assertEqual(read_state_value(db), "%%% not base64 %%%")

    def test_entries_whose_file_is_missing_are_kept_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = workspace_field("file:///g:/My%20Proj", "file:///g:/My%20Proj")
            gone = inner_blob("Deleted locally", ws, updated=50)
            here = inner_blob("Still here", ws, updated=60)
            db, patches = self._env(temp, encode_index([("on-disk", here), ("gone", gone)]))
            self.assertEqual(self._run(patches), 0)
            result = decode_index(read_state_value(db))
            self.assertEqual(set(result), {"on-disk", "gone"})
            self.assertEqual(result["gone"], gone)
            self.assertEqual(fix.extract_workspace_hint(result["on-disk"]), "file:///g:/My%20Proj")

    def test_drop_missing_removes_them(self):
        with tempfile.TemporaryDirectory() as temp:
            db, patches = self._env(temp, encode_index([
                ("on-disk", inner_blob("Here")), ("gone", inner_blob("Gone"))]))
            self.assertEqual(self._run(patches, keep_missing=False), 0)
            self.assertEqual(set(decode_index(read_state_value(db))), {"on-disk"})


class TitleRobustnessTests(unittest.TestCase):
    def test_malformed_ipv6_link_does_not_crash_title_formatting(self):
        title = fix.format_prompt_title("open http://[::1 and check it")
        self.assertTrue(title.startswith("open"))


if __name__ == "__main__":
    unittest.main()
