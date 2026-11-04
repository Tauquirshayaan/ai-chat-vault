"""
Antigravity sidebar index repair.

Derived from Antigravity Conversation Fix v1.07 (MIT,
https://github.com/FutureisinPast/antigravity-conversation-fix), with these
corrections:

  - Existing workspace metadata (protobuf field 9) is preserved byte-for-byte.
    v1.07 re-encoded it from a mis-parsed value, corrupting every workspace
    URI that contained %20/%3A and dropping its parent-repo and git sub-fields.
  - Database paths that resolve to the same file (``Antigravity`` and
    ``antigravity`` on case-insensitive file systems) are written once, and
    backup file names can never collide, so the first rollback copy survives.
  - An index that exists but cannot be decoded aborts the repair instead of
    being silently replaced with an index that has no titles or workspaces.
  - Index entries whose conversation file is not on disk are kept by default.
  - All databases are backed up before any is written; a failed write rolls
    the already-written databases back.
  - Duplicate conversation IDs across folders resolve to ``.db`` first, then
    the most recently modified file.
"""

import sys
import os
import sqlite3
import base64
import json
import re
import time
import subprocess
import platform
from urllib.parse import quote, unquote, urlparse

from . import __version__ as _CURRENT_VERSION


class IndexReadError(RuntimeError):
    """The sidebar index exists but could not be decoded safely."""

# ─── Path Detection ──────────────────────────────────────────────────────────
# Antigravity was renamed to "Antigravity IDE" in a recent update.
# We check the new name first, then fall back to the old name so the tool
# works on both old and new installations.

_SYSTEM = platform.system()
_ANTIGRAVITY_NAMES = ("Antigravity IDE", "antigravity", "Antigravity")


def _is_wsl():
    """Detect if running inside Windows Subsystem for Linux."""
    if _SYSTEM != "Linux":
        return False
    if "microsoft" in platform.release().lower():
        return True
    try:
        with open("/proc/version", "r") as f:
            if "microsoft" in f.read().lower():
                return True
    except Exception:
        pass
    return False


_IS_WSL = _is_wsl()


def _get_wsl_windows_appdata():
    """
    Resolve the Windows %APPDATA% path from inside WSL.
    Strategy 1: Ask Windows directly via cmd.exe and convert with wslpath.
    Strategy 2: Scan /mnt/c/Users/ for folders that have Antigravity installed.
    Returns a WSL-accessible path string, or None if resolution fails.
    """
    # Strategy 1: cmd.exe %APPDATA% → wslpath
    try:
        proc = subprocess.run(
            ['cmd.exe', '/c', 'echo %APPDATA%'],
            capture_output=True, text=True, check=True
        )
        win_path = proc.stdout.strip()
        if win_path and win_path != "%APPDATA%":
            proc_wsl = subprocess.run(
                ['wslpath', win_path],
                capture_output=True, text=True, check=True
            )
            wsl_path = proc_wsl.stdout.strip()
            if os.path.exists(wsl_path):
                return wsl_path
    except Exception:
        pass

    # Strategy 2: Scan /mnt/c/Users/ for user folders that have Antigravity
    if os.path.exists("/mnt/c/Users"):
        _skip = {"Default", "Default User", "All Users", "desktop.ini", "Public"}
        try:
            for user in os.listdir("/mnt/c/Users"):
                if user in _skip:
                    continue
                appdata = os.path.join("/mnt/c/Users", user, "AppData", "Roaming")
                if not os.path.exists(appdata):
                    continue
                for name in _ANTIGRAVITY_NAMES:
                    if os.path.exists(os.path.join(appdata, name)):
                        return appdata
        except Exception:
            pass

    return None


def _first_existing(*candidates):
    """Return the first path that exists on disk, or the first candidate if none exist."""
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


def _existing_paths(*candidates):
    """Return all candidate paths that exist on disk, preserving order."""
    return [p for p in candidates if p and os.path.exists(p)]


if _SYSTEM == "Windows":
    _appdata = os.path.expandvars(r"%APPDATA%")
    _profile = os.path.expandvars(r"%USERPROFILE%")
    _gemini = os.path.join(_profile, ".gemini")

    _DB_CANDIDATES = (
        os.path.join(_appdata, "Antigravity IDE", "User", "globalStorage", "state.vscdb"),
        os.path.join(_appdata, "antigravity", "User", "globalStorage", "state.vscdb"),
        os.path.join(_appdata, "Antigravity", "User", "globalStorage", "state.vscdb"),
    )
    DB_PATH = _first_existing(*_DB_CANDIDATES)
    CONVERSATIONS_DIR = _first_existing(
        os.path.join(_gemini, "antigravity-ide", "conversations"),
        os.path.join(_gemini, "antigravity", "conversations"),
    )
    BRAIN_DIR = _first_existing(
        os.path.join(_gemini, "antigravity-ide", "brain"),
        os.path.join(_gemini, "antigravity", "brain"),
    )
    WORKSPACE_STORAGE_DIR = _first_existing(
        os.path.join(_appdata, "Antigravity IDE", "User", "workspaceStorage"),
        os.path.join(_appdata, "antigravity", "User", "workspaceStorage"),
    )
    _ALL_CONV_DIRS = [
        os.path.join(_gemini, "antigravity-ide", "conversations"),
        os.path.join(_gemini, "antigravity", "conversations"),
        os.path.join(_gemini, "antigravity-backup", "conversations"),
    ]
    _ALL_BRAIN_DIRS = [
        os.path.join(_gemini, "antigravity-ide", "brain"),
        os.path.join(_gemini, "antigravity", "brain"),
        os.path.join(_gemini, "antigravity-backup", "brain"),
    ]
elif _IS_WSL:
    _wsl_appdata = _get_wsl_windows_appdata()
    _home = os.path.expanduser("~")

    if _wsl_appdata:
        _DB_CANDIDATES = (
            os.path.join(_wsl_appdata, "Antigravity IDE", "User", "globalStorage", "state.vscdb"),
            os.path.join(_wsl_appdata, "antigravity", "User", "globalStorage", "state.vscdb"),
            os.path.join(_wsl_appdata, "Antigravity", "User", "globalStorage", "state.vscdb"),
        )
        DB_PATH = _first_existing(*_DB_CANDIDATES)
        WORKSPACE_STORAGE_DIR = _first_existing(
            os.path.join(_wsl_appdata, "Antigravity IDE", "User", "workspaceStorage"),
            os.path.join(_wsl_appdata, "antigravity", "User", "workspaceStorage"),
            os.path.join(_wsl_appdata, "Antigravity", "User", "workspaceStorage"),
        )
    else:
        _DB_CANDIDATES = ()
        DB_PATH = ""
        WORKSPACE_STORAGE_DIR = ""

    CONVERSATIONS_DIR = _first_existing(
        os.path.join(_home, ".gemini", "antigravity-ide", "conversations"),
        os.path.join(_home, ".gemini", "antigravity", "conversations"),
    )
    BRAIN_DIR = _first_existing(
        os.path.join(_home, ".gemini", "antigravity-ide", "brain"),
        os.path.join(_home, ".gemini", "antigravity", "brain"),
    )
    _gemini_wsl = os.path.join(_home, ".gemini")
    _ALL_CONV_DIRS = [
        os.path.join(_gemini_wsl, "antigravity-ide", "conversations"),
        os.path.join(_gemini_wsl, "antigravity", "conversations"),
        os.path.join(_gemini_wsl, "antigravity-backup", "conversations"),
    ]
    _ALL_BRAIN_DIRS = [
        os.path.join(_gemini_wsl, "antigravity-ide", "brain"),
        os.path.join(_gemini_wsl, "antigravity", "brain"),
        os.path.join(_gemini_wsl, "antigravity-backup", "brain"),
    ]
