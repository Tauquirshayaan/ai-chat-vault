"""Export every chat as readable Markdown, grouped by project."""

import glob
import json
import os
import re
import shutil
import sqlite3
import time
from urllib.parse import unquote

from . import antigravity_index as ag
from .common import IS_WINDOWS, default_env

AG_FOLDERS = ("antigravity-ide", "antigravity", "antigravity-backup")

_CONTEXT_TAGS = re.compile(
    r"<(system-reminder|ide_opened_file|ide_selection|ide_diagnostics|"
    r"local-command-caveat|environment_context|user_instructions)>.*?</\1>",
    re.DOTALL,
)
_CODEX_SKIP_PREFIXES = (
    "<environment_context>", "<user_instructions>", "# AGENTS.md instructions",
    "<permissions instructions>", "<turn_aborted>", "<user_shell_command>",
    "<collaboration_mode>", "<skills_instructions>",
)


# ─── Small helpers ───────────────────────────────────────────────────────────

def _slug(text, limit=50):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return (slug[:limit].rstrip("-") or "chat")


def _fmt_time(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    text = str(value).replace("T", " ")
    return text[:16]


def _sort_key(value):
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(value))
    return str(value or "")


def _clean_text(text):
    text = _CONTEXT_TAGS.sub("", text or "")
    return text.strip()


def _uri_to_display(uri):
    if not uri:
        return None
    if uri.startswith("file:///"):
        path = unquote(uri[len("file:///"):])
        if IS_WINDOWS or re.match(r"^[A-Za-z]:", path):
            return path.replace("/", "\\")
        return "/" + path
    return unquote(uri)


def _read_jsonl(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


FAILED = []


def _safe(func, *args):
    """Run one per-chat loader; a damaged file is reported, never fatal."""
    try:
        return func(*args)
    except Exception as error:
        FAILED.append(f"{args[0] if args else ''}: {error}")
        return None


class Chat:
    def __init__(self, tool, chat_id, title, project, started, updated, resume=None):
        self.tool = tool
        self.id = chat_id
        self.title = title
        self.project = project
        self.started = started
        self.updated = updated
        self.resume = resume
        self.turns = []        # (speaker, text, tools)
        self.attachments = []  # (label, relative link)
        self.note = None
        self.file = None


def _add_turn(chat, speaker, text, tool=None):
    if chat.turns and chat.turns[-1][0] == speaker and speaker != "You":
        last = chat.turns[-1]
        merged = (last[1] + "\n\n" + text).strip() if text else last[1]
        tools = last[2]
        if tool:
            tools[tool] = tools.get(tool, 0) + 1
        chat.turns[-1] = (speaker, merged, tools)
        return
    tools = {tool: 1} if tool else {}
    chat.turns.append((speaker, text or "", tools))


# ─── Claude Code ─────────────────────────────────────────────────────────────

def _claude_user_text(content):
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image":
                parts.append("[image]")
    else:
        return ""
    text = _clean_text("\n".join(parts))
    if "<local-command-stdout>" in text or text.startswith("<local-command"):
        return ""
    if "<command-name>" in text:
        name = re.search(r"<command-name>(.*?)</command-name>", text, re.DOTALL)
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.DOTALL)
        text = ((name.group(1) if name else "") + " " + (args.group(1) if args else "")).strip()
    return text


def load_claude_chats(env):
    chats = []
    pattern = os.path.join(env["HOME"], ".claude", "projects", "*", "*.jsonl")
    for path in sorted(glob.glob(pattern)):
        chat = _safe(_load_claude_file, path)
        if chat:
            chats.append(chat)
    return chats


