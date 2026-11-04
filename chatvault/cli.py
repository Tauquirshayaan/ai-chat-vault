"""Command line and double-click menu for AI Chat Vault."""

import argparse
import glob
import os
import sys
import time

from . import __version__
from .common import (IS_WINDOWS, ask, ask_yes, default_env, drive_of, human_size, is_frozen,
                     running_apps, suggest_backup_folder, system_drive)

TOOL_CHOICES = ("antigravity", "codex", "claude")


def _p(message=""):
    print(message, flush=True)


def _banner():
    _p()
    _p("=" * 62)
    _p(f"   AI Chat Vault  v{__version__}")
    _p("   Back up and restore Antigravity, Codex and Claude Code chats")
    _p("=" * 62)


def _parse_tools(value):
    if not value:
        return None
    tools = [t.strip().lower() for t in value.split(",") if t.strip()]
    bad = [t for t in tools if t not in TOOL_CHOICES]
    if bad:
        raise SystemExit(f"Unknown tool(s): {', '.join(bad)}. Use: {', '.join(TOOL_CHOICES)}")
    return tools


def _clean_path(raw):
    return raw.strip().strip('"').strip("'")


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_backup(dest=None, tools=None, include_logins=False, include_extensions=True,
               readable=True, assume_yes=False, interactive=False, include_recordings=False):
    from .archive import (BackupError, drop_optional, optional_sizes, plan_backup,
                          print_plan, write_backup)
    from .common import human_size

    busy = running_apps()
    if busy:
        _p()
        _p("  WARNING: still running: " + ", ".join(busy))
        _p("  Close them for the most complete backup (databases are still copied")
        _p("  safely, but a chat being written right now may be cut short).")
        if interactive and not ask_yes("  Continue anyway?", default=False):
            return 1

    if interactive:
        include_logins = ask_yes(
            "  Include login tokens? Only if the backup never leaves your own drives.", False)

    _p()
    _p("  Scanning (this can take a minute) ...")
    plan = plan_backup(default_env(), tools, True, include_logins)
    if not plan["files"]:
        _p("  Nothing to back up: no Antigravity, Codex or Claude Code data found.")
        return 1

    sizes = optional_sizes(plan)
    if interactive:
        _p()
        if sizes.get("extensions"):
            include_extensions = ask_yes(
                f"  Include editor extensions ({human_size(sizes['extensions'])}; "
                "they can also be reinstalled)?", True)
        if sizes.get("recordings"):
            include_recordings = ask_yes(
                f"  Include Antigravity browser-agent screenshots "
                f"({human_size(sizes['recordings'])} of page images, not chats)?", False)
    if not include_extensions:
        drop_optional(plan, "extensions")
    if not include_recordings:
        drop_optional(plan, "recordings")
    _p()
    print_plan(plan)
    _p()

    if not dest:
        suggestion = suggest_backup_folder()
        dest = _clean_path(ask(f"  Save backup in [{suggestion}]: ", suggestion)) if interactive \
            else suggestion
    dest = os.path.abspath(dest)
    sysdrive = system_drive()
    if sysdrive and drive_of(dest) == sysdrive:
        _p(f"  WARNING: {dest} is on {sysdrive}, the Windows drive.")
        _p("  If you format that drive the backup is erased with it.")
        if interactive and not ask_yes("  Use it anyway?", default=False):
            return 1
    if interactive and not assume_yes and not ask_yes(f"  Create the backup in {dest}?", True):
        return 1

    try:
        path, manifest = write_backup(plan, dest, readable=readable)
    except BackupError as error:
        _p(f"  ERROR: {error}")
        return 1
    size = os.path.getsize(path)
    _p()
    _p("  " + "=" * 58)
    _p("  BACKUP COMPLETE")
    _p(f"  {path}")
    _p(f"  {len(manifest['files'])} files, {human_size(size)} compressed")
    _p("  " + "=" * 58)
    if manifest["skipped"]:
        _p(f"  {len(manifest['skipped'])} item(s) could not be read (usually locked files):")
        for item in manifest["skipped"][:10]:
            _p(f"    - {item['path']}: {item['reason']}")
    _p()
    _p("  Before formatting:")
    _p("    1. Copy this zip to a second place too (USB drive or cloud storage).")
    _p("    2. Open readable/INDEX.md inside the zip to check your chats are there.")
    _p("  After reinstalling: install the apps, sign in, open each once, close")
    _p("  them, then run AI Chat Vault -> 'Restore a backup'.")
    return 0


