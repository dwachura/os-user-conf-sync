# os-user-conf-sync

Small Python CLI for syncing selected files from `$HOME` through a git repository.

Current scope:

- Ubuntu/Linux-oriented
- files and directories under `$HOME`
- no symlink tracking; symlinks are detected and skipped with warnings
- explicit `push` or `pull`
- `pull` stops on local changes unless `--force`
- stores file contents plus metadata: mode, atime, mtime

## Requirements

- Python 3.9+
- `git`
- access to a remote git repository you can clone and push to

## Install

Recommended: install with `pipx`.

### Install `pipx`

On Ubuntu:

```bash
sudo apt update
sudo apt install pipx
pipx ensurepath
```

Restart your shell after `pipx ensurepath` if needed.

### Install the tool

From this project directory:

```bash
pipx install .
```

This installs both `os-user-conf-sync` and the shorter alias `userconf`.

Then verify:

```bash
userconf --help
```

If you already installed an older version, refresh the entry points with:

```bash
pipx install --force .
```

## Quick Start

Initialize the tool with the git repository that will hold your tracked files:

```bash
userconf init <repo-url>
```

Add files under `$HOME`:

```bash
userconf add ~/.bashrc
userconf add ~/.gitconfig
userconf add -r ~/.config/nvim
userconf add -r -i ~/.config
```

See what is currently managed:

```bash
userconf dirs
userconf list
userconf status
userconf status --json
userconf status --json --offline
```

Push current local versions to the remote repository:

```bash
userconf sync push
```

On another machine, initialize against the same repository and pull tracked files down:

```bash
userconf init <repo-url>
userconf sync pull
```

Stop tracking a file:

```bash
userconf remove ~/.gitconfig
userconf sync push
```

## Command Reference

### `init <repo-url>`

- stores the repository URL locally
- clones the repository into the local cache directory
- prepares the tool for later `add`, `remove`, and `sync` operations

### `add <path>`

- validates the file exists
- requires the file to be under `$HOME`
- queues it to become tracked on the next `sync push`

### `add -r <path>`

- tracks a directory recursively as one root
- shows a summary first: file count, directory count, total file size, symlink count, unsupported entry count
- asks for confirmation before queuing it
- warns if the directory is large
- future files created under that directory are included on later `sync push`

### `add -r -i <path>`

- walks the directory interactively
- for each directory you can:
  - track it as a whole
  - enter it and decide on children one by one
  - skip it completely
- for each file you can decide whether to track it
- interactive selections create explicit file roots plus any subtree roots you chose to track whole

### `remove <path>`

- queues a tracked file for removal from the managed set
- leaves the local file on disk
- takes effect on the next `sync push`

### `list`

- shows tracked roots and pending local add/remove operations
- also shows basic state like `tracked-file`, `tracked-dir`, `pending-add-file`, `pending-add-dir`, `modified`, `missing`

### `dirs`

- shows important local paths used by the tool
- includes home, config root/file, data root, state file, repo cache, and managed repo files directory

### `status`

- fetches remote state and compares it with the current local machine
- shows pending local add/remove operations separately from remote drift
- reports actionable differences like `modified file`, `missing file`, `missing dir`, `path collision`
- reports pull blockers using the same checks as `sync pull`
- reports paths that were previously synced but are no longer managed remotely
- supports `--json` for machine-readable output with a versioned schema
- supports `--offline` to use cached remote-tracking refs without running `git fetch`

### `sync push`

- uploads current local contents of all tracked files to the managed git repository
- records file and directory metadata: mode, atime, mtime
- aborts if the remote repository changed since the last successful sync
- aborts if a tracked local file is missing or unsupported
- if tracked directories contain symlinks or unsupported entries, they are skipped and reported clearly

### `sync pull`

- downloads tracked files from the managed git repository into local `$HOME`
- recreates tracked directories too
- aborts if there are pending add/remove operations
- aborts if tracked local files were modified since last sync
- if something previously managed is no longer in remote state, it stays on disk locally and is reported as no longer managed

### `sync pull --force`

- same as `sync pull`
- overwrites modified local tracked files

## Typical Workflow

Machine A:

```bash
os-user-conf-sync init git@github.com:you/dotfiles-sync.git
os-user-conf-sync add ~/.bashrc
os-user-conf-sync add ~/.config/nvim/init.lua
os-user-conf-sync add -r ~/.ssh
os-user-conf-sync sync push
```

Machine B:

```bash
os-user-conf-sync init git@github.com:you/dotfiles-sync.git
os-user-conf-sync sync pull
```

After editing a tracked file on Machine A:

```bash
os-user-conf-sync status
os-user-conf-sync sync push
```

Then on Machine B:

```bash
os-user-conf-sync status
os-user-conf-sync status --offline
os-user-conf-sync status --json --offline
os-user-conf-sync sync pull
```

## What Gets Stored

The managed repository stores:

- `manifest.json` with tracked paths and metadata
- `files/HOME/...` with file contents

Tracked paths are stored as `$HOME/...` so they map cleanly across machines.

The tool preserves:

- file contents
- directory metadata
- file mode/permissions
- access time (`atime`)
- modification time (`mtime`)

## Local State

The tool keeps local data under XDG data home.

Default location:

```text
~/.local/share/os-user-conf-sync/
```

Key files:

- `state.json` - local tool state
- `repo/` - local clone of the managed repository

If `XDG_DATA_HOME` is set, that location is used instead.

## Limitations

- files under `$HOME` only
- symlinks not supported
- no conflict merge logic
- `push` and `pull` are explicit; nothing syncs automatically
- interactive mode requires a TTY

## Config

Default config path:

```text
~/.config/os-user-conf-sync/config.json
```

Current options:

- `large_directory_warning_bytes` - default `52428800` (50 MiB)

Example:

```json
{
  "large_directory_warning_bytes": 104857600
}
```

## Troubleshooting

### `remote changed; run \`sync pull\` first`

Another machine pushed new state. Run:

```bash
os-user-conf-sync status
os-user-conf-sync sync pull
```

If you have local changes in tracked files, inspect them first or use:

```bash
os-user-conf-sync sync pull --force
```

### `pending add/remove changes exist`

You queued `add` or `remove` operations locally. Finish them with:

```bash
os-user-conf-sync sync push
```

### `only files under $HOME are supported`

Move the file into your home directory or keep it out of scope for this tool.

### large directory warning

The warning threshold is controlled by `large_directory_warning_bytes` in the config file.

### symlink warnings

Symlinks are never tracked in v1. During recursive add and push, the tool reports them clearly and skips them.

## Development

Run directly from the source tree:

```bash
python3 -m os_user_conf_sync --help
python3 -m unittest discover -s tests
```

Reinstall after local changes:

```bash
pipx install --force .
```

Uninstall:

```bash
pipx uninstall os-user-conf-sync
```
