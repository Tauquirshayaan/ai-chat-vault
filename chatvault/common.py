"""Platform helpers shared by every command."""

import os
import platform
import shutil
import subprocess
import sys

SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"

# Process names per app. Windows names are compared case-insensitively.
APP_PROCESSES = {
    "Antigravity": {
        "windows": ("antigravity.exe", "antigravity ide.exe"),
        "posix": ("antigravity", "Antigravity", "antigravity-ide",
                  "antigravity ide", "Antigravity IDE"),
    },
    "Codex": {
        "windows": ("codex.exe",),
        "posix": ("codex",),
    },
    "Claude Code": {
        "windows": ("claude.exe",),
        "posix": ("claude",),
    },
}


def default_env():
    """Template variables for the current machine."""
    home = os.path.expanduser("~")
    if IS_WINDOWS:
        appcfg = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif SYSTEM == "Darwin":
        appcfg = os.path.join(home, "Library", "Application Support")
    else:
        appcfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return {"HOME": home, "APPCFG": appcfg}


def expand(template, env):
    """Turn '{HOME}/.codex' into a native path for this machine."""
    path = template
    for key, value in env.items():
        path = path.replace("{" + key + "}", value)
    return os.path.normpath(path.replace("/", os.sep))


def long_path(path):
    """Prefix Windows paths so files deeper than 260 characters still work."""
    if not IS_WINDOWS:
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _windows_process_names():
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=0x08000000,
        )
    except Exception:
        return set()
    names = set()
    for line in result.stdout.splitlines():
        if line.startswith('"'):
            names.add(line.split('","', 1)[0].strip('"').lower())
    return names


def running_apps(apps=None):
    """Return the names (from APP_PROCESSES) of apps that are running now."""
    apps = list(apps or APP_PROCESSES)
    found = []
    if IS_WINDOWS:
        names = _windows_process_names()
        for app in apps:
            if any(n in names for n in APP_PROCESSES[app]["windows"]):
                found.append(app)
        return found
    for app in apps:
        for name in APP_PROCESSES[app]["posix"]:
            try:
                result = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
            except Exception:
                continue
            if result.returncode == 0 and result.stdout.strip():
                found.append(app)
                break
    return found


def system_drive():
    if IS_WINDOWS:
        return (os.environ.get("SystemDrive") or "C:").upper()
    return None


def drive_of(path):
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return drive.upper() if drive else None


def suggest_backup_folder():
    """
    Suggest a destination that survives formatting Windows: the non-system
    fixed or removable drive with the most free space.
    """
    if not IS_WINDOWS:
        return os.path.join(os.path.expanduser("~"), "AI-Chat-Backups")
    try:
        import ctypes
        get_type = ctypes.windll.kernel32.GetDriveTypeW
    except Exception:
        get_type = None
    best, best_free = None, -1
    sysdrive = system_drive()
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = letter + ":\\"
        if (letter + ":") == sysdrive or not os.path.exists(root):
            continue
        if get_type is not None and get_type(root) not in (2, 3):  # removable, fixed
            continue
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        if free > best_free:
            best, best_free = root, free
    if best is None:
        return os.path.join(os.path.expanduser("~"), "AI-Chat-Backups")
    return os.path.join(best, "AI-Chat-Backups")


def human_size(num):
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def ask(prompt, default=""):
    """input() that returns the default when there is no console input."""
    try:
        answer = input(prompt)
    except (EOFError, OSError):
        return default
    answer = answer.strip()
    return answer if answer else default


def ask_yes(prompt, default=True):
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = ask(prompt + suffix, "y" if default else "n").lower()
    return answer in ("y", "yes")


def is_frozen():
    return bool(getattr(sys, "frozen", False))