def cmd_verify(zip_path):
    from .archive import BackupError, describe_backup, verify_backup
    _p(f"  Checking {zip_path} ...")
    try:
        ok, problems, manifest = verify_backup(zip_path)
    except (BackupError, OSError, ValueError) as error:
        _p(f"  ERROR: {error}")
        return 1
    describe_backup(manifest)
    if ok:
        _p(f"  OK: all {len(manifest['files'])} files match their checksums.")
        return 0
    _p(f"  DAMAGED: {len(problems)} problem(s):")
    for problem in problems[:30]:
        _p(f"    - {problem}")
    return 1


def cmd_restore(zip_path, tools=None, dry_run=False, assume_yes=False, interactive=False):
    import zipfile
    from .archive import BackupError, describe_backup, load_manifest, restore_backup

    try:
        with zipfile.ZipFile(zip_path) as zf:
            manifest = load_manifest(zf)
    except (BackupError, OSError, zipfile.BadZipFile) as error:
        _p(f"  ERROR: {error}")
        return 1
    describe_backup(manifest)
    _p()

    if interactive:
        _p("  Restore which tools? Enter = all, or e.g. 'antigravity,claude'")
        tools = _parse_tools(ask("  Tools: ", "")) or None

    try:
        preview = restore_backup(zip_path, tools=tools, dry_run=True, skip_verify=True)
    except BackupError as error:
        _p(f"  ERROR: {error}")
        return 1
    _p(f"  Plan: {preview['new']} new files, {preview['replaced']} replaced, "
       f"{preview['unchanged']} already identical.")
    if preview["replaced"] or preview["sidecars_removed"]:
        _p("  Files that get replaced are saved first in an 'AI-Chat-Vault_before-restore'")
        _p("  zip next to the backup, so this can be undone.")
    for warning in preview["warnings"]:
        _p(f"  NOTE: {warning}")
    if dry_run:
        _p("  Dry run: nothing was changed.")
        return 0
    if interactive and not assume_yes and not ask_yes("  Restore now?", True):
        return 1

    try:
        summary = restore_backup(zip_path, tools=tools)
    except BackupError as error:
        _p(f"  ERROR: {error}")
        return 1
    _p()
    _p("  " + "=" * 58)
    _p(f"  RESTORE COMPLETE: {summary['new']} new, {summary['replaced']} replaced, "
       f"{summary['unchanged']} unchanged")
    if summary["safety_backup"]:
        _p(f"  Previous files saved in: {summary['safety_backup']}")
    _p("  " + "=" * 58)
    _p()
    _p("  Next:")
    _p("    1. Sign in again to Codex and Claude Code if asked.")
    _p("    2. Open Antigravity: your chats should be in the sidebar.")
    _p("       If some are missing, run 'Repair Antigravity sidebar index'.")
    _p("    3. Codex: 'codex resume'.  Claude Code: 'claude --resume' in the project folder.")
    return 0


def cmd_export(dest=None, tools=None, interactive=False):
    from .export import export_all
    if not dest:
        default = os.path.join(suggest_backup_folder(),
                               "AI-Chat-Export_" + time.strftime("%Y-%m-%d_%H%M%S"))
        dest = _clean_path(ask(f"  Export folder [{default}]: ", default)) if interactive else default
    _p(f"  Exporting chats to {dest} ...")
    stats = export_all(dest, default_env(), tools)
    _p(f"  Done: {stats['chats']} chats. Open {os.path.join(dest, 'INDEX.md')}")
    return 0