elif _SYSTEM == "Darwin":  # macOS
    _home = os.path.expanduser("~")
    _support = os.path.join(_home, "Library", "Application Support")

    _DB_CANDIDATES = (
        os.path.join(_support, "Antigravity IDE", "User", "globalStorage", "state.vscdb"),
        os.path.join(_support, "antigravity", "User", "globalStorage", "state.vscdb"),
    )
    DB_PATH = _first_existing(*_DB_CANDIDATES)
    CONVERSATIONS_DIR = _first_existing(
        os.path.join(_home, ".gemini", "antigravity-ide", "conversations"),
        os.path.join(_home, ".gemini", "antigravity", "conversations"),
    )
    BRAIN_DIR = _first_existing(
        os.path.join(_home, ".gemini", "antigravity-ide", "brain"),
        os.path.join(_home, ".gemini", "antigravity", "brain"),
    )
    WORKSPACE_STORAGE_DIR = _first_existing(
        os.path.join(_support, "Antigravity IDE", "User", "workspaceStorage"),
        os.path.join(_support, "antigravity", "User", "workspaceStorage"),
    )
    _gemini_mac = os.path.join(_home, ".gemini")
    _ALL_CONV_DIRS = [
        os.path.join(_gemini_mac, "antigravity-ide", "conversations"),
        os.path.join(_gemini_mac, "antigravity", "conversations"),
        os.path.join(_gemini_mac, "antigravity-backup", "conversations"),
    ]
    _ALL_BRAIN_DIRS = [
        os.path.join(_gemini_mac, "antigravity-ide", "brain"),
        os.path.join(_gemini_mac, "antigravity", "brain"),
        os.path.join(_gemini_mac, "antigravity-backup", "brain"),
    ]
else:  # Linux and other POSIX systems
    _home = os.path.expanduser("~")
    _config = os.path.join(_home, ".config")

    _DB_CANDIDATES = (
        os.path.join(_config, "Antigravity IDE", "User", "globalStorage", "state.vscdb"),
        os.path.join(_config, "Antigravity", "User", "globalStorage", "state.vscdb"),
    )
    DB_PATH = _first_existing(*_DB_CANDIDATES)
    CONVERSATIONS_DIR = _first_existing(
        os.path.join(_home, ".gemini", "antigravity-ide", "conversations"),
        os.path.join(_home, ".gemini", "antigravity", "conversations"),
    )
    BRAIN_DIR = _first_existing(
        os.path.join(_home, ".gemini", "antigravity-ide", "brain"),
        os.path.join(_home, ".gemini", "antigravity", "brain"),
    )
    WORKSPACE_STORAGE_DIR = _first_existing(
        os.path.join(_config, "Antigravity IDE", "User", "workspaceStorage"),
        os.path.join(_config, "Antigravity", "User", "workspaceStorage"),
    )
    _gemini_linux = os.path.join(_home, ".gemini")
    _ALL_CONV_DIRS = [
        os.path.join(_gemini_linux, "antigravity-ide", "conversations"),
        os.path.join(_gemini_linux, "antigravity", "conversations"),
        os.path.join(_gemini_linux, "antigravity-backup", "conversations"),
    ]
    _ALL_BRAIN_DIRS = [
        os.path.join(_gemini_linux, "antigravity-ide", "brain"),
        os.path.join(_gemini_linux, "antigravity", "brain"),
        os.path.join(_gemini_linux, "antigravity-backup", "brain"),
    ]

def unique_existing_files(paths):
    """
    Return existing paths with duplicates removed, preserving order.

    On case-insensitive file systems (Windows, default macOS, WSL /mnt/c)
    ``.../Antigravity/...`` and ``.../antigravity/...`` are the same file and
    must be read, backed up and written only once.
    """
    unique = []
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        duplicate = False
        for seen in unique:
            try:
                if os.path.samefile(seen, path):
                    duplicate = True
                    break
            except OSError:
                if os.path.normcase(os.path.abspath(seen)) == os.path.normcase(os.path.abspath(path)):
                    duplicate = True
                    break
        if not duplicate:
            unique.append(path)
    return unique


DB_PATHS = unique_existing_files(_DB_CANDIDATES)
_ALL_BRAIN_DIRS = unique_existing_files(_ALL_BRAIN_DIRS) or _ALL_BRAIN_DIRS


def _backup_dir():
    """
    Folder where rollback backups are written: ``ChatVault_index_backups``
    next to the executable (or the package), falling back to the home folder.

    When frozen with PyInstaller (--onefile), __file__ points inside the
    temporary _MEIxxxx extraction folder, which Windows deletes the moment
    the exe exits, so the executable's own folder is used instead.
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for candidate in (base, os.getcwd(), os.path.expanduser("~")):
        folder = os.path.join(candidate, "ChatVault_index_backups")
        try:
            os.makedirs(folder, exist_ok=True)
            if os.access(folder, os.W_OK):
                return folder
        except OSError:
            continue
    return os.getcwd()


def _find_brain_path(conversation_id):
    """Return the first existing brain folder for this conversation across all locations."""
    for brain_dir in _ALL_BRAIN_DIRS:
        p = os.path.join(brain_dir, conversation_id)
        if os.path.isdir(p):
            return p
    return None


def _iter_brain_paths(conversation_id):
    """Yield every matching brain folder in configured priority order."""
    for brain_dir in _ALL_BRAIN_DIRS:
        path = os.path.join(brain_dir, conversation_id)
        if os.path.isdir(path):
            yield path


def _conversation_rank(path):
    """Sort key: a .db beats a legacy .pb, then the newest file wins."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    return (1 if path.lower().endswith(".db") else 0, mtime)


def _collect_all_conversations():
    """
    Merge conversation files from all folders (new, old, backup).
    Supports both .pb (protobuf, legacy) and .db (SQLite, v2.x+) formats.
    For an ID present more than once, a .db beats a .pb and then the most
    recently modified copy wins; on a tie the earlier folder (new > old >
    backup) is kept.
    Returns dict: {conversation_id: full_file_path}
    """
    catalog = {}
    for conv_dir in _ALL_CONV_DIRS:
        if not os.path.isdir(conv_dir):
            continue
        try:
            names = sorted(os.listdir(conv_dir))
        except OSError:
            continue
        for name in names:
            lower_name = name.lower()
            if not (lower_name.endswith(".db") or lower_name.endswith(".pb")):
                continue
            cid = name[:-3]
            path = os.path.join(conv_dir, name)
            if not os.path.isfile(path):
                continue
            current = catalog.get(cid)
            if current is None or _conversation_rank(path) > _conversation_rank(current):
                catalog[cid] = path
    return catalog


# ─── Protobuf Varint Helpers ─────────────────────────────────────────────────

def encode_varint(value):
    """Encode an integer as a protobuf varint."""
    result = b""
    while value > 0x7F:
        result += bytes([(value & 0x7F) | 0x80])
        value >>= 7
    result += bytes([value & 0x7F])
    return result or b'\x00'


def decode_varint(data, pos):
    """Decode a protobuf varint at the given position. Returns (value, new_pos)."""
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos + 1
        shift += 7
        pos += 1
    return result, pos


