# sublime-wsl-autorefresh

Sublime Text 4 plugin for projects stored in WSL and opened from Windows through `\\wsl.localhost` or `\\wsl$`.

WSL's UNC/9P bridge does not reliably deliver Windows file-change notifications. This plugin polls the active window's project folders every five seconds to keep the editor and sidebar in sync.

## Features

- Recursively watches the current project folders, not only open files.
- Refreshes the sidebar when files or folders are added, removed, or renamed.
- Reloads open files when their content changes on disk.
- Never overwrites a buffer with unsaved changes; shows a status-bar warning instead.
- Triggers `Project: Refresh Folders` after content changes so Sublime's built-in Git badges update in the same polling cycle.
- Skips large generated directories such as `.git`, `node_modules`, `vendor`, `venv`, `dist`, and `build`.
- Provides a manual `Wsl Autorefresh: Refresh Now` command.

## Install

Copy `wsl_autorefresh.py` into one of these directories:

- Standard install: `%APPDATA%\Sublime Text\Packages\User\`
- Portable install: `<Sublime Text>\Data\Packages\WslAutoRefresh\`

Restart Sublime Text if it does not reload the plugin automatically.

## Configuration

Edit the constants at the top of `wsl_autorefresh.py`:

- `INTERVAL_MS`: polling interval, default `5000`.
- `DEFAULT_EXCLUDED_DIRS`: generated directories skipped during project scans.
- `MAX_ENTRIES_PER_PROJECT`: safety limit for very large projects.

The plugin uses APIs compatible with Sublime Text 4's Python 3.3 plugin host.

## Development sync

The maintained source lives in WSL at `/www/my/sublime-wsl-autorefresh`. After editing it, sync the portable Sublime copy with:

```cmd
copy /Y "\\wsl.localhost\Ubuntu\www\my\sublime-wsl-autorefresh\wsl_autorefresh.py" "D:\Sublime Text\Data\Packages\WslAutoRefresh\wsl_autorefresh.py"
```