def cmd_repair(auto=False, drop_missing=False, interactive=True):
    from . import antigravity_index
    return antigravity_index.run_repair(
        interactive=interactive and not auto,
        workspace_mode="auto" if auto else None,
        keep_missing=not drop_missing,
    )


def cmd_undo_repair(backup_file=None, interactive=True):
    from . import antigravity_index as ag
    if ag.is_antigravity_running():
        _p("  ERROR: Close Antigravity first. Nothing was changed.")
        return 1
    if not ag.DB_PATHS:
        _p("  ERROR: no Antigravity database found.")
        return 1
    if not backup_file:
        files = sorted(glob.glob(os.path.join(ag._backup_dir(), "trajectorySummaries_backup*.txt")),
                       key=os.path.getmtime, reverse=True)
        if not files:
            _p(f"  No index backups found in {ag._backup_dir()}")
            return 1
        _p("  Index backups (newest first):")
        for i, path in enumerate(files[:15], 1):
            _p(f"    {i:2d}) {os.path.basename(path)}")
        if not interactive:
            backup_file = files[0]
        else:
            choice = ask("  Number to restore (Enter = cancel): ", "")
            if not choice.isdigit() or not 1 <= int(choice) <= len(files[:15]):
                return 1
            backup_file = files[int(choice) - 1]
    name = os.path.basename(backup_file).lower()
    targets = [db for db in ag.DB_PATHS
               if ag.re.sub(r"[^a-z0-9]+", "_", ag._db_app_name(db).lower()).strip("_") in name]
    targets = targets or ag.DB_PATHS[:1]
    for db in targets:
        safety = ag.restore_index_backup(db, backup_file)
        _p(f"  Restored {os.path.basename(backup_file)} into {db}")
        if safety:
            _p(f"  (the index it replaced was saved as {safety})")
    return 0


def cmd_list(tools=None, include_extensions=True):
    from .archive import plan_backup, print_plan
    plan = plan_backup(default_env(), tools, include_extensions, False)
    print_plan(plan)
    return 0


# ─── Menu ────────────────────────────────────────────────────────────────────

MENU = (
    ("1", "Back up everything (do this before formatting)"),
    ("2", "Restore a backup (after reinstalling)"),
    ("3", "Check a backup file"),
    ("4", "Export all chats as readable Markdown"),
    ("5", "Repair Antigravity sidebar index"),
    ("6", "Undo an Antigravity index repair"),
    ("7", "Show what would be backed up"),
    ("0", "Exit"),
)


def _backup_folders():
    """Every place the tool would normally have saved a backup."""
    folders = [suggest_backup_folder()]
    if IS_WINDOWS:
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            folders.append(letter + ":\\AI-Chat-Backups")
    unique = []
    for folder in folders:
        if os.path.isdir(folder) and os.path.normcase(folder) not in map(os.path.normcase, unique):
            unique.append(folder)
    return unique


def find_backups():
    """Backup zips (not before-restore copies) on all drives, newest first."""
    zips = []
    for folder in _backup_folders():
        zips += [z for z in glob.glob(os.path.join(folder, "AI-Chat-Vault_*.zip"))
                 if "_before-restore_" not in os.path.basename(z)]
    return sorted(zips, key=os.path.getmtime, reverse=True)


def _ask_zip():
    zips = find_backups()
    if zips:
        _p("  Backups found (newest first):")
        for i, path in enumerate(zips[:9], 1):
            size = human_size(os.path.getsize(path))
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
            _p(f"    {i}) {path}  ({size}, {when})")
        _p()
        answer = _clean_path(ask(
            "  Press Enter for 1, type a number, or drag another zip here: ", "1"))
        if answer.isdigit() and 1 <= int(answer) <= len(zips[:9]):
            return zips[int(answer) - 1]
        path = answer
    else:
        _p("  No backup found yet.")
        _p(f"  (Looked in {suggest_backup_folder()} and \\AI-Chat-Backups on every drive.)")
        _p("  Create one first with option 1, or drag a backup zip from")
        _p("  another place into this window.")
        _p()
        path = _clean_path(ask("  Backup zip (Enter = back to menu): ", ""))
        if not path:
            return None
    if not os.path.isfile(path):
        _p(f"  File not found: {path}")
        return None
    return path