def skip_protobuf_field(data, pos, wire_type):
    """Skip over a protobuf field value at the given position. Returns new_pos."""
    if wire_type == 0:    # varint
        _, pos = decode_varint(data, pos)
    elif wire_type == 2:  # length-delimited
        length, pos = decode_varint(data, pos)
        pos += length
    elif wire_type == 1:  # 64-bit fixed
        pos += 8
    elif wire_type == 5:  # 32-bit fixed
        pos += 4
    return pos


def strip_field_from_protobuf(data, target_field_number):
    """
    Remove all instances of a specific field from raw protobuf bytes.
    Returns the remaining bytes with the target field stripped out.
    """
    remaining = b""
    pos = 0
    while pos < len(data):
        start_pos = pos
        try:
            tag, pos = decode_varint(data, pos)
        except Exception:
            remaining += data[start_pos:]
            break
        wire_type = tag & 7
        field_num = tag >> 3
        new_pos = skip_protobuf_field(data, pos, wire_type)
        if new_pos == pos and wire_type not in (0, 1, 2, 5):
            # Unknown wire type — keep everything from here
            remaining += data[start_pos:]
            break
        pos = new_pos
        if field_num != target_field_number:
            remaining += data[start_pos:pos]
    return remaining


def iter_length_delimited_fields(data, target_field_number):
    """Yield well-formed length-delimited values for one protobuf field."""
    pos = 0
    try:
        while pos < len(data):
            tag, pos = decode_varint(data, pos)
            field_number, wire_type = tag >> 3, tag & 7
            if wire_type == 2:
                length, pos = decode_varint(data, pos)
                end = pos + length
                if end > len(data):
                    return
                if field_number == target_field_number:
                    yield data[pos:end]
                pos = end
            else:
                new_pos = skip_protobuf_field(data, pos, wire_type)
                if new_pos <= pos or new_pos > len(data):
                    return
                pos = new_pos
    except Exception:
        return


# ─── Protobuf Write Helpers ──────────────────────────────────────────────────