def _load_claude_file(path):
    session_id = os.path.splitext(os.path.basename(path))[0]
    chat = Chat("Claude Code", session_id, None, None, None, None,
                resume=f"claude --resume {session_id}   (run inside the project folder)")
    ai_title = summary = None
    for record in _read_jsonl(path):
        kind = record.get("type")
        if kind == "ai-title" and record.get("aiTitle"):
            ai_title = record["aiTitle"]
        elif kind == "summary" and record.get("summary"):
            summary = record["summary"]
        if kind not in ("user", "assistant") or record.get("isSidechain"):
            continue
        chat.project = chat.project or record.get("cwd")
        stamp = record.get("timestamp")
        if stamp:
            chat.started = chat.started or stamp
            chat.updated = stamp
        message = record.get("message") or {}
        if kind == "user":
            if record.get("isMeta"):
                continue
            text = _claude_user_text(message.get("content"))
            if text and record.get("isCompactSummary"):
                _add_turn(chat, "Summary of the earlier part of this chat", text)
            elif text:
                _add_turn(chat, "You", text)
        else:
            content = message.get("content")
            if isinstance(content, str):
                _add_turn(chat, "Claude", content.strip())
                continue
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    _add_turn(chat, "Claude", block["text"].strip())
                elif block.get("type") == "tool_use":
                    _add_turn(chat, "Claude", "", block.get("name") or "tool")
    if not any(t[0] == "You" for t in chat.turns):
        return None
    first = next(t[1] for t in chat.turns if t[0] == "You")
    chat.title = ai_title or summary or ag.format_prompt_title(first) or session_id[:8]
    return chat


# ─── Codex ───────────────────────────────────────────────────────────────────

def _codex_text(content):
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text", "text"):
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def load_codex_chats(env):
    codex = os.path.join(env["HOME"], ".codex")
    names = {}
    for record in _read_jsonl(os.path.join(codex, "session_index.jsonl")):
        if record.get("id") and record.get("thread_name"):
            names[record["id"]] = record["thread_name"]
    files = glob.glob(os.path.join(codex, "sessions", "**", "*.jsonl"), recursive=True)
    files += glob.glob(os.path.join(codex, "archived_sessions", "**", "*.jsonl"), recursive=True)
    chats = []
    for path in sorted(set(files)):
        chat = _safe(_load_codex_file, path, names)
        if chat:
            chats.append(chat)
    return chats


def _load_codex_file(path, names):
    chat = Chat("Codex", None, None, None, None, None)
    for record in _read_jsonl(path):
        kind = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else None
        stamp = record.get("timestamp")
        if kind == "session_meta" and payload:
            chat.id = chat.id or payload.get("id") or payload.get("session_id")
            chat.project = chat.project or payload.get("cwd")
            chat.started = chat.started or payload.get("timestamp") or stamp
            continue
        item = payload if kind == "response_item" else (record if kind == "message" else None)
        if not item:
            continue
        if stamp:
            chat.updated = stamp
            chat.started = chat.started or stamp
        item_type = item.get("type")
        if item_type == "message":
            role = item.get("role")
            text = _codex_text(item.get("content"))
            if role == "user":
                if not text or text.lstrip().startswith(_CODEX_SKIP_PREFIXES):
                    continue
                text = _clean_text(text)
                if text:
                    _add_turn(chat, "You", text)
            elif role == "assistant" and text:
                _add_turn(chat, "Codex", text)
        elif item_type in ("function_call", "custom_tool_call", "local_shell_call"):
            _add_turn(chat, "Codex", "", item.get("name") or item_type)
    if not any(t[0] == "You" for t in chat.turns):
        return None
    if not chat.id:
        match = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27,})\.jsonl$", path)
        chat.id = match.group(1) if match else os.path.basename(path)
    first = next(t[1] for t in chat.turns if t[0] == "You")
    chat.title = names.get(chat.id) or ag.format_prompt_title(first) or chat.id[:8]
    chat.resume = f"codex resume {chat.id}"
    return chat


# ─── Antigravity ─────────────────────────────────────────────────────────────

def _ag_catalog(gemini):
    catalog = {}
    for folder in AG_FOLDERS:
        conv_dir = os.path.join(gemini, folder, "conversations")
        if not os.path.isdir(conv_dir):
            continue
        for name in sorted(os.listdir(conv_dir)):
            lower = name.lower()
            if not (lower.endswith(".db") or lower.endswith(".pb")):
                continue
            path = os.path.join(conv_dir, name)
            cid = name[:-3]
            if cid not in catalog or ag._conversation_rank(path) > ag._conversation_rank(catalog[cid]):
                catalog[cid] = path
    return catalog


