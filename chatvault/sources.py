"""What gets backed up, and which parts are skipped."""

import fnmatch
import os

from .common import IS_WINDOWS, SYSTEM, expand

# Login tokens: excluded unless the user explicitly asks for them, so a
# backup copied to cloud storage or a USB stick cannot sign anyone in.
LOGIN_FILES = {
    "codex": ("auth.json", ".sandbox-secrets/**"),
    "claude": (".credentials.json", ".credentials.json.*"),
}


class Root:
    """A folder or file to back up. ``template`` uses {HOME} / {APPCFG}."""

    def __init__(self, root_id, tool, label, templates, kind="dir",
                 excludes=(), category="chats"):
        self.id = root_id
        self.tool = tool
        self.label = label
        self.templates = tuple(templates)  # first existing one is used
        self.kind = kind
        self.excludes = tuple(excludes)
        self.category = category

    def resolve(self, env):
        """Return (template, native_path) of the first existing candidate."""
        for template in self.templates:
            path = expand(template, env)
            if (self.kind == "dir" and os.path.isdir(path)) or (
                    self.kind == "file" and os.path.isfile(path)):
                return template, path
        return None, None


def _appcfg_names(*names):
    return tuple("{APPCFG}/" + n + "/User" for n in names)


if SYSTEM == "Darwin":
    _AG_STANDALONE = _appcfg_names("antigravity", "Antigravity")
else:
    _AG_STANDALONE = _appcfg_names("Antigravity", "antigravity")

ROOTS = (
    Root("antigravity-data", "Antigravity", "Chats, brain notes, knowledge (~/.gemini)",
         ["{HOME}/.gemini"],
         excludes=(
             "antigravity-browser-profile/**", "bin/**", "logs/**", "tmp/**",
             "*/presence/**",
             "antigravity/bin/**", "antigravity-ide/bin/**",
             "antigravity/cache/**", "antigravity/log/**",
             "antigravity/crashes/**", "antigravity-ide/crashes/**",
             "antigravity-backup/crashes/**",
         )),
    Root("antigravity-ide-app", "Antigravity", "Antigravity IDE sidebar index and settings",
         _appcfg_names("Antigravity IDE")),
    Root("antigravity-app", "Antigravity", "Antigravity sidebar index and settings",
         _AG_STANDALONE),
    Root("antigravity-ext", "Antigravity", "Antigravity extensions (~/.antigravity)",
         ["{HOME}/.antigravity"], category="extensions",
         excludes=("extensions/.obsolete", "extensions/*/node_modules/.cache/**")),
    Root("antigravity-ide-ext", "Antigravity", "Antigravity IDE extensions (~/.antigravity-ide)",
         ["{HOME}/.antigravity-ide"], category="extensions",
         excludes=("extensions/.obsolete", "extensions/*/node_modules/.cache/**")),
    Root("codex", "Codex", "Codex sessions, history, memories, config (~/.codex)",
         ["{HOME}/.codex"],
         excludes=(
             ".tmp/**", "tmp/**", "cache/**", ".sandbox/**", ".sandbox-bin/**",
             "thread-writer-locks/**", "logs_*.sqlite", "logs_*.sqlite-*",
             "*.lock", "cap_sid", "models_cache.json", ".sandbox_migration",
         )),
    Root("claude", "Claude Code", "Claude Code transcripts, memory, settings, skills (~/.claude)",
         ["{HOME}/.claude"],
         excludes=(
             "cache/**", "shell-snapshots/**", "session-env/**", "ide/**",
             "statsig/**", "telemetry/**", "*.tmp.*",
         )),
    Root("claude-config", "Claude Code", "Claude Code MCP servers and project settings (~/.claude.json)",
         ["{HOME}/.claude.json"], kind="file"),
)

TOOL_KEYS = {"antigravity": "Antigravity", "codex": "Codex", "claude": "Claude Code"}

# Optional parts inside a root, offered to the user with their size.
OPTIONAL_GROUPS = {
    "recordings": {
        "label": "Antigravity browser-agent screenshots (browser_recordings)",
        "patterns": {"antigravity-data": ("*/browser_recordings/**",)},
    },
}


def optional_group_of(root_id, rel_path):
    for name, group in OPTIONAL_GROUPS.items():
        patterns = group["patterns"].get(root_id)
        if patterns and is_excluded(rel_path, patterns):
            return name
    return None


def _norm(value):
    return value.lower() if IS_WINDOWS or SYSTEM == "Darwin" else value


def is_excluded(rel_path, patterns):
    """
    Match a forward-slash relative path against anchored glob patterns.
    'dir/**' excludes a folder and everything under it; a pattern without a
    slash also matches a file name at any depth.
    """
    rel = _norm(rel_path)
    parts = rel.split("/")
    for raw in patterns:
        pattern = _norm(raw)
        if pattern.endswith("/**"):
            base = pattern[:-3]
            depth = base.count("/") + 1
            if len(parts) >= depth and fnmatch.fnmatchcase("/".join(parts[:depth]), base):
                return True
        elif "/" in pattern:
            if fnmatch.fnmatchcase(rel, pattern):
                return True
        elif fnmatch.fnmatchcase(parts[-1], pattern) or fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


def root_excludes(root, include_logins):
    patterns = list(root.excludes)
    if not include_logins:
        patterns.extend(LOGIN_FILES.get(root.id, ()))
    return patterns


def iter_root_files(root, path, include_logins=False, skipped=None):
    """
    Yield (relative_path, absolute_path) for every file to back up.
    Unreadable folders are reported through the ``skipped`` list.
    """
    if root.kind == "file":
        yield os.path.basename(path), path
        return
    patterns = root_excludes(root, include_logins)
    for current, dirs, files in os.walk(path, onerror=lambda e: skipped is not None and
                                        skipped.append({"path": getattr(e, "filename", ""),
                                                        "reason": str(e)})):
        rel_dir = os.path.relpath(current, path).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        kept = []
        for d in sorted(dirs):
            full = os.path.join(current, d)
            if os.path.islink(full):
                continue
            if is_excluded(rel_dir + d + "/__dir__", patterns) or is_excluded(rel_dir + d, patterns):
                continue
            kept.append(d)
        dirs[:] = kept
        for name in sorted(files):
            rel = rel_dir + name
            full = os.path.join(current, name)
            if os.path.islink(full) or is_excluded(rel, patterns):
                continue
            yield rel, full


def selected_roots(env, tools=None, include_extensions=True):
    """Return [(root, template, path)] for roots that exist on this machine."""
    wanted = None
    if tools:
        wanted = {TOOL_KEYS[t] for t in tools}
    result = []
    seen_paths = []
    for root in ROOTS:
        if wanted and root.tool not in wanted:
            continue
        if root.category == "extensions" and not include_extensions:
            continue
        template, path = root.resolve(env)
        if not path:
            continue
        # Antigravity / antigravity are one folder on case-insensitive disks.
        if any(_same(path, p) for p in seen_paths):
            continue
        seen_paths.append(path)
        result.append((root, template, path))
    return result


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