def encode_length_delimited(field_number, data):
    """Encode a length-delimited protobuf field (wire type 2)."""
    tag = (field_number << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data


def encode_string_field(field_number, string_value):
    """Encode a string as a protobuf field."""
    return encode_length_delimited(field_number, string_value.encode('utf-8'))


# ─── Workspace Helpers ───────────────────────────────────────────────────────

def _is_remote_uri(path_or_uri):
    """Check if a string is already a remote/absolute URI (not a local path)."""
    return path_or_uri.startswith("vscode-remote://") or path_or_uri.startswith("file:///")


def path_to_workspace_uri(folder_path):
    """
    Convert a local folder path to a file:/// URI in the form Antigravity
    itself stores: lower-case drive, literal colon, percent-encoded path.
    Passes through remote URIs (vscode-remote://, file:///) unchanged.
    Example: D:\\Repos\\My Project  →  file:///d:/Repos/My%20Project
    WSL:     /mnt/c/Users/name/Project → file:///c:/Users/name/Project
    """
    # Pass through URIs that are already in the correct format
    if _is_remote_uri(folder_path):
        return folder_path

    def encode(rest):
        return quote(rest, safe="/:@!$&'()*+,;=-._~")

    # WSL: convert /mnt/<drive>/... to file:///<drive>:/...
    if _IS_WSL and folder_path.startswith("/mnt/"):
        parts = folder_path.split("/")
        if len(parts) >= 3 and len(parts[2]) == 1:
            drive = parts[2].lower()
            rest = "/".join(parts[3:])
            return f"file:///{drive}:/{encode(rest)}"

    p = folder_path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        rest = p[2:]
    else:
        drive = None
        rest = p

    if drive:
        return f"file:///{drive}:{encode(rest)}"
    else:
        return f"file:///{encode(rest.lstrip('/'))}"


def build_workspace_field(folder_path):
    """
    Build protobuf field 9 (workspace sub-message) from a filesystem path.
    Sub-message structure:
      sub-field 1 (string) = workspace URI
      sub-field 2 (string) = workspace URI (duplicate)
    Returns raw bytes for one field-9 entry.
    """
    uri = path_to_workspace_uri(folder_path)
    sub_msg = (
        encode_string_field(1, uri)
        + encode_string_field(2, uri)
    )
    return encode_length_delimited(9, sub_msg)


_WORKSPACE_PREFIXES = ("file:///", "vscode-remote://")


def _decode_uri_bytes(raw):
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    return text if text.startswith(_WORKSPACE_PREFIXES) else None


def extract_workspace_hint(inner_blob):
    """
    Return the workspace URI stored in a trajectory summary, or None.

    Antigravity stores the workspace as field 9, a sub-message whose
    sub-field 1 is the workspace URI (sub-field 2 is the repository root and
    sub-field 3 git remote details). Only that URI string is returned — never
    the raw sub-message bytes, which v1.07 mistook for the URI.
    """
    if not inner_blob:
        return None
    for workspace in iter_length_delimited_fields(inner_blob, 9):
        for raw_uri in iter_length_delimited_fields(workspace, 1):
            uri = _decode_uri_bytes(raw_uri)
            if uri:
                return uri
    # Defensive fallback for layouts where a top-level string is the URI.
    pos = 0
    try:
        while pos < len(inner_blob):
            tag, pos = decode_varint(inner_blob, pos)
            field_num, wire_type = tag >> 3, tag & 7
            if wire_type == 2:
                length, pos = decode_varint(inner_blob, pos)
                content = inner_blob[pos:pos + length]
                pos += length
                if field_num > 1:
                    uri = _decode_uri_bytes(content)
                    if uri:
                        return uri
            elif wire_type in (0, 1, 5):
                pos = skip_protobuf_field(inner_blob, pos, wire_type)
            else:
                break
    except Exception:
        pass
    return None


def load_known_workspace_uris():
    """
    Load all known workspace URIs from Antigravity's workspaceStorage.
    Each subfolder contains a workspace.json with a 'folder' or 'workspace' URI.
    Returns a list of URI strings sorted longest-first for prefix matching.
    """
    uris = []
    if not os.path.isdir(WORKSPACE_STORAGE_DIR):
        return uris
    try:
        for name in os.listdir(WORKSPACE_STORAGE_DIR):
            ws_json = os.path.join(WORKSPACE_STORAGE_DIR, name, "workspace.json")
            if os.path.exists(ws_json):
                try:
                    with open(ws_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    uri = data.get("folder") or data.get("workspace")
                    if uri:
                        uris.append(uri)
                except Exception:
                    pass
    except Exception:
        pass
    # Sort longest first so more-specific paths match before parent paths
    uris.sort(key=len, reverse=True)
    return uris


def _uri_to_local_path(file_uri):
    """
    Convert a file:/// URI to a local filesystem path.
    Handles URL-encoding (e.g. %20 -> space, %3A -> colon).
    On WSL, converts file:///C:/... to /mnt/c/...
    Returns None for non-file URIs.
    """
    if not file_uri.startswith("file:///"):
        return None
    raw = unquote(file_uri[len("file://"):])
    # On Windows, file:///C:/... -> C:/...
    if _SYSTEM == "Windows" and len(raw) >= 3 and raw[0] == '/' and raw[2] == ':':
        raw = raw[1:]  # strip leading /
    # On WSL, file:///C:/... -> /mnt/c/...
    elif _IS_WSL and len(raw) >= 3 and raw[0] == '/' and raw[2] == ':':
        drive = raw[1].lower()
        raw = f"/mnt/{drive}{raw[3:]}"
    return raw


def infer_workspace_from_brain(conversation_id, known_ws_uris=None):
    """
    Scan brain .md files for file:/// and vscode-remote:// paths and infer
    the workspace by matching against known workspace URIs.
    Falls back to a heuristic depth-based approach if no known URIs match.
    Returns a filesystem path string, a remote URI string, or None.
    """
    brain_paths = list(_iter_brain_paths(conversation_id))
    if not brain_paths:
        return None

    # Two separate patterns: local file:/// and remote vscode-remote://
    if _SYSTEM == "Windows":
        local_pattern = re.compile(r"file:///([A-Za-z](?:%3A|:)/[^)\s\"'\]>]+)")
    else:
        local_pattern = re.compile(r"file:///([^)\s\"'\]>]+)")
    remote_pattern = re.compile(r"(vscode-remote://[^)\s\"'\]>]+)")

    # Collect all file URIs found in brain .md files
    found_uris = []     # full file:/// URIs
    found_remote = []   # full vscode-remote:// URIs

    for brain_path in brain_paths:
        try:
            names = os.listdir(brain_path)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".md") or name.startswith("."):
                continue
            filepath = os.path.join(brain_path, name)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(16384)

                for match in remote_pattern.finditer(content):
                    found_remote.append(match.group(1))

                for match in local_pattern.finditer(content):
                    found_uris.append("file:///" + match.group(1))
            except Exception:
                pass

    if not found_uris and not found_remote:
        return None

    def normalize_file_uri(uri):
        value = unquote(uri).replace("\\", "/")
        # Windows drive letters and paths are case-insensitive.
        return value.lower() if _SYSTEM == "Windows" or _IS_WSL else value

    # ── Strategy 1: Match against known workspace URIs (preferred) ────────
    if known_ws_uris:
        ws_counts = {}
        for file_uri in found_uris:
            normalized = normalize_file_uri(file_uri)
            for ws_uri in known_ws_uris:
                ws_norm = normalize_file_uri(ws_uri).rstrip("/")
                if normalized.startswith(ws_norm + "/") or normalized == ws_norm:
                    ws_counts[ws_uri] = ws_counts.get(ws_uri, 0) + 1
                    break  # matched most-specific (sorted longest-first)

        for remote_uri in found_remote:
            for ws_uri in known_ws_uris:
                if remote_uri.startswith(ws_uri + "/") or remote_uri == ws_uri:
                    ws_counts[ws_uri] = ws_counts.get(ws_uri, 0) + 1
                    break

        if ws_counts:
            best_ws_uri = max(ws_counts, key=ws_counts.get)
            local = _uri_to_local_path(best_ws_uri)
            if local:
                return local
            return best_ws_uri

    # ── Strategy 2: Fallback — heuristic depth-based approach ─────────────
    path_counts = {}
    for file_uri in found_uris:
        raw = file_uri[len("file:///"):]
        raw = raw.replace("%3A", ":").replace("%3a", ":")
        raw = raw.replace("%20", " ")

        # WSL: normalize Windows drive letters in URIs to /mnt/ paths
        if _IS_WSL and len(raw) >= 2 and raw[1] == ':':
            drive = raw[0].lower()
            raw = f"mnt/{drive}/{raw[3:]}"

        parts = raw.replace("\\", "/").split("/")
        # On Windows paths like C:/Users/name/Desktop/Project → 5 segments.
        # On WSL paths like mnt/c/Users/name/Project → 5 segments.
        # On Linux/Mac like home/user/projects/Project → 4 segments + re-add /.
        if _SYSTEM == "Windows":
            depth = 5
        elif _IS_WSL and raw.startswith("mnt/"):
            depth = 5
        else:
            depth = 4
        if len(parts) >= depth:
            ws = "/".join(parts[:depth])
            if _SYSTEM != "Windows" and not ws.startswith("/"):
                ws = "/" + ws
            path_counts[ws] = path_counts.get(ws, 0) + 1

    for remote_uri in found_remote:
        path_counts[remote_uri] = path_counts.get(remote_uri, 0) + 1

    if not path_counts:
        return None

    best = max(path_counts, key=path_counts.get)
    # Remote URIs are returned as-is; local paths get OS-native separators
    if best.startswith("vscode-remote://"):
        return best
    return best.replace("/", os.sep)


# ─── Timestamp Helpers ───────────────────────────────────────────────────────

def build_timestamp_fields(epoch_seconds):
    """
    Build protobuf timestamp fields 3, 7, and 10 from an epoch timestamp.
    Each is a sub-message with: sub-field 1 (varint) = seconds since epoch.
    Returns raw protobuf bytes containing all three fields.
    """
    seconds = int(epoch_seconds)
    ts_inner = encode_varint((1 << 3) | 0) + encode_varint(seconds)
    return (
        encode_length_delimited(3, ts_inner)
        + encode_length_delimited(7, ts_inner)
        + encode_length_delimited(10, ts_inner)
    )


def build_timestamp_field(field_number, epoch_seconds):
    """Build one protobuf timestamp field with seconds in nested field 1."""
    ts_inner = encode_varint((1 << 3) | 0) + encode_varint(int(epoch_seconds))
    return encode_length_delimited(field_number, ts_inner)


def extract_timestamp_seconds(inner_blob, field_number=3):
    """Return the newest seconds value found in a repeated timestamp field."""
    values = []
    for timestamp_blob in iter_length_delimited_fields(inner_blob or b"", field_number):
        pos = 0
        try:
            while pos < len(timestamp_blob):
                tag, pos = decode_varint(timestamp_blob, pos)
                nested_field, wire_type = tag >> 3, tag & 7
                if nested_field == 1 and wire_type == 0:
                    seconds, pos = decode_varint(timestamp_blob, pos)
                    values.append(seconds)
                    break
                new_pos = skip_protobuf_field(timestamp_blob, pos, wire_type)
                if new_pos <= pos or new_pos > len(timestamp_blob):
                    break
                pos = new_pos
        except Exception:
            continue
    return max(values) if values else None


def has_timestamp_fields(inner_blob):
    """Check if the inner blob already contains timestamp fields (3, 7, or 10)."""
    if not inner_blob:
        return False
    try:
        pos = 0
        while pos < len(inner_blob):
            tag, pos = decode_varint(inner_blob, pos)
            fn = tag >> 3
            wt = tag & 7
            if fn in (3, 7, 10):
                return True
            pos = skip_protobuf_field(inner_blob, pos, wt)
    except Exception:
        pass
    return False


# ─── Interactive Workspace Assignment ────────────────────────────────────────

def _prompt_valid_folder(prompt_text):
    """Keep asking for a folder until user gives a valid one or presses Enter."""
    while True:
        raw = input(prompt_text).strip()
        if raw == "":
            return None
        folder = raw.strip('"').strip("'").rstrip("\\/")
        # Accept remote URIs without filesystem validation
        if _is_remote_uri(folder):
            print(f"    + Mapped remote URI: {folder}")
            return folder
        if os.path.isdir(folder):
            print(f"    + Mapped to {folder}")
            return folder
        else:
            print(f"    x Path not found: {folder}")
            print(f"      (Make sure the folder exists. Try again or press Enter to skip)")


def interactive_workspace_assignment(unmapped_entries):
    """
    Show unmapped conversations and let user assign workspace paths.
    unmapped_entries: list of (index, conversation_id, title)
    Returns dict: {conversation_id: folder_path}
    """
    if not unmapped_entries:
        return {}

    print()
    print("  " + "=" * 58)
    print("  WORKSPACE ASSIGNMENT (optional)")
    print("  " + "=" * 58)
    print(f"  {len(unmapped_entries)} conversation(s) have no workspace.")
    print("  You can assign each to a workspace folder now,")
    print("  or press Enter to skip and leave them unassigned.")
    print()

    assignments = {}
    batch_path = None

    for idx, cid, title in unmapped_entries:
        if batch_path:
            assignments[cid] = batch_path
            print(f"    [{idx:3d}] {title[:45]}  -> {os.path.basename(batch_path)}")
            continue

        print(f"  [{idx:3d}] {title[:55]}")
        while True:
            raw = input("    Workspace path (Enter=skip, 'all'=batch, 'q'=stop): ").strip()
            if raw == "":
                print("    Skipped.")
                break
            if raw.lower() == "q":
                print("    Stopped — remaining conversations left unmapped.")
                return assignments
            if raw.lower() == "all":
                folder = _prompt_valid_folder("    Path for ALL remaining (Enter=cancel): ")
                if folder is None:
                    continue
                batch_path = folder
                assignments[cid] = folder
                break
            # Normal path entry
            folder = raw.strip('"').strip("'").rstrip("\\/")
            # Accept remote URIs without filesystem validation
            if _is_remote_uri(folder):
                print(f"    + Mapped remote URI: {folder}")
                assignments[cid] = folder
                break
            if os.path.isdir(folder):
                print(f"    + Mapped to {folder}")
                assignments[cid] = folder
                break
            else:
                print(f"    x Path not found: {folder}")
                print(f"      (Try again or press Enter to skip)")

    if assignments:
        print()
        print(f"  + Assigned workspace to {len(assignments)} conversation(s)")
    print()
    return assignments


# ─── Metadata Extraction ─────────────────────────────────────────────────────

def _is_generated_fallback_title(title, conversation_id):
    """Return True only for this tool's exact date/ID fallback formats."""
    short_id = re.escape(conversation_id[:8])
    return bool(
        re.fullmatch(r"Conversation \([A-Z][a-z]{2} \d{2}\) " + short_id, title)
        or title == "Conversation " + conversation_id[:8]
    )


def extract_existing_metadata(db_path):
    """
    Read metadata already stored in the database's trajectory data.
    Returns two dicts:
      - titles:      {conversation_id: title}  (real, non-fallback titles)
      - inner_blobs: {conversation_id: raw_inner_protobuf_bytes}
    The inner_blobs contain workspace URIs, timestamps, tool state, etc.
    These are preserved so re-running the script doesn't lose data.
    """
    titles = {}
    inner_blobs = {}
    if not os.path.isfile(db_path):
        return titles, inner_blobs
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable "
                "WHERE key='antigravityUnifiedStateSync.trajectorySummaries'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as error:
        raise IndexReadError(f"Cannot read {db_path}: {error}")

    if not row or not row[0]:
        return titles, inner_blobs

    try:
        value = row[0]
        if isinstance(value, bytes):
            value = value.decode("ascii")
        decoded = base64.b64decode(value, validate=True)
    except Exception as error:
        raise IndexReadError(f"Sidebar index in {db_path} is not valid base64: {error}")

    entries = list(iter_length_delimited_fields(decoded, 1))
    if decoded and not entries:
        raise IndexReadError(f"Sidebar index in {db_path} has an unknown layout.")

    for entry in entries:
        uid_bytes = next(iter_length_delimited_fields(entry, 1), None)
        info = next(iter_length_delimited_fields(entry, 2), None)
        info_b64 = next(iter_length_delimited_fields(info, 1), None) if info else None
        if uid_bytes is None or info_b64 is None:
            raise IndexReadError(f"Sidebar index in {db_path} has an entry without an ID or data.")
        uid = uid_bytes.decode("utf-8", errors="replace")
        try:
            raw_inner = base64.b64decode(info_b64, validate=True)
        except Exception as error:
            raise IndexReadError(f"Entry {uid} in {db_path} is not valid base64: {error}")
        inner_blobs[uid] = raw_inner
        title_bytes = next(iter_length_delimited_fields(raw_inner, 1), None)
        if title_bytes is not None:
            title = title_bytes.decode("utf-8", errors="replace")
            if title and not _is_generated_fallback_title(title, uid):
                titles[uid] = title

    return titles, inner_blobs


def extract_existing_metadata_from_paths(db_paths):
    """
    Read metadata from ALL existing Antigravity databases.
    First DB wins for each conversation ID, so metadata is not overwritten
    by a later DB that might have stale data.
    """
    merged_titles = {}
    merged_inner_blobs = {}
    for db_path in db_paths:
        titles, inner_blobs = extract_existing_metadata(db_path)
        for cid, title in titles.items():
            if cid not in merged_titles:
                merged_titles[cid] = title
        for cid, blob in inner_blobs.items():
            if cid not in merged_inner_blobs:
                merged_inner_blobs[cid] = blob
    return merged_titles, merged_inner_blobs


_INDEX_KEY = "antigravityUnifiedStateSync.trajectorySummaries"


def _unique_backup_path(folder, base_name):
    """Return folder/base_name.txt, adding _2, _3 ... so nothing is overwritten."""
    candidate = os.path.join(folder, base_name + ".txt")
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base_name}_{counter}.txt")
        counter += 1
    return candidate


