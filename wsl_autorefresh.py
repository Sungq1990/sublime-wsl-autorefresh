"""Sublime Text 4 plugin for WSL projects opened through \\wsl.localhost.

Polls the active window's project folders every five seconds because the WSL
9P/UNC bridge does not reliably deliver Windows file-change notifications.
"""

import fnmatch
import os
import time

import sublime
import sublime_plugin


INTERVAL_MS = 5000
ACTIVATION_DELAY_MS = 300
MAX_ENTRIES_PER_PROJECT = 100000
MAX_ERROR_BACKOFF_SECONDS = 60
TOO_LARGE_RETRY_SECONDS = 300
STATUS_KEY = "wsl_autorefresh"

# Large generated directories are skipped to keep the five-second scan cheap.
DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".cache",
    ".gradle",
    ".mypy_cache",
    ".next",
    ".nox",
    ".nuxt",
    ".output",
    ".pytest_cache",
    ".ruff_cache",
    ".terraform",
    ".tox",
    ".venv",
    "__pycache__",
    "bower_components",
    "build",
    "coverage",
    "dist",
    "env",
    "htmlcov",
    "node_modules",
    "target",
    "vendor",
    "venv",
}

_WINDOW_STATE = {}
_VIEW_STATE = {}
_running = False
_unloaded = False
_activation_sequence = 0


class _TooManyEntries(Exception):
    pass


def _is_wsl_path(path):
    if not path:
        return False
    normalized = path.replace("/", "\\").lower()
    return normalized.startswith("\\\\wsl.localhost\\") or normalized.startswith("\\\\wsl$\\")


def _matches_patterns(name, relative_path, patterns):
    relative = relative_path.replace("\\", "/")
    for raw_pattern in patterns or ():
        pattern = str(raw_pattern).replace("\\", "/")
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern):
            return True
    return False


def _path_kind(path):
    try:
        if os.path.islink(path):
            return "link"
        if os.path.isdir(path):
            return "dir"
        if os.path.isfile(path):
            return "file"
    except OSError:
        pass
    return "other"


def _scan_project(root, window):
    """Return a set of relative entries, excluding generated directories."""
    settings = window.settings()
    folder_excludes = settings.get("folder_exclude_patterns", []) or []
    file_excludes = settings.get("file_exclude_patterns", []) or []

    snapshot = set()
    stack = [(root, "")]

    while stack:
        current, prefix = stack.pop()
        try:
            names = os.listdir(current)
        except OSError:
            # A subdirectory may disappear during an atomic save/rename. The
            # project root itself must remain accessible or the scan fails.
            if current == root:
                raise
            continue

        for name in names:
            full_path = os.path.join(current, name)
            relative = prefix + name if not prefix else prefix + "\\" + name
            kind = _path_kind(full_path)

            if kind == "dir":
                if name.lower() in DEFAULT_EXCLUDED_DIRS:
                    continue
                if _matches_patterns(name, relative, folder_excludes):
                    continue
            elif kind == "file" and _matches_patterns(name, relative, file_excludes):
                continue

            snapshot.add(kind + ":" + relative)
            if len(snapshot) > MAX_ENTRIES_PER_PROJECT:
                raise _TooManyEntries(root)

            if kind == "dir":
                stack.append((full_path, relative))

    return snapshot


def _fingerprint(path):
    stat = os.stat(path)
    mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000))
    return (mtime_ns, stat.st_size)


def _window_is_valid(window):
    try:
        return any(item.id() == window.id() for item in sublime.windows())
    except Exception:
        return False


def _wsl_project_roots(window):
    return tuple(folder for folder in window.folders() if _is_wsl_path(folder))


def _get_window_state(window, roots):
    window_id = window.id()
    state = _WINDOW_STATE.get(window_id)
    if state is None or state["roots"] != roots:
        state = {"roots": roots, "projects": {}}
        _WINDOW_STATE[window_id] = state
    return state