def menu():
    while True:
        _banner()
        for key, label in MENU:
            _p(f"   {key}) {label}")
        _p()
        choice = ask("  Choose: ", "0")
        _p()
        if choice == "1":
            cmd_backup(interactive=True)
        elif choice == "2":
            path = _ask_zip()
            if path:
                cmd_restore(path, interactive=True)
        elif choice == "3":
            path = _ask_zip()
            if path:
                cmd_verify(path)
        elif choice == "4":
            cmd_export(interactive=True)
        elif choice == "5":
            cmd_repair(interactive=True)
        elif choice == "6":
            cmd_undo_repair(interactive=True)
        elif choice == "7":
            cmd_list()
        else:
            return 0
        _p()
        ask("  Press Enter to return to the menu...", "")


# ─── Entry point ─────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog="AI_Chat_Vault",
        description="Back up, restore, export and repair Antigravity, Codex and Claude Code chats.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("backup", help="create a verified backup zip")
    p.add_argument("--dest", help="folder for the zip (default: largest non-Windows drive)")
    p.add_argument("--only", help="comma list: antigravity,codex,claude")
    p.add_argument("--include-logins", action="store_true", help="also back up login tokens")
    p.add_argument("--no-extensions", action="store_true", help="skip editor extensions")
    p.add_argument("--include-screenshots", action="store_true",
                   help="also back up Antigravity browser-agent screenshots (browser_recordings)")
    p.add_argument("--no-readable", action="store_true", help="skip the Markdown copy")

    p = sub.add_parser("verify", help="check a backup zip against its checksums")
    p.add_argument("zip")

    p = sub.add_parser("restore", help="restore a backup zip")
    p.add_argument("zip")
    p.add_argument("--only", help="comma list: antigravity,codex,claude")
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    p = sub.add_parser("export", help="write every chat as Markdown")
    p.add_argument("--dest")
    p.add_argument("--only", help="comma list: antigravity,codex,claude")

    p = sub.add_parser("repair-index", help="rebuild the Antigravity sidebar index")
    p.add_argument("--auto", action="store_true", help="no questions; auto-assign workspaces")
    p.add_argument("--drop-missing", action="store_true",
                   help="remove index entries whose conversation file is gone")

    p = sub.add_parser("undo-repair", help="put an index backup back")
    p.add_argument("backup_file", nargs="?")

    p = sub.add_parser("list", help="show what a backup would include")
    p.add_argument("--only")
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return menu()
    args = build_parser().parse_args(argv)
    _banner()
    if args.command == "backup":
        return cmd_backup(args.dest, _parse_tools(args.only), args.include_logins,
                          not args.no_extensions, not args.no_readable, assume_yes=True,
                          include_recordings=args.include_screenshots)
    if args.command == "verify":
        return cmd_verify(args.zip)
    if args.command == "restore":
        return cmd_restore(args.zip, _parse_tools(args.only), args.dry_run, args.yes,
                           interactive=not (args.yes or args.dry_run))
    if args.command == "export":
        return cmd_export(args.dest, _parse_tools(args.only))
    if args.command == "repair-index":
        return cmd_repair(args.auto, args.drop_missing, interactive=not args.auto)
    if args.command == "undo-repair":
        return cmd_undo_repair(args.backup_file, interactive=args.backup_file is None)
    if args.command == "list":
        return cmd_list(_parse_tools(args.only))
    build_parser().print_help()
    return 1


def run():
    """Entry point that keeps a double-clicked window open on errors."""
    for stream in (sys.stdout, sys.stderr):
        try:  # chat titles may contain characters the console cannot show
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    try:
        return main()
    except KeyboardInterrupt:
        _p("\n  Cancelled.")
        return 1
    except Exception as error:
        _p()
        _p(f"  UNEXPECTED ERROR: {error}")
        _p("  Nothing after this point was changed. Please report this message.")
        if is_frozen():
            ask("\n  Press Enter to close...", "")
        return 1