def read_index_value(db_path):
    """Return the raw stored index value (or None) for one state.vscdb."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key=?", (_INDEX_KEY,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def backup_index(db_path, backup_suffix, folder=None):
    """
    Save the current index value of one database to a never-overwritten
    text file. Returns (backup_path or None, original_value or None).
    """
    value = read_index_value(db_path)
    if not value:
        return None, value
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="replace")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = "trajectorySummaries_backup"
    if backup_suffix:
        name += "_" + backup_suffix
    path = _unique_backup_path(folder or _backup_dir(), f"{name}_{stamp}")
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(value)
    except OSError as e:
        raise RuntimeError(
            f"Could not write rollback backup to {path}: {e}\n"
            f"    Refusing to modify the database without a backup."
        )
    return path, value


def write_index_value(db_path, encoded_value):
    """Write (insert or replace) the index value in one state.vscdb."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE ItemTable SET value=? WHERE key=?", (encoded_value, _INDEX_KEY)
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                    (_INDEX_KEY, encoded_value),
                )
    finally:
        conn.close()


def _db_app_name(db_path):
    return os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(db_path))))


def write_index_to_all(db_paths, encoded_value, folder=None):
    """
    Back up every database first, then write each one. If any write fails,
    restore the databases already written and re-raise.
    Returns a list of (db_path, backup_path or None).
    """
    prepared = []
    for db_path in db_paths:
        suffix = re.sub(r"[^A-Za-z0-9]+", "_", _db_app_name(db_path)).strip("_").lower()
        backup_path, original = backup_index(db_path, suffix, folder)
        prepared.append((db_path, backup_path, original))

    written = []
    try:
        for db_path, _, original in prepared:
            write_index_value(db_path, encoded_value)
            written.append((db_path, original))
    except Exception:
        for db_path, original in written:
            try:
                if original is not None:
                    write_index_value(db_path, original)
            except Exception:
                pass
        raise
    return [(db_path, backup_path) for db_path, backup_path, _ in prepared]