def _scan_projects(window, state, now):
    changed = False

    for root in state["roots"]:
        project = state["projects"].get(root)
        if project is None:
            project = {
                "snapshot": None,
                "next_scan": 0.0,
                "errors": 0,
                "error_logged": False,
                "disabled": False,
                "large_logged": False,
            }
            state["projects"][root] = project

        if now < project["next_scan"]:
            continue

        try:
            snapshot = _scan_project(root, window)
        except _TooManyEntries:
            project["disabled"] = True
            project["next_scan"] = now + TOO_LARGE_RETRY_SECONDS
            if not project["large_logged"]:
                print(
                    "[WslAutorefresh] Skipped {}: more than {} entries".format(
                        root, MAX_ENTRIES_PER_PROJECT
                    )
                )
                project["large_logged"] = True
            continue
        except OSError as exc:
            project["errors"] += 1
            delay = min(
                MAX_ERROR_BACKOFF_SECONDS,
                (INTERVAL_MS // 1000) * (2 ** min(project["errors"] - 1, 4)),
            )
            project["next_scan"] = now + delay
            if not project["error_logged"]:
                print("[WslAutorefresh] Cannot scan {}: {}".format(root, exc))
                project["error_logged"] = True
            continue

        project["disabled"] = False
        project["next_scan"] = 0.0
        project["large_logged"] = False
        if project["error_logged"]:
            print("[WslAutorefresh] Scan recovered: {}".format(root))
            project["error_logged"] = False
        project["errors"] = 0

        previous = project["snapshot"]
        project["snapshot"] = snapshot
        if previous is not None and previous != snapshot:
            changed = True

    return changed


def _refresh_folder_list(window):
    def apply():
        if not _window_is_valid(window):
            return
        try:
            window.run_command("refresh_folder_list")
        except Exception as exc:
            print("[WslAutorefresh] refresh_folder_list failed: {}".format(exc))

    sublime.set_timeout(apply, 0)


def _clear_view_status(view):
    def apply():
        if view.is_valid():
            view.erase_status(STATUS_KEY)

    sublime.set_timeout(apply, 0)


def _reset_view_state(view):
    path = view.file_name()
    view_id = view.id()

    if not path or not _is_wsl_path(path):
        _VIEW_STATE.pop(view_id, None)
        _clear_view_status(view)
        return

    try:
        current = _fingerprint(path)
    except OSError:
        _VIEW_STATE.pop(view_id, None)
        return

    _VIEW_STATE[view_id] = {"path": path, "disk": current, "pending": False}
    _clear_view_status(view)


def _check_open_files(window):
    ui_actions = []
    content_changed = False

    for view in window.views():
        path = view.file_name()
        view_id = view.id()

        if not path or not _is_wsl_path(path):
            _VIEW_STATE.pop(view_id, None)
            continue
        if view.is_loading():
            continue

        try:
            current = _fingerprint(path)
        except OSError:
            # Keep the previous state: atomic saves can briefly remove a file.
            continue

        state = _VIEW_STATE.get(view_id)
        if state is None or state["path"] != path:
            _VIEW_STATE[view_id] = {"path": path, "disk": current, "pending": False}
            continue

        if current != state["disk"]:
            state["disk"] = current
            state["pending"] = True
            content_changed = True

        if not state["pending"]:
            continue

        if view.is_dirty():
            ui_actions.append(
                (
                    "status",
                    view,
                    "WSL 文件已在外部修改；本地有未保存改动，未自动刷新",
                )
            )
            continue

        state["pending"] = False
        ui_actions.append(("revert", view, None))

    if ui_actions:
        def apply():
            for action, view, message in ui_actions:
                if not view.is_valid():
                    continue
                if action == "status":
                    view.set_status(STATUS_KEY, message)
                else:
                    view.erase_status(STATUS_KEY)
                    view.run_command("revert")

        sublime.set_timeout(apply, 0)

    return content_changed


def _cleanup_states():
    valid_windows = {window.id() for window in sublime.windows()}
    for window_id in list(_WINDOW_STATE):
        if window_id not in valid_windows:
            _WINDOW_STATE.pop(window_id, None)

    valid_views = {
        view.id() for window in sublime.windows() for view in window.views()
    }
    for view_id in list(_VIEW_STATE):
        if view_id not in valid_views:
            _VIEW_STATE.pop(view_id, None)


def _run(window=None, force=False):
    global _running

    if _unloaded or _running:
        return

    _running = True
    try:
        if window is None:
            window = sublime.active_window()
        if window is None or not _window_is_valid(window):
            return

        _cleanup_states()
        roots = _wsl_project_roots(window)
        state = _get_window_state(window, roots)
        projects_changed = _scan_projects(window, state, time.monotonic())
        content_changed = _check_open_files(window)

        if projects_changed or content_changed or force:
            _refresh_folder_list(window)
    except Exception as exc:
        print("[WslAutorefresh] Poll failed: {}".format(exc))
    finally:
        _running = False


def _periodic_poll():
    if _unloaded:
        return
    _run()
    if not _unloaded:
        sublime.set_timeout_async(_periodic_poll, INTERVAL_MS)


def _schedule_activation_poll():
    global _activation_sequence

    _activation_sequence += 1
    sequence = _activation_sequence

    def apply():
        if not _unloaded and sequence == _activation_sequence:
            _run()

    sublime.set_timeout_async(apply, ACTIVATION_DELAY_MS)


class WslAutorefreshListener(sublime_plugin.EventListener):
    def on_load_async(self, view):
        _reset_view_state(view)

    def on_post_save_async(self, view):
        _reset_view_state(view)

    def on_close(self, view):
        _VIEW_STATE.pop(view.id(), None)

    def on_activated_async(self, view):
        _schedule_activation_poll()


class WslAutorefreshRefreshNowCommand(sublime_plugin.WindowCommand):
    def run(self):
        _run(self.window, force=True)


def plugin_unloaded():
    global _unloaded
    _unloaded = True
    _WINDOW_STATE.clear()
    _VIEW_STATE.clear()


sublime.set_timeout_async(_periodic_poll, INTERVAL_MS)
sublime.set_timeout_async(_run, 1000)