def _ag_brain_paths(gemini, cid):
    paths = []
    for folder in AG_FOLDERS:
        path = os.path.join(gemini, folder, "brain", cid)
        if os.path.isdir(path):
            paths.append(path)
    return paths


def _ag_brain_heading(brain_paths):
    for brain in brain_paths:
        for name in sorted(os.listdir(brain)):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            try:
                with open(os.path.join(brain, name), "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if i >= 100:
                            break
                        if line.strip().startswith("#"):
                            return line.strip().lstrip("# ").strip()[:80]
            except OSError:
                continue
    return None


def _ag_index(env):
    candidates = [
        os.path.join(env["APPCFG"], name, "User", "globalStorage", "state.vscdb")
        for name in ("Antigravity IDE", "Antigravity", "antigravity")
    ]
    try:
        return ag.extract_existing_metadata_from_paths(ag.unique_existing_files(candidates))
    except Exception:
        return {}, {}


def _ag_read_steps(db_path, chat):
    """Fill chat turns from a v2 Antigravity conversation database."""
    conn = None
    try:
        conn = ag._open_sqlite_readonly(db_path)
        table, has_idx = ag._find_step_table(conn)
        if not table:
            return False
        order = '"idx"' if has_idx else "rowid"
        rows = conn.execute(
            "SELECT step_type, step_payload FROM " + table
            + " WHERE step_type IN (14, 15) ORDER BY " + order)
        for step_type, payload in rows:
            if isinstance(payload, memoryview):
                payload = payload.tobytes()
            if not isinstance(payload, bytes):
                continue
            if step_type == 14:
                for container in ag.iter_length_delimited_fields(payload, 19):
                    for raw in ag.iter_length_delimited_fields(container, 2):
                        text = raw.decode("utf-8", errors="replace").strip()
                        if text:
                            _add_turn(chat, "You", text)
            else:
                for container in ag.iter_length_delimited_fields(payload, 20):
                    for raw in ag.iter_length_delimited_fields(container, 1):
                        text = raw.decode("utf-8", errors="replace").strip()
                        if text:
                            _add_turn(chat, "Antigravity", text)
                    for call in ag.iter_length_delimited_fields(container, 7):
                        name = next(ag.iter_length_delimited_fields(call, 2), None)
                        if name:
                            _add_turn(chat, "Antigravity", "",
                                      name.decode("utf-8", errors="replace"))
        return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        if conn is not None:
            conn.close()


def _ag_read_transcript(brain_paths, chat):
    """
    Fill chat turns from a brain transcript (.system_generated/logs), which
    Antigravity keeps in readable JSONL: USER_INPUT steps are your requests,
    PLANNER_RESPONSE steps the agent's replies and tool calls.
    """
    for brain in brain_paths:
        logs = os.path.join(brain, ".system_generated", "logs")
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            path = os.path.join(logs, name)
            if not os.path.isfile(path):
                continue
            for record in _read_jsonl(path):
                kind = record.get("type")
                stamp = record.get("created_at")
                if kind == "USER_INPUT":
                    text = ag._unwrap_user_request_text(record.get("content"))
                    if text:
                        _add_turn(chat, "You", text)
                elif kind == "PLANNER_RESPONSE":
                    text = record.get("content")
                    if isinstance(text, str) and text.strip():
                        _add_turn(chat, "Antigravity", text.strip())
                    calls = record.get("tool_calls")
                    if isinstance(calls, list):
                        for call in calls:
                            if isinstance(call, dict) and call.get("name"):
                                _add_turn(chat, "Antigravity", "", call["name"])
                else:
                    continue
                if stamp:
                    chat.started = chat.started or stamp
                    chat.updated = stamp
            if chat.turns:
                return True
    return False


def load_antigravity_chats(env):
    gemini = os.path.join(env["HOME"], ".gemini")
    titles, blobs = _ag_index(env)
    chats = []
    for cid, path in _ag_catalog(gemini).items():
        chat = _safe(_load_ag_chat, gemini, cid, path, titles, blobs)
        if chat:
            chats.append(chat)
    return chats


def _load_ag_chat(gemini, cid, path, titles, blobs):
    brain_paths = _ag_brain_paths(gemini, cid)
    mtime = os.path.getmtime(path)
    workspace = _uri_to_display(ag.extract_workspace_hint(blobs.get(cid)))
    chat = Chat("Antigravity", cid, None, workspace, None, mtime)
    from_transcript = Chat("Antigravity", cid, None, workspace, None, mtime)
    _ag_read_transcript(brain_paths, from_transcript)
    db_ok = path.lower().endswith(".db") and _ag_read_steps(path, chat)

    def user_turns(c):
        return sum(1 for t in c.turns if t[0] == "You")

    # The transcript usually has every request; some .db files store requests
    # elsewhere. Use whichever source recovered more of your messages.
    if from_transcript.turns and user_turns(from_transcript) >= user_turns(chat):
        chat = from_transcript
    elif not chat.turns:
        if path.lower().endswith(".pb"):
            chat.note = ("Stored in Antigravity's older encrypted .pb format with no readable "
                         "transcript, so only its brain notes (linked below) are available.")
        elif not db_ok:
            chat.note = "This conversation database could not be read."
    first = next((t[1] for t in chat.turns if t[0] == "You"), None)
    chat.title = (titles.get(cid) or _ag_brain_heading(brain_paths)
                  or ag.format_prompt_title(first)
                  or f"Conversation ({time.strftime('%b %d', time.localtime(mtime))}) {cid[:8]}")
    chat.brain_paths = brain_paths
    return chat


# ─── Writing ─────────────────────────────────────────────────────────────────

def _write_chat(chat, path, rel_root):
    lines = [f"# {chat.title}", ""]
    lines.append(f"- **Tool:** {chat.tool}")
    lines.append(f"- **Project folder:** {chat.project or 'unknown'}")
    if chat.started:
        lines.append(f"- **Started:** {_fmt_time(chat.started)}")
    if chat.updated:
        lines.append(f"- **Last activity:** {_fmt_time(chat.updated)}")
    lines.append(f"- **Chat ID:** `{chat.id}`")
    if chat.resume:
        lines.append(f"- **Continue it:** `{chat.resume}`")
    lines.append(f"- [Back to index]({rel_root}INDEX.md)")
    lines.append("")
    if chat.note:
        lines += [f"> {chat.note}", ""]
    if chat.attachments:
        lines.append("**Agent notes and artifacts:**")
        for label, link in chat.attachments:
            lines.append(f"- [{label}]({link})")
        lines.append("")
    lines += ["---", ""]
    for speaker, text, tools in chat.turns:
        lines.append(f"### {speaker}")
        lines.append("")
        if text:
            lines.append(text)
            lines.append("")
        if tools:
            used = ", ".join(f"{name} ×{count}" if count > 1 else name
                             for name, count in tools.items())
            lines.append(f"*Tools used: {used}*")
            lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


def _project_key(project):
    if not project:
        return "~"
    key = project.replace("/", "\\") if IS_WINDOWS else project
    return key.rstrip("\\/").lower() if IS_WINDOWS else key.rstrip("/")


HOW_TO_CONTINUE = """# How to continue your projects after reinstalling

Every chat from Antigravity, Codex and Claude Code is in this folder as plain
Markdown, grouped by project in [INDEX.md](INDEX.md).

## Fastest way to brief a new agent

Open the project folder in your agent and paste:

> I reinstalled my PC. My earlier conversations about this project are saved as
> Markdown in `<path to this folder>`. Read INDEX.md, then every chat listed under
> this project folder. Write `PROJECT_CONTEXT.md` in the project root with: the
> goal, architecture, key decisions and why they were made, what is finished,
> open bugs, and the next steps. Then continue from where we stopped.

Keep `PROJECT_CONTEXT.md` in the repository. Point each tool at it so a new
session starts with the context:

- Antigravity reads `GEMINI.md`
- Codex reads `AGENTS.md`
- Claude Code reads `CLAUDE.md` (it can contain just `@AGENTS.md`)

## Resuming the original sessions

If you restored the backup with AI Chat Vault, the original sessions are back:

- Antigravity: they appear in the sidebar again
- Codex: `codex resume` (or the ID shown in each chat file)
- Claude Code: `claude --resume` inside the project folder
"""


def export_all(out_dir, env=None, tools=None, quiet=False):
    """Write INDEX.md and one Markdown file per chat into out_dir."""
    env = env or default_env()
    log = (lambda *_: None) if quiet else (lambda m: print(m, flush=True))
    loaders = {"antigravity": load_antigravity_chats, "codex": load_codex_chats,
               "claude": load_claude_chats}
    os.makedirs(out_dir, exist_ok=True)
    del FAILED[:]
    all_chats = []
    counts = {}
    for key, loader in loaders.items():
        if tools and key not in tools:
            continue
        try:
            chats = loader(env)
        except Exception as error:
            log(f"  {key}: could not export ({error})")
            chats = []
        counts[key] = len(chats)
        log(f"  {key}: {len(chats)} chats")
        folder = os.path.join(out_dir, key)
        for chat in chats:
            os.makedirs(folder, exist_ok=True)
            stamp = _sort_key(chat.started or chat.updated)[:10] or "undated"
            name = f"{stamp}_{_slug(chat.title)}_{str(chat.id)[:8]}.md"
            for brain in getattr(chat, "brain_paths", []):
                for fname in sorted(os.listdir(brain)):
                    if fname.startswith(".") or not fname.endswith(".md"):
                        continue
                    target_dir = os.path.join(folder, "brain", chat.id)
                    os.makedirs(target_dir, exist_ok=True)
                    target = os.path.join(target_dir, fname)
                    link = (fname, f"brain/{chat.id}/{fname}")
                    if link in chat.attachments:
                        continue  # same note in an older folder; newest copied first
                    shutil.copy2(os.path.join(brain, fname), target)
                    chat.attachments.append(link)
            _write_chat(chat, os.path.join(folder, name), "../")
            chat.file = f"{key}/{name}"
            all_chats.append(chat)

    groups = {}
    for chat in all_chats:
        groups.setdefault(_project_key(chat.project), []).append(chat)

    def newest(chats):
        return max(_sort_key(c.updated or c.started) for c in chats)

    lines = ["# All AI chats", "",
             f"Exported {time.strftime('%Y-%m-%d %H:%M')}: "
             + ", ".join(f"{v} {k}" for k, v in counts.items()) + " chats.", "",
             "See [HOW_TO_CONTINUE.md](HOW_TO_CONTINUE.md) to brief a new agent.", ""]
    ordered = sorted(groups.items(), key=lambda kv: newest(kv[1]), reverse=True)
    ordered.sort(key=lambda kv: kv[0] == "~")
    for key, chats in ordered:
        project = next((c.project for c in chats if c.project), None)
        lines.append(f"## {project or 'No project folder recorded'}")
        lines.append("")
        lines.append("| Last activity | Tool | Chat |")
        lines.append("|---|---|---|")
        for chat in sorted(chats, key=lambda c: _sort_key(c.updated or c.started), reverse=True):
            title = chat.title.replace("|", "\\|")
            lines.append(f"| {_fmt_time(chat.updated or chat.started)} | {chat.tool} | "
                         f"[{title}]({chat.file}) |")
        lines.append("")
    with open(os.path.join(out_dir, "INDEX.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    with open(os.path.join(out_dir, "HOW_TO_CONTINUE.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(HOW_TO_CONTINUE)
    for failure in FAILED:
        log(f"  could not read: {failure}")
    return {"chats": len(all_chats), "by_tool": counts, "failed": list(FAILED)}