def restore_index_backup(db_path, backup_file):
    """Put a previously saved index backup back into one database."""
    with open(backup_file, "r", encoding="utf-8") as f:
        value = f.read().strip()
    base64.b64decode(value, validate=True)  # refuse obviously wrong files
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", _db_app_name(db_path)).strip("_").lower()
    safety, _ = backup_index(db_path, suffix + "_before_undo")
    write_index_value(db_path, value)
    return safety


def _open_sqlite_readonly(db_path):
    """Open an existing SQLite file without allowing SQLite to create it."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(db_path)
    absolute = os.path.abspath(db_path).replace("\\", "/")
    uri = "file:" + quote(absolute, safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
    except Exception:
        conn.close()
        raise
    return conn


def _quote_sqlite_identifier(identifier):
    """Safely quote a SQLite table or column identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _find_step_table(conn):
    """Return the quoted step table name and whether it has an idx column."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    for (table_name,) in cursor.fetchall():
        quoted = _quote_sqlite_identifier(table_name)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(" + quoted + ")").fetchall()
        }
        if {"step_type", "step_payload"}.issubset(columns):
            return quoted, "idx" in columns
    return None, False


def normalize_prompt_text(prompt):
    """Normalize prompt whitespace without otherwise rewriting its contents."""
    if not isinstance(prompt, str):
        return ""
    return re.sub(r"\s+", " ", prompt).strip()


def _collapse_prompt_urls(prompt):
    """Replace HTTP(S) URLs with their hostnames, retaining sentence punctuation."""
    def replace_url(match):
        raw_url = match.group(0)
        url = raw_url
        trailing = ""
        while url and url[-1] in ".,!?;:)]}":
            trailing = url[-1] + trailing
            url = url[:-1]
        try:
            hostname = urlparse(url).hostname
        except ValueError:  # e.g. a malformed "http://[..." IPv6-style link
            hostname = None
        return (hostname or url) + trailing

    return re.sub(r"https?://[^\s<>()]+", replace_url, prompt, flags=re.IGNORECASE)


def _truncate_prompt_title(text, max_chars=60, max_words=10):
    """Return a word-boundary title within both limits, adding an ellipsis."""
    words = text.split()
    truncated = len(words) > max_words
    words = words[:max_words]
    result = ""
    for word in words:
        candidate = word if not result else result + " " + word
        if len(candidate) > max_chars:
            truncated = True
            break
        result = candidate
    if not result and text:
        result = text[:max_chars].rstrip()
        truncated = len(text) > len(result)
    if truncated:
        result = result.rstrip(".,;:!?")
        while result and len(result) + 1 > max_chars:
            if " " in result:
                result = result.rsplit(" ", 1)[0]
            else:
                result = result[:max_chars - 1].rstrip()
                break
        result += "…"
    return result


def format_prompt_title(prompt):
    """Create a concise, deterministic title from a user prompt."""
    normalized = normalize_prompt_text(prompt)
    if not normalized:
        return None
    normalized = normalize_prompt_text(_collapse_prompt_urls(normalized))
    normalized = re.sub(r"^#{1,6}\s+", "", normalized)

    sentence_match = re.match(r"^(.+?[.!?])(?:\s|$)", normalized)
    if sentence_match:
        first_sentence = sentence_match.group(1).strip()
        if len(first_sentence) <= 60 and len(first_sentence.split()) <= 10:
            return first_sentence

    return _truncate_prompt_title(normalized) or None


def extract_prompt_from_conversation_db(db_path):
    """
    Read the first valid user prompt from a v2 conversation database.

    Prompt text is protobuf field 2 nested inside field 19 of an ordered
    step_type=14 payload. Conversation databases are opened read-only.
    """
    conn = None
    try:
        conn = _open_sqlite_readonly(db_path)
        table, has_idx = _find_step_table(conn)
        if not table:
            return None
        order_column = _quote_sqlite_identifier("idx") if has_idx else "rowid"
        rows = conn.execute(
            "SELECT step_payload FROM " + table
            + " WHERE step_type=? ORDER BY " + order_column,
            (14,),
        )
        for row in rows:
            if not row or row[0] is None:
                continue
            payload = row[0]
            if isinstance(payload, memoryview):
                payload = payload.tobytes()
            if not isinstance(payload, bytes):
                continue
            for title_container in iter_length_delimited_fields(payload, 19):
                for title_bytes in iter_length_delimited_fields(title_container, 2):
                    try:
                        prompt = title_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    prompt = normalize_prompt_text(prompt)
                    if prompt:
                        return prompt
        return None
    except (OSError, sqlite3.Error):
        return None
    finally:
        if conn is not None:
            conn.close()


def extract_title_from_conversation_db(db_path):
    """Derive a concise title from the first valid v2 user prompt."""
    return format_prompt_title(extract_prompt_from_conversation_db(db_path))


_USER_REQUEST_TYPES = {"USER_EXPLICIT", "USER_INPUT"}
_REQUEST_TYPE_KEYS = (
    "type", "request_type", "message_type", "event_type", "kind", "role",
)
_REQUEST_TEXT_KEYS = ("text", "content", "message", "request", "prompt", "input", "query")
_REQUEST_CONTAINER_KEYS = ("data", "payload", "event", "details")


def _unwrap_user_request_text(value):
    """Remove Antigravity's request envelope while tolerating truncated logs."""
    if not isinstance(value, str):
        return ""
    opening = "<USER_REQUEST>"
    start = value.find(opening)
    if start >= 0:
        value = value[start + len(opening):]
        end_positions = [
            value.find(marker)
            for marker in (
                "</USER_REQUEST>",
                "<ADDITIONAL_METADATA>",
                "<USER_SETTINGS_CHANGE>",
                "<truncated",
            )
            if value.find(marker) >= 0
        ]
        if end_positions:
            value = value[:min(end_positions)]
    return normalize_prompt_text(value)


