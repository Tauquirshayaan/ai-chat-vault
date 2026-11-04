import json
import os
import sqlite3
import tempfile
import unittest

from chatvault import antigravity_index as fix
from chatvault import export


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
        f.write("{broken json\n")


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = self.temp.name
        self.env = {"HOME": os.path.join(base, "home"), "APPCFG": os.path.join(base, "app")}
        self.out = os.path.join(base, "out")
        h = self.env["HOME"]

        write_jsonl(os.path.join(h, ".claude", "projects", "g--proj", "sess-1.jsonl"), [
            {"type": "user", "cwd": "G:\\proj", "timestamp": "2026-01-02T10:00:00Z",
             "message": {"role": "user", "content":
                         "<system-reminder>hidden</system-reminder>Build the login page"}},
            {"type": "assistant", "timestamp": "2026-01-02T10:01:00Z",
             "message": {"content": [{"type": "text", "text": "Done, see auth.ts"},
                                     {"type": "tool_use", "name": "Edit"}]}},
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            {"type": "user", "isCompactSummary": True,
             "message": {"content": "This session is being continued..."}},
            {"type": "ai-title", "aiTitle": "Login page work"},
        ])
        write_jsonl(os.path.join(h, ".codex", "sessions", "2026", "01", "rollout-a.jsonl"), [
            {"type": "session_meta", "payload": {"id": "cx-1", "cwd": "G:\\proj",
                                                 "timestamp": "2026-01-03T09:00:00Z"}},
            {"type": "response_item", "timestamp": "2026-01-03T09:00:01Z",
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Fix the failing test"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text",
                                                               "text": "Fixed it."}]}},
        ])
        write_jsonl(os.path.join(h, ".codex", "session_index.jsonl"),
                    [{"id": "cx-1", "thread_name": "Test fix"}])

        gem = os.path.join(h, ".gemini", "antigravity-ide")
        conv = os.path.join(gem, "conversations")
        os.makedirs(conv)
        db = os.path.join(conv, "ag-1.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, step_payload BLOB)")
        user = fix.encode_length_delimited(19, fix.encode_string_field(2, "Design the API"))
        call = fix.encode_string_field(1, "call_1") + fix.encode_string_field(2, "view_file")
        reply = fix.encode_length_delimited(
            20, fix.encode_string_field(1, "Here is the design.") + fix.encode_length_delimited(7, call))
        conn.executemany("INSERT INTO steps VALUES (?, ?, ?)",
                         [(0, 14, user), (1, 15, reply), (2, 9, b"")])
        conn.commit()
        conn.close()
        with open(os.path.join(conv, "ag-2.pb"), "wb") as f:
            f.write(b"encrypted")
        brain = os.path.join(gem, "brain", "ag-2")
        os.makedirs(brain)
        with open(os.path.join(brain, "walkthrough.md"), "w", encoding="utf-8") as f:
            f.write("# Legacy walkthrough\n")
        write_jsonl(os.path.join(gem, "brain", "ag-3", ".system_generated", "logs",
                                 "transcript.jsonl"), [
            {"type": "USER_INPUT", "created_at": "2026-01-05T08:00:00Z",
             "content": "<USER_REQUEST>\nAudit the repo\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x"},
            {"type": "PLANNER_RESPONSE", "content": "Audit complete.",
             "tool_calls": [{"name": "grep_search"}]},
            {"type": "EPHEMERAL_MESSAGE", "content": "ignore"},
        ])
        with open(os.path.join(conv, "ag-3.db"), "wb") as f:
            f.write(b"not sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def read(self, rel):
        with open(os.path.join(self.out, rel), encoding="utf-8") as f:
            return f.read()

    def test_exports_all_three_tools_grouped_by_project(self):
        stats = export.export_all(self.out, self.env, quiet=True)
        self.assertEqual(stats["by_tool"], {"antigravity": 3, "codex": 1, "claude": 1})
        index = self.read("INDEX.md")
        self.assertIn("## G:\\proj", index)
        self.assertIn("Login page work", index)
        self.assertIn("Test fix", index)

        claude = self.read(next(os.path.join("claude", n)
                                for n in os.listdir(os.path.join(self.out, "claude"))))
        self.assertIn("Build the login page", claude)
        self.assertNotIn("hidden", claude)
        self.assertNotIn("meta", claude)
        self.assertIn("Tools used: Edit", claude)
        self.assertIn("Summary of the earlier part", claude)
        self.assertIn("claude --resume sess-1", claude)

        codex = self.read(next(os.path.join("codex", n)
                               for n in os.listdir(os.path.join(self.out, "codex"))))
        self.assertIn("Fix the failing test", codex)
        self.assertNotIn("environment_context", codex)
        self.assertIn("Fixed it.", codex)
        self.assertIn("codex resume cx-1", codex)

    def test_antigravity_db_transcript_and_legacy_pb(self):
        export.export_all(self.out, self.env, tools=["antigravity"], quiet=True)
        files = {n.split("_")[-1]: n for n in os.listdir(os.path.join(self.out, "antigravity"))
                 if n.endswith(".md")}
        db_chat = self.read(os.path.join("antigravity", files["ag-1.md"]))
        self.assertIn("Design the API", db_chat)
        self.assertIn("Here is the design.", db_chat)
        self.assertIn("view_file", db_chat)

        legacy = self.read(os.path.join("antigravity", files["ag-2.md"]))
        self.assertIn("# Legacy walkthrough", legacy)
        self.assertIn("encrypted .pb", legacy)
        self.assertIn("brain/ag-2/walkthrough.md", legacy)
        self.assertTrue(os.path.isfile(
            os.path.join(self.out, "antigravity", "brain", "ag-2", "walkthrough.md")))

        transcript = self.read(os.path.join("antigravity", files["ag-3.md"]))
        self.assertIn("Audit the repo", transcript)
        self.assertNotIn("ADDITIONAL_METADATA", transcript)
        self.assertIn("Audit complete.", transcript)
        self.assertNotIn("ignore", transcript)


if __name__ == "__main__":
    unittest.main()
