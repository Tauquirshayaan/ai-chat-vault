"""Create, verify and restore AI Chat Vault backup archives."""

import hashlib
import json
import os
import platform
import shutil
import socket
import sqlite3
import tempfile
import time
import zipfile
from urllib.parse import quote

from . import __version__
from .common import default_env, expand, human_size, long_path, running_apps
from .sources import ROOTS, TOOL_KEYS, iter_root_files, optional_group_of, selected_roots

FORMAT = "ai-chat-vault/1"
MANIFEST = "manifest.json"
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".vscdb", ".vscdb.backup")
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
STORED_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".mov", ".mkv",
    ".zip", ".gz", ".7z", ".rar", ".vsix", ".woff", ".woff2", ".pb", ".br", ".zst",
)
CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    pass


class Cancelled(Exception):
    """Raised from a progress callback to stop a backup, check or export."""


def _tick(progress, done, total, label):
    if progress is not None:
        progress(done, total, label)


def _log(message=""):
    print(message, flush=True)


# ─── SQLite snapshots ────────────────────────────────────────────────────────

def is_sqlite_file(path):
    if not path.lower().endswith(SQLITE_SUFFIXES):
        return False
    try:
        with open(long_path(path), "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def snapshot_sqlite(source, target):
    """
    Copy a live SQLite database, including committed data still in its WAL
    file, into ``target`` using SQLite's online backup API. Returns True on
    success. The source is opened read-only first.
    """
    absolute = os.path.abspath(source).replace("\\", "/")
    uris = ["file:" + quote(absolute, safe="/:") + "?mode=ro", None]
    for uri in uris:
        for _attempt in range(3):
            src = dst = None
            try:
                if uri:
                    src = sqlite3.connect(uri, uri=True, timeout=5)
                else:
                    src = sqlite3.connect(source, timeout=5)
                dst = sqlite3.connect(target)
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                dst.close()
                dst = None
                return True
            except sqlite3.Error:
                time.sleep(0.5)
            finally:
                for conn in (dst, src):
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            if os.path.exists(target):
                os.remove(target)
    return False


# ─── Planning ────────────────────────────────────────────────────────────────

def plan_backup(env=None, tools=None, include_extensions=True, include_logins=False):
    """
    Walk every source and return a plan:
      {"roots": [{root, template, path, files: [(rel, abs, size, mtime)], bytes}],
       "skipped": [...], "bytes": int, "files": int}
    """
    env = env or default_env()
    skipped = []
    roots = []
    for root, template, path in selected_roots(env, tools, include_extensions):
        files = []
        for rel, full in iter_root_files(root, path, include_logins, skipped):
            try:
                st = os.stat(long_path(full))
            except OSError as error:
                skipped.append({"path": full, "reason": str(error)})
                continue
            files.append((rel, full, st.st_size, st.st_mtime, optional_group_of(root.id, rel)))
        roots.append({"root": root, "template": template, "path": path, "files": files})
    plan = {"roots": roots, "skipped": skipped, "env": env, "include_logins": include_logins,
            "tools": list(tools) if tools else None}
    _recount(plan)
    return plan


def _recount(plan):
    plan["files"] = plan["bytes"] = 0
    for item in plan["roots"]:
        item["bytes"] = sum(f[2] for f in item["files"])
        plan["files"] += len(item["files"])
        plan["bytes"] += item["bytes"]


def optional_sizes(plan):
    """Bytes per optional part: {'extensions': n, 'recordings': n}."""
    sizes = {"extensions": 0}
    for item in plan["roots"]:
        if item["root"].category == "extensions":
            sizes["extensions"] += item["bytes"]
        for f in item["files"]:
            if f[4]:
                sizes[f[4]] = sizes.get(f[4], 0) + f[2]
    return sizes


def filter_plan(plan, tools=None, include_extensions=True, include_recordings=False,
                include_logins=False):
    """Return a copy of a full scan narrowed to the user's choices (no rescan)."""
    from .sources import LOGIN_FILES, is_excluded
    wanted = {TOOL_KEYS[t] for t in tools} if tools else None
    roots = []
    for item in plan["roots"]:
        root = item["root"]
        if wanted is not None and root.tool not in wanted:
            continue
        if root.category == "extensions" and not include_extensions:
            continue
        files = item["files"]
        if not include_recordings:
            files = [f for f in files if f[4] != "recordings"]
        if not include_logins and LOGIN_FILES.get(root.id):
            files = [f for f in files if not is_excluded(f[0], LOGIN_FILES[root.id])]
        roots.append(dict(item, files=files))
    narrowed = dict(plan, roots=roots, tools=list(tools) if tools else None,
                    include_logins=include_logins and plan["include_logins"])
    _recount(narrowed)
    return narrowed


def drop_optional(plan, name):
    """Remove an optional part ('extensions' or a group name) from a plan."""
    if name == "extensions":
        plan["roots"] = [i for i in plan["roots"] if i["root"].category != "extensions"]
    else:
        for item in plan["roots"]:
            item["files"] = [f for f in item["files"] if f[4] != name]
    _recount(plan)


def print_plan(plan):
    _log("  What will be backed up:")
    for item in plan["roots"]:
        root = item["root"]
        _log(f"    [{root.tool}] {root.label}")
        _log(f"        {item['path']}  -  {len(item['files'])} files, {human_size(item['bytes'])}")
    wanted = {TOOL_KEYS[t] for t in plan["tools"]} if plan.get("tools") else None
    missing = [r for r in ROOTS if r.id not in {i["root"].id for i in plan["roots"]}
               and (wanted is None or r.tool in wanted) and r.category != "extensions"]
    for root in missing:
        _log(f"    [{root.tool}] {root.label}: not found on this PC (skipped)")
    _log(f"  Total: {plan['files']} files, {human_size(plan['bytes'])} before compression")
    if not plan["include_logins"]:
        _log("  Login tokens are NOT included - sign in again after restoring.")


# ─── Writing ─────────────────────────────────────────────────────────────────

def _zip_info(arcname, mtime, compress):
    stamp = time.localtime(max(mtime, 315532800 + 86400))  # zip needs >= 1980
    info = zipfile.ZipInfo(arcname, date_time=stamp[:6])
    info.compress_type = compress
    info.external_attr = 0o644 << 16
    return info


def _add_file(zf, arcname, source, mtime):
    """Stream one file into the archive; return (size, sha256)."""
    compress = zipfile.ZIP_STORED if source.lower().endswith(STORED_SUFFIXES) else zipfile.ZIP_DEFLATED
    digest = hashlib.sha256()
    size = 0
    with open(long_path(source), "rb") as src, \
            zf.open(_zip_info(arcname, mtime, compress), "w", force_zip64=True) as dst:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            dst.write(chunk)
    return size, digest.hexdigest()


def write_backup(plan, dest_dir, readable=True, verify=True, name=None, progress=None):
    """
    Write the planned backup into a zip in dest_dir; return (path, manifest).
    ``progress(done, total, label)`` is called as files are saved and checked;
    it may raise Cancelled to stop. An unfinished zip is always removed.
    """
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    host = "".join(c for c in socket.gethostname() if c.isalnum() or c in "-_") or "PC"
    name = name or f"AI-Chat-Vault_{host}_{stamp}.zip"
    partial = os.path.join(dest_dir, name) + ".partial"
    try:
        return _write_backup(plan, dest_dir, readable, verify, name, progress)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise


def _write_backup(plan, dest_dir, readable, verify, name, progress):
    os.makedirs(dest_dir, exist_ok=True)
    try:
        free = shutil.disk_usage(dest_dir).free
    except OSError:
        free = None
    if free is not None and free < plan["bytes"] * 1.05 + 50 * 1024 * 1024:
        raise BackupError(
            f"Not enough free space in {dest_dir}: need up to {human_size(plan['bytes'])}, "
            f"have {human_size(free)}.")

    final_path = os.path.join(dest_dir, name)
    partial = final_path + ".partial"
    total_files = plan["files"]
    overall = 0

    manifest = {
        "format": FORMAT,
        "tool_version": __version__,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "env": plan["env"],
        "include_logins": plan["include_logins"],
        "roots": [],
        "files": [],
        "skipped": list(plan["skipped"]),
        "readable_export": None,
    }

    with tempfile.TemporaryDirectory(prefix="chatvault-") as work:
        with zipfile.ZipFile(partial, "w", allowZip64=True, compresslevel=6) as zf:
            for item in plan["roots"]:
                root = item["root"]
                manifest["roots"].append({
                    "id": root.id, "tool": root.tool, "label": root.label,
                    "template": item["template"], "source": item["path"], "kind": root.kind,
                })
                _log(f"  Backing up {root.label} ...")
                snapshotted = set()
                files = item["files"]
                rels = {f[0] for f in files}
                done = 0
                for rel, full, size, mtime, _group in files:
                    _tick(progress, overall, total_files, f"Backing up {root.tool}")
                    overall += 1
                    base = None
                    for suffix in SQLITE_SIDECARS:
                        if rel.endswith(suffix):
                            base = rel[:-len(suffix)]
                    if base is not None and base in snapshotted:
                        continue  # data already inside the consistent snapshot
                    arcname = f"files/{root.id}/{rel}"
                    entry = {"root": root.id, "rel": rel, "mtime": mtime}
                    try:
                        if is_sqlite_file(full):
                            snap = os.path.join(work, "snapshot.sqlite")
                            if snapshot_sqlite(full, snap):
                                entry["size"], entry["sha256"] = _add_file(zf, arcname, snap, mtime)
                                entry["method"] = "sqlite-snapshot"
                                os.remove(snap)
                                snapshotted.add(rel)
                                # A sidecar walked earlier than its database is
                                # dropped from the manifest and archive below.
                            else:
                                entry["size"], entry["sha256"] = _add_file(zf, arcname, full, mtime)
                                entry["method"] = "raw-sqlite"
                            entry["sqlite"] = True
                        else:
                            entry["size"], entry["sha256"] = _add_file(zf, arcname, full, mtime)
                            entry["method"] = "copy"
                    except OSError as error:
                        manifest["skipped"].append({"path": full, "reason": str(error)})
                        continue
                    manifest["files"].append(entry)
                    done += 1
                    if done % 500 == 0:
                        _log(f"      {done}/{len(files)} files ...")
                # os.walk sorts names, so 'x.db' precedes 'x.db-wal'; this
                # guard only matters for unusual orderings.
                stale = {f"{root.id}/{b}{s}" for b in snapshotted for s in SQLITE_SIDECARS
                         if f"{b}{s}" in rels}
                if stale:
                    manifest["files"] = [e for e in manifest["files"]
                                         if f"{e['root']}/{e['rel']}" not in stale]
                _log(f"      {done} files saved")

            if readable:
                _log("  Creating readable Markdown copy of all chats ...")
                _tick(progress, total_files, total_files, "Creating readable copy of chats")
                from .export import export_all
                out = os.path.join(work, "readable")
                try:
                    stats = export_all(out, plan["env"], plan.get("tools"), quiet=True)
                    for current, _dirs, names in os.walk(out):
                        for fname in names:
                            full = os.path.join(current, fname)
                            rel = os.path.relpath(full, out).replace(os.sep, "/")
                            _add_file(zf, "readable/" + rel, full, os.path.getmtime(full))
                    manifest["readable_export"] = stats
                    _log(f"      {stats['chats']} chats exported to readable/")
                except Exception as error:  # never let the export block a backup
                    manifest["skipped"].append({"path": "readable export", "reason": str(error)})
                    _log(f"      Readable export failed ({error}); raw backup continues.")

            zf.writestr("README.txt", _archive_readme(manifest))
            zf.writestr(MANIFEST, json.dumps(manifest, indent=1))

    os.replace(partial, final_path)
    if verify:
        _log("  Verifying backup ...")
        ok, problems, _ = verify_backup(final_path, quiet=True, progress=progress)
        if not ok:
            raise BackupError("Backup verification failed:\n    " + "\n    ".join(problems[:20]))
        _log("      every file matches its checksum")
    return final_path, manifest


def _archive_readme(manifest):
    lines = [
        "AI Chat Vault backup",
        "====================",
        f"Created {manifest['created']} on {manifest['hostname']} with v{manifest['tool_version']}.",
        "",
        "files/     raw app data, restored with: AI_Chat_Vault.exe -> Restore a backup",
        "readable/  every chat as Markdown; open readable/INDEX.md in any editor",
        "manifest.json  checksums and original locations",
        "",
        "This archive contains private conversations and project details.",
        "Keep it somewhere safe and do not share it.",
    ]
    return "\n".join(lines) + "\n"


# ─── Verification ────────────────────────────────────────────────────────────

def load_manifest(zf):
    try:
        manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
    except KeyError:
        raise BackupError("This zip is not an AI Chat Vault backup (no manifest.json).")
    if manifest.get("format") != FORMAT:
        raise BackupError(f"Unsupported backup format: {manifest.get('format')!r}")
    return manifest


def verify_backup(zip_path, quiet=False, progress=None):
    """Re-read every file and compare it with its recorded SHA-256."""
    problems = []
    with zipfile.ZipFile(zip_path) as zf:
        manifest = load_manifest(zf)
        names = set(zf.namelist())
        for i, entry in enumerate(manifest["files"], 1):
            _tick(progress, i, len(manifest["files"]), "Checking backup")
            arcname = f"files/{entry['root']}/{entry['rel']}"
            if arcname not in names:
                problems.append(f"missing: {arcname}")
                continue
            digest = hashlib.sha256()
            try:
                with zf.open(arcname) as f:
                    for chunk in iter(lambda: f.read(CHUNK), b""):
                        digest.update(chunk)
            except (zipfile.BadZipFile, OSError, EOFError) as error:
                problems.append(f"unreadable: {arcname} ({error})")
                continue
            if digest.hexdigest() != entry["sha256"]:
                problems.append(f"checksum mismatch: {arcname}")
            if not quiet and i % 1000 == 0:
                _log(f"    checked {i}/{len(manifest['files'])} ...")
    return not problems, problems, manifest


def describe_backup(manifest):
    _log(f"  Backup created {manifest['created']} on {manifest['hostname']}")
    by_tool = {}
    for entry in manifest["files"]:
        by_tool.setdefault(entry["root"], [0, 0])
        by_tool[entry["root"]][0] += 1
        by_tool[entry["root"]][1] += entry["size"]
    for root in manifest["roots"]:
        count, size = by_tool.get(root["id"], (0, 0))
        _log(f"    [{root['tool']}] {root['label']}: {count} files, {human_size(size)}")
    stats = manifest.get("readable_export")
    if stats:
        _log(f"    Readable Markdown copy: {stats['chats']} chats")
    if manifest.get("skipped"):
        _log(f"    {len(manifest['skipped'])} item(s) could not be read when backing up")


# ─── Restore ─────────────────────────────────────────────────────────────────

def _file_sha256(path):
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_restore(manifest, env=None, tools=None):
    """Return [(entry, root_record, target_path)] for the selected tools."""
    env = env or default_env()
    wanted = {TOOL_KEYS[t] for t in tools} if tools else None
    roots = {r["id"]: r for r in manifest["roots"]}
    plan = []
    for entry in manifest["files"]:
        root = roots.get(entry["root"])
        if not root or (wanted and root["tool"] not in wanted):
            continue
        base = expand(root["template"], env)
        if root["kind"] == "file":
            target = base
        else:
            target = os.path.join(base, *entry["rel"].split("/"))
        plan.append((entry, root, target))
    return plan


def restore_backup(zip_path, env=None, tools=None, dry_run=False, check_running=True,
                   safety_dir=None, skip_verify=False, progress=None):
    """
    Restore files from a backup. Existing files that would be replaced (and
    stale SQLite -wal/-shm files beside restored databases) are first saved
    into a "before-restore" backup that this tool can restore again.
    Returns a summary dict.
    """
    env = env or default_env()
    if check_running and not dry_run:
        busy = running_apps()
        if busy:
            raise BackupError(
                "Close these apps completely before restoring: " + ", ".join(busy)
                + "\n    (they would overwrite the restored data when they exit).")

    if not skip_verify:
        _log("  Verifying backup before restoring ...")
        ok, problems, manifest = verify_backup(zip_path, quiet=True, progress=progress)
        if not ok:
            raise BackupError("The backup is damaged; nothing was restored:\n    "
                              + "\n    ".join(problems[:20]))
        _log("      backup is intact")
    else:
        with zipfile.ZipFile(zip_path) as zf:
            manifest = load_manifest(zf)

    plan = plan_restore(manifest, env, tools)
    backed_up_paths = {os.path.normcase(t) for _, _, t in plan}
    summary = {"new": 0, "replaced": 0, "unchanged": 0, "sidecars_removed": 0,
               "safety_backup": None, "warnings": []}

    old_home = manifest.get("env", {}).get("HOME")
    if old_home and os.path.normcase(old_home) != os.path.normcase(env["HOME"]):
        summary["warnings"].append(
            f"The backup came from {old_home} but your home folder is now {env['HOME']}. "
            "Chats are restored to the new location, but paths written inside old chats "
            "still mention the old user folder.")

    # Work out what changes before touching anything.
    to_write, to_save, sidecars = [], [], []
    for entry, root, target in plan:
        if os.path.exists(long_path(target)):
            try:
                same = (os.path.getsize(long_path(target)) == entry["size"]
                        and _file_sha256(target) == entry["sha256"])
            except OSError:
                same = False
            if same:
                summary["unchanged"] += 1
                continue
            to_save.append((root, entry, target))
            summary["replaced"] += 1
        else:
            summary["new"] += 1
        to_write.append((entry, root, target))
        if entry.get("sqlite"):
            sidecars.extend(_stale_sidecars(target, backed_up_paths))
    summary["sidecars_removed"] = len(sidecars)

    if dry_run:
        return summary

    if to_save or sidecars:
        safety_dir = safety_dir or os.path.dirname(os.path.abspath(zip_path))
        summary["safety_backup"] = _write_safety_backup(
            safety_dir, manifest, env, to_save, sidecars)

    with zipfile.ZipFile(zip_path) as zf:
        for i, (entry, root, target) in enumerate(to_write, 1):
            _tick(progress, i, len(to_write), "Restoring files")
            arcname = f"files/{entry['root']}/{entry['rel']}"
            os.makedirs(long_path(os.path.dirname(target)), exist_ok=True)
            temp = target + ".chatvault-tmp"
            with zf.open(arcname) as src, open(long_path(temp), "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            os.replace(long_path(temp), long_path(target))
            try:
                os.utime(long_path(target), (entry["mtime"], entry["mtime"]))
            except OSError:
                pass
            if i % 1000 == 0:
                _log(f"      {i}/{len(to_write)} files ...")
    for path in sidecars:
        try:
            os.remove(long_path(path))
        except OSError:
            pass
    return summary


def _stale_sidecars(target, backed_up_paths):
    found = []
    for suffix in SQLITE_SIDECARS:
        path = target + suffix
        if os.path.normcase(path) not in backed_up_paths and os.path.exists(long_path(path)):
            found.append(path)
    return found


def _write_safety_backup(folder, manifest, env, to_save, sidecars):
    """Save files about to be replaced in a normal, restorable vault zip."""
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    path = os.path.join(folder, f"AI-Chat-Vault_before-restore_{stamp}.zip")
    counter = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"AI-Chat-Vault_before-restore_{stamp}_{counter}.zip")
        counter += 1
    roots = {r["id"]: r for r in manifest["roots"]}
    safety = {"format": FORMAT, "tool_version": __version__,
              "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "hostname": socket.gethostname(), "platform": platform.platform(),
              "env": env, "include_logins": True, "roots": [], "files": [],
              "skipped": [], "readable_export": None}
    used_roots = set()
    items = [(roots[entry["root"]], entry["rel"], target) for _root, entry, target in to_save]
    for side in sidecars:
        for entry_root in manifest["roots"]:
            base = expand(entry_root["template"], env)
            if entry_root["kind"] == "dir" and os.path.normcase(side).startswith(
                    os.path.normcase(base) + os.sep):
                rel = os.path.relpath(side, base).replace(os.sep, "/")
                items.append((entry_root, rel, side))
                break
    with zipfile.ZipFile(path, "w", allowZip64=True) as zf:
        for root, rel, target in items:
            mtime = os.path.getmtime(long_path(target))
            size, sha = _add_file(zf, f"files/{root['id']}/{rel}", target, mtime)
            safety["files"].append({"root": root["id"], "rel": rel, "mtime": mtime,
                                    "size": size, "sha256": sha, "method": "copy"})
            used_roots.add(root["id"])
        safety["roots"] = [r for r in manifest["roots"] if r["id"] in used_roots]
        zf.writestr(MANIFEST, json.dumps(safety, indent=1))
    return path