def _text_from_request_value(value):
    """Extract text from common transcript content shapes."""
    if isinstance(value, str):
        return _unwrap_user_request_text(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _text_from_request_value(item)
            if text:
                parts.append(text)
        return normalize_prompt_text(" ".join(parts))
    if isinstance(value, dict):
        for key in _REQUEST_TEXT_KEYS:
            if key in value:
                text = _text_from_request_value(value[key])
                if text:
                    return text
    return ""


def _request_text_from_json(node):
    """Find the first explicitly tagged user request in a JSON value."""
    if isinstance(node, dict):
        request_type = None
        for key in _REQUEST_TYPE_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value.upper() in _USER_REQUEST_TYPES:
                request_type = value.upper()
                break
        if request_type:
            for key in _REQUEST_TEXT_KEYS + _REQUEST_CONTAINER_KEYS:
                if key in node:
                    text = _text_from_request_value(node[key])
                    if text and text.upper() not in _USER_REQUEST_TYPES:
                        return text
        for value in node.values():
            text = _request_text_from_json(value)
            if text:
                return text
    elif isinstance(node, list):
        for value in node:
            text = _request_text_from_json(value)
            if text:
                return text
    return None


def _extract_request_from_log(log_path):
    """Read a transcript/overview line by line and return its first user request."""
    marker_pattern = re.compile(r"\b(?:USER_EXPLICIT|USER_INPUT)\b", re.IGNORECASE)
    waiting_for_text = False
    is_jsonl = log_path.lower().endswith(".jsonl")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as log_file:
            for raw_line in log_file:
                line = raw_line.strip()
                if not line:
                    continue

                try:
                    parsed = json.loads(line)
                except (TypeError, ValueError):
                    parsed = None
                if parsed is not None:
                    text = _request_text_from_json(parsed)
                    if text:
                        return text
                elif is_jsonl:
                    # A partially written JSONL record must not be mistaken for
                    # plaintext. Skip it and keep scanning later complete rows.
                    continue

                marker = marker_pattern.search(line)
                if marker:
                    tail = line[marker.end():].lstrip(" \t:=-|]})\"")
                    tail = tail.rstrip(" \t,\"")
                    if tail:
                        return normalize_prompt_text(tail)
                    waiting_for_text = True
                    continue

                if waiting_for_text:
                    key_value = re.match(
                        r"^(?:text|content|message|request|prompt|input|query)\s*[:=]\s*(.+)$",
                        line,
                        flags=re.IGNORECASE,
                    )
                    candidate = key_value.group(1) if key_value else line
                    candidate = candidate.strip(" \t,\"")
                    candidate = normalize_prompt_text(candidate)
                    if candidate:
                        return candidate
    except OSError:
        return None
    return None


def get_title_from_brain_request(conversation_id):
    """Recover a legacy title from the first readable user request artifact."""
    for brain_path in _iter_brain_paths(conversation_id):
        logs_path = os.path.join(brain_path, ".system_generated", "logs")
        for filename in ("transcript.jsonl", "overview.txt"):
            log_path = os.path.join(logs_path, filename)
            if not os.path.isfile(log_path):
                continue
            prompt = _extract_request_from_log(log_path)
            title = format_prompt_title(prompt)
            if title:
                return title
    return None


def get_title_from_brain(conversation_id):
    """
    Try to extract a title from brain artifact .md files.
    Returns the first markdown heading found, or None.
    """
    for brain_path in _iter_brain_paths(conversation_id):
        for item in sorted(os.listdir(brain_path)):
            if item.startswith('.') or not item.endswith('.md'):
                continue
            try:
                filepath = os.path.join(brain_path, item)
                with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                    for line_number, line in enumerate(f):
                        if line_number >= 100:
                            break
                        heading = line.strip()
                        if heading.startswith('#'):
                            return heading.lstrip('# ').strip()[:80]
            except Exception:
                pass

    return None


def resolve_title(conversation_id, existing_titles, conversation_path=None):
    """
    Determine the best title for a conversation. Priority:
      1. Existing title from database (canonical Antigravity title), unless it
         exactly matches the raw title written by the earlier v1.07 build
      2. Brain artifact .md heading (fallback for new/missing conversations)
      3. A concise title derived from a v2 prompt or readable legacy log
      4. Fallback: date + short UUID
    Returns (title, source), with source identifying the selected title source.
    """
    conv_file = conversation_path
    if not conv_file:
        for conv_dir in _ALL_CONV_DIRS:
            db_path = os.path.join(conv_dir, f"{conversation_id}.db")
            pb_path = os.path.join(conv_dir, f"{conversation_id}.pb")
            if os.path.exists(db_path):
                conv_file = db_path
                break
            if os.path.exists(pb_path):
                conv_file = pb_path
                break

    db_prompt = None
    if conv_file and conv_file.lower().endswith(".db"):
        db_prompt = extract_prompt_from_conversation_db(conv_file)

    # Preserve canonical Antigravity titles. The sole migration exception is
    # the exact normalized-prompt[:80] value written by the first v1.07 build.
    existing_title = existing_titles.get(conversation_id)
    if existing_title is not None:
        old_v107_title = db_prompt[:80] if db_prompt else None
        if old_v107_title is not None and existing_title == old_v107_title:
            migrated_title = format_prompt_title(db_prompt)
            if migrated_title and migrated_title != existing_title:
                return migrated_title, "conversation"
        return existing_title, "preserved"

    # Fall back to brain artifact heading for conversations not yet indexed
    brain_title = get_title_from_brain(conversation_id)
    if brain_title:
        return brain_title, "brain"

    if db_prompt:
        conversation_title = format_prompt_title(db_prompt)
        if conversation_title:
            return conversation_title, "conversation"
    if conv_file and conv_file.lower().endswith(".pb"):
        conversation_title = get_title_from_brain_request(conversation_id)
        if conversation_title:
            return conversation_title, "conversation"
    if conv_file and os.path.exists(conv_file):
        mod_time = time.strftime("%b %d", time.localtime(os.path.getmtime(conv_file)))
        return f"Conversation ({mod_time}) {conversation_id[:8]}", "fallback"

    return f"Conversation {conversation_id[:8]}", "fallback"


# ─── Protobuf Entry Builder ──────────────────────────────────────────────────

def build_trajectory_entry(conversation_id, title, existing_inner_data=None,
                           workspace_path=None, conversation_mtime=None,
                           source_is_db=False):
    """
    Build a single trajectory summary protobuf entry.

    - If existing_inner_data is provided, title (field 1) is replaced but
      ALL other fields (workspace, timestamps, tool state) are preserved.
    - If workspace_path is provided and there is no existing workspace,
      a workspace field (field 9) is injected.
    - Existing .db entries refresh only updated-at field 3 when the source file
      is newer. Existing .pb entries retain their current timestamps.
    - New entries receive timestamp fields 3, 7, and 10 from the source mtime.
    """
    if existing_inner_data:
        preserved_fields = strip_field_from_protobuf(existing_inner_data, 1)
        inner_info = encode_string_field(1, title) + preserved_fields

        # Existing workspace metadata (field 9) is kept byte-for-byte. It holds
        # the workspace URI, the repository root and git remote details in the
        # exact encoding Antigravity wrote; re-encoding it corrupted data.

        # Override workspace if user assigned a new one
        if workspace_path:
            # Strip old workspace (field 9) and inject the new one
            inner_info = strip_field_from_protobuf(inner_info, 9)
            inner_info += build_workspace_field(workspace_path)
        if source_is_db and conversation_mtime:
            indexed_mtime = extract_timestamp_seconds(existing_inner_data, 3)
            if indexed_mtime is None or int(conversation_mtime) > indexed_mtime:
                inner_info = strip_field_from_protobuf(inner_info, 3)
                inner_info += build_timestamp_field(3, conversation_mtime)
        elif conversation_mtime and not has_timestamp_fields(existing_inner_data):
            inner_info += build_timestamp_fields(conversation_mtime)
    else:
        inner_info = encode_string_field(1, title)
        if workspace_path:
            inner_info += build_workspace_field(workspace_path)
        if conversation_mtime:
            inner_info += build_timestamp_fields(conversation_mtime)

    info_b64 = base64.b64encode(inner_info).decode('utf-8')
    sub_message = encode_string_field(1, info_b64)

    entry = encode_string_field(1, conversation_id)
    entry += encode_length_delimited(2, sub_message)
    return entry




# ─── Repair ──────────────────────────────────────────────────────────────────

def is_antigravity_running():
    """Return True when an Antigravity desktop process is detected."""
    from .common import running_apps
    return bool(running_apps(["Antigravity"]))


def _print(*args):
    print(*args, flush=True)


def run_repair(interactive=True, workspace_mode=None, keep_missing=True, ask=input):
    """
    Rebuild the Antigravity sidebar index.

    workspace_mode: "auto" (infer from brain files), "manual" (auto, then
    prompt for the rest), or "none". When None and interactive, the user is
    asked. keep_missing keeps index entries whose conversation file is gone.
    Returns a process exit code.
    """
    _print()
    _print("  Repair Antigravity sidebar index")
    _print("  " + "-" * 58)

    if is_antigravity_running():
        _print("  ERROR: Antigravity is still running.")
        _print("  No files or databases were changed.")
        _print("  Please close it first: File > Exit, or end it in Task Manager.")
        return 1

    if not DB_PATHS:
        _print("  ERROR: Database not found at any known Antigravity location:")
        for candidate in _DB_CANDIDATES:
            _print(f"    {candidate}")
        _print("  Make sure Antigravity has been installed and opened at least once.")
        return 1

    conv_catalog = _collect_all_conversations()
    if not conv_catalog:
        _print("  No conversations found on disk. Nothing to fix.")
        return 0

    conversation_ids = sorted(
        conv_catalog, key=lambda cid: _conversation_rank(conv_catalog[cid])[1], reverse=True
    )
    dir_counts = {}
    for path in conv_catalog.values():
        parent = os.path.basename(os.path.dirname(os.path.dirname(path)))
        dir_counts[parent] = dir_counts.get(parent, 0) + 1
    for folder_name, count in dir_counts.items():
        _print(f"    {folder_name}: {count} conversation(s)")
    _print(f"  Found {len(conversation_ids)} unique conversations on disk")
    _print()

    _print("  Reading existing index from database(s)...")
    for db_path in DB_PATHS:
        _print(f"    {_db_app_name(db_path)}: {db_path}")
    try:
        existing_titles, existing_inner_blobs = extract_existing_metadata_from_paths(DB_PATHS)
    except IndexReadError as error:
        _print(f"  ERROR: {error}")
        _print("  The existing index could not be decoded, so nothing was changed")
        _print("  (rebuilding would lose its titles and workspaces).")
        return 1
    ws_count = sum(1 for v in existing_inner_blobs.values() if extract_workspace_hint(v))
    _print(f"  Found {len(existing_inner_blobs)} indexed conversations, "
           f"{len(existing_titles)} titles, {ws_count} workspace links")
    _print()

    _print("  Conversations (newest first):")
    _print("  " + "-" * 58)
    resolved = []
    stats = {"brain": 0, "preserved": 0, "conversation": 0, "fallback": 0}
    markers = {"brain": "+", "preserved": "~", "conversation": "=", "fallback": "?"}
    for i, cid in enumerate(conversation_ids, 1):
        title, source = resolve_title(cid, existing_titles, conv_catalog.get(cid))
        inner_data = existing_inner_blobs.get(cid)
        has_ws = bool(inner_data and extract_workspace_hint(inner_data))
        resolved.append((cid, title, source, inner_data, has_ws))
        stats[source] += 1
        ws_flag = " [WS]" if has_ws else ""
        _print(f"    [{i:3d}] {markers[source]} {title[:50]}{ws_flag}")
    _print("  " + "-" * 58)
    _print("  Legend: [+] brain  [~] preserved  [=] conversation  [?] fallback  [WS] workspace")
    _print(f"  Totals: {stats['brain']} brain, {stats['preserved']} preserved, "
           f"{stats['conversation']} conversation, {stats['fallback']} fallback")
    _print()

    missing = [cid for cid in existing_inner_blobs if cid not in conv_catalog]
    if missing:
        verb = "Keeping" if keep_missing else "Dropping"
        _print(f"  {verb} {len(missing)} indexed conversation(s) whose file is not on disk.")
        _print()

    unmapped = [(i, cid, title)
                for i, (cid, title, _, _, has_ws) in enumerate(resolved, 1) if not has_ws]
    ws_assignments = {}
    if unmapped:
        mode = workspace_mode
        if mode is None:
            if interactive:
                _print(f"  {len(unmapped)} conversation(s) have no workspace assigned.")
                _print("    Enter or 1: auto-assign from brain files (recommended)")
                _print("    2:          auto-assign, then assign the rest by hand")
                _print("    3:          leave them unassigned")
                choice = ask("  Your choice: ").strip()
                mode = {"2": "manual", "3": "none"}.get(choice, "auto")
            else:
                mode = "auto"
        if mode in ("auto", "manual"):
            known_ws_uris = load_known_workspace_uris()
            _print(f"  Auto-assigning workspaces ({len(known_ws_uris)} known workspace(s))...")
            for idx, cid, title in unmapped:
                inferred = infer_workspace_from_brain(cid, known_ws_uris)
                if inferred and (_is_remote_uri(inferred) or os.path.isdir(inferred)):
                    ws_assignments[cid] = inferred
                    shown = inferred if _is_remote_uri(inferred) else os.path.basename(inferred)
                    _print(f"    [{idx:3d}] -> {shown}")
            _print(f"  Auto-assigned {len(ws_assignments)} workspace(s)")
            _print()
        if mode == "manual" and interactive:
            rest = [(i, c, t) for i, c, t in unmapped if c not in ws_assignments]
            if rest:
                ws_assignments.update(interactive_workspace_assignment(rest))

    _print("  Building index...")
    result_bytes = b""
    ts_updated = 0
    for cid, title, source, inner_data, has_ws in resolved:
        conversation_path = conv_catalog.get(cid)
        conversation_mtime = (
            os.path.getmtime(conversation_path)
            if conversation_path and os.path.exists(conversation_path) else None
        )
        source_is_db = bool(conversation_path and conversation_path.lower().endswith(".db"))
        old_timestamp = extract_timestamp_seconds(inner_data, 3) if inner_data else None
        entry = build_trajectory_entry(
            cid, title, inner_data, ws_assignments.get(cid), conversation_mtime, source_is_db
        )
        result_bytes += encode_length_delimited(1, entry)
        if conversation_mtime and (
            not inner_data
            or (source_is_db and (old_timestamp is None or int(conversation_mtime) > old_timestamp))
            or (not source_is_db and not has_timestamp_fields(inner_data))
        ):
            ts_updated += 1

    if keep_missing and missing:
        missing.sort(key=lambda c: extract_timestamp_seconds(existing_inner_blobs[c], 3) or 0,
                     reverse=True)
        for cid in missing:
            title = existing_titles.get(cid)
            if not title:
                raw_title = next(iter_length_delimited_fields(existing_inner_blobs[cid], 1), b"")
                title = raw_title.decode("utf-8", "replace") or f"Conversation {cid[:8]}"
            result_bytes += encode_length_delimited(
                1, build_trajectory_entry(cid, title, existing_inner_blobs[cid]))

    ws_total = sum(1 for item in resolved if item[4] or item[0] in ws_assignments)
    _print(f"  Workspaces: {ws_total} linked  |  Timestamps added/refreshed: {ts_updated}")
    _print()

    encoded = base64.b64encode(result_bytes).decode("utf-8")
    _print("  Writing index (every database is backed up first):")
    results = write_index_to_all(DB_PATHS, encoded)
    for db_path, backup_path in results:
        _print(f"    {_db_app_name(db_path)}: updated")
        if backup_path:
            _print(f"      backup: {backup_path}")

    kept = f" + {len(missing)} kept" if keep_missing and missing else ""
    _print()
    _print(f"  SUCCESS: index rebuilt with {len(conversation_ids)} conversation(s){kept}.")
    _print("  Open Antigravity; conversations should appear sorted by date.")
    _print("  To undo, choose 'Undo an index repair' and pick a backup file above.")
    return 0
