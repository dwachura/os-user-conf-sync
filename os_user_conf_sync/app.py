from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

APP_DIR_NAME = "os-user-conf-sync"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 2
DEFAULT_LARGE_DIRECTORY_WARNING_BYTES = 50 * 1024 * 1024
MAX_SCAN_EXAMPLES = 5


class AppError(RuntimeError):
    pass


def data_root() -> Path:
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME


def config_root() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / APP_DIR_NAME
    return Path.home() / ".config" / APP_DIR_NAME


def state_path() -> Path:
    return data_root() / "state.json"


def repo_dir() -> Path:
    return data_root() / "repo"


def config_path() -> Path:
    return config_root() / "config.json"


def ensure_data_root() -> None:
    data_root().mkdir(parents=True, exist_ok=True)


def ensure_config_root() -> None:
    config_root().mkdir(parents=True, exist_ok=True)


def default_config() -> dict[str, Any]:
    return {
        "large_directory_warning_bytes": DEFAULT_LARGE_DIRECTORY_WARNING_BYTES,
    }


def load_config() -> dict[str, Any]:
    config = default_config()
    path = config_path()
    if not path.exists():
        return config

    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise AppError(f"invalid config format: {path}")

    if "large_directory_warning_bytes" in data:
        config["large_directory_warning_bytes"] = int(data["large_directory_warning_bytes"])
    return config


def ensure_config_file() -> None:
    path = config_path()
    if path.exists():
        return
    ensure_config_root()
    path.write_text(json.dumps(default_config(), indent=2, sort_keys=True) + "\n")


def root_entry(kind: str, token: str) -> dict[str, str]:
    if kind not in {"file", "dir"}:
        raise AppError(f"unsupported root type: {kind}")
    token_to_relative_path(token)
    return {"kind": kind, "path": token}


def root_key(entry: dict[str, str]) -> tuple[str, str]:
    return entry["kind"], entry["path"]


def default_state(repo_url: str = "") -> dict[str, Any]:
    return {
        "repo_url": repo_url,
        "pending_adds": [],
        "pending_removes": [],
        "last_sync_commit": None,
        "last_synced_hashes": {},
        "last_synced_directories": [],
    }


def normalize_root_entries(values: list[Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, str):
            entries.append(root_entry("file", value))
            continue
        if not isinstance(value, dict):
            raise AppError("invalid root entry format")
        entries.append(root_entry(str(value["kind"]), str(value["path"])))
    return dedupe_and_normalize_roots(entries)


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        raise AppError("not initialized; run `init <repo-url>` first")

    data = json.loads(path.read_text())
    state = default_state(str(data.get("repo_url", "")))
    state["pending_adds"] = normalize_root_entries(list(data.get("pending_adds", [])))
    state["pending_removes"] = normalize_root_entries(list(data.get("pending_removes", [])))
    state["last_sync_commit"] = data.get("last_sync_commit")
    state["last_synced_hashes"] = {
        str(token): str(value) for token, value in dict(data.get("last_synced_hashes", {})).items()
    }
    last_synced_directories: list[str] = []
    for token in list(data.get("last_synced_directories", [])):
        token_text = str(token)
        token_to_relative_path(token_text)
        last_synced_directories.append(token_text)
    state["last_synced_directories"] = sorted(last_synced_directories)
    return state


def save_state(state: dict[str, Any]) -> None:
    ensure_data_root()
    payload = {
        "repo_url": state["repo_url"],
        "pending_adds": sorted(state["pending_adds"], key=root_sort_key),
        "pending_removes": sorted(state["pending_removes"], key=root_sort_key),
        "last_sync_commit": state.get("last_sync_commit"),
        "last_synced_hashes": dict(sorted(state.get("last_synced_hashes", {}).items())),
        "last_synced_directories": sorted(set(state.get("last_synced_directories", []))),
    }
    tmp_path = state_path().with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(state_path())


def normalize_user_path(raw_path: str, *, require_exists: bool) -> tuple[Path, str]:
    input_path = Path(raw_path).expanduser()
    if not input_path.is_absolute():
        input_path = Path.cwd() / input_path

    if input_path.exists() and input_path.is_symlink():
        raise AppError("symlinks are not supported")

    try:
        resolved = input_path.resolve(strict=require_exists)
    except FileNotFoundError as exc:
        raise AppError(f"path does not exist: {input_path}") from exc

    home = Path.home().resolve()
    try:
        relative = resolved.relative_to(home)
    except ValueError as exc:
        raise AppError("only paths under $HOME are supported") from exc

    token = f"$HOME/{relative.as_posix()}"
    return resolved, token


def token_to_relative_path(token: str) -> Path:
    if not token.startswith("$HOME/"):
        raise AppError(f"invalid tracked path: {token}")
    relative = Path(token.removeprefix("$HOME/"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise AppError(f"invalid tracked path: {token}")
    return relative


def token_to_local_path(token: str) -> Path:
    return Path.home() / token_to_relative_path(token)


def token_to_repo_path(token: str) -> Path:
    return repo_dir() / "files" / "HOME" / token_to_relative_path(token)


def root_sort_key(entry: dict[str, str]) -> tuple[str, str]:
    return entry["path"], entry["kind"]


def token_is_within(child_token: str, parent_token: str) -> bool:
    child = token_to_relative_path(child_token)
    parent = token_to_relative_path(parent_token)
    return child != parent and parent in child.parents


def entry_covered_by_dir(entry: dict[str, str], dir_entry: dict[str, str]) -> bool:
    return dir_entry["kind"] == "dir" and (
        entry["path"] == dir_entry["path"] or token_is_within(entry["path"], dir_entry["path"])
    )


def dedupe_and_normalize_roots(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for entry in sorted(entries, key=lambda item: (item["path"].count("/"), item["path"], item["kind"])):
        exact_index = next((index for index, current in enumerate(normalized) if root_key(current) == root_key(entry)), None)
        if exact_index is not None:
            continue
        covered = any(entry_covered_by_dir(entry, current) for current in normalized if current["kind"] == "dir")
        if covered:
            continue
        if entry["kind"] == "dir":
            normalized = [current for current in normalized if not entry_covered_by_dir(current, entry)]
        normalized.append(entry)
    return sorted(normalized, key=root_sort_key)


def remove_exact_root(entries: list[dict[str, str]], kind: str, token: str) -> list[dict[str, str]]:
    return [entry for entry in entries if root_key(entry) != (kind, token)]


def has_exact_root(entries: list[dict[str, str]], kind: str, token: str) -> bool:
    return any(root_key(entry) == (kind, token) for entry in entries)


def file_covered_by_dir_root(entries: list[dict[str, str]], token: str) -> bool:
    candidate = root_entry("file", token)
    return any(entry_covered_by_dir(candidate, entry) for entry in entries if entry["kind"] == "dir")


def root_label(entry: dict[str, str], prefix: str) -> str:
    return f"{prefix}-{entry['kind']}"


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_metadata(path: Path) -> dict[str, Any]:
    file_stat = path.stat()
    return {
        "mode": stat.S_IMODE(file_stat.st_mode),
        "atime_ns": file_stat.st_atime_ns,
        "mtime_ns": file_stat.st_mtime_ns,
    }


def collect_file_metadata(path: Path) -> dict[str, Any]:
    metadata = collect_metadata(path)
    metadata["sha256"] = hash_file(path)
    return metadata


def apply_metadata(path: Path, metadata: dict[str, Any]) -> None:
    os.chmod(path, int(metadata["mode"]))
    os.utime(path, ns=(int(metadata["atime_ns"]), int(metadata["mtime_ns"])))


def empty_manifest() -> dict[str, Any]:
    return {"version": MANIFEST_VERSION, "roots": [], "directories": {}, "files": {}}


def normalize_metadata(metadata: dict[str, Any], *, with_hash: bool) -> dict[str, Any]:
    required = {"mode", "atime_ns", "mtime_ns"}
    if with_hash:
        required.add("sha256")
    missing = required - set(metadata)
    if missing:
        raise AppError(f"manifest metadata missing: {', '.join(sorted(missing))}")
    normalized: dict[str, Any] = {}
    normalized["mode"] = int(metadata["mode"])
    normalized["atime_ns"] = int(metadata["atime_ns"])
    normalized["mtime_ns"] = int(metadata["mtime_ns"])
    if with_hash:
        normalized["sha256"] = str(metadata["sha256"])
    return normalized


def load_manifest() -> dict[str, Any]:
    manifest_path = repo_dir() / MANIFEST_NAME
    if not manifest_path.exists():
        return empty_manifest()

    data = json.loads(manifest_path.read_text())
    return normalize_manifest_data(data)


def normalize_manifest_data(data: dict[str, Any]) -> dict[str, Any]:
    version = int(data.get("version", 1))
    if version == 1:
        files = {
            token: normalize_metadata(metadata, with_hash=True)
            for token, metadata in dict(data.get("files", {})).items()
        }
        roots = [root_entry("file", token) for token in files]
        return {
            "version": MANIFEST_VERSION,
            "roots": dedupe_and_normalize_roots(roots),
            "directories": {},
            "files": dict(sorted(files.items())),
        }
    if version != MANIFEST_VERSION:
        raise AppError(f"unsupported manifest version: {version}")

    roots = normalize_root_entries(list(data.get("roots", [])))
    directories = {
        str(token): normalize_metadata(metadata, with_hash=False)
        for token, metadata in dict(data.get("directories", {})).items()
    }
    files = {
        str(token): normalize_metadata(metadata, with_hash=True)
        for token, metadata in dict(data.get("files", {})).items()
    }
    return {
        "version": MANIFEST_VERSION,
        "roots": roots,
        "directories": dict(sorted(directories.items())),
        "files": dict(sorted(files.items())),
    }


def load_manifest_at_ref(ref: str | None) -> dict[str, Any]:
    if not ref:
        return empty_manifest()
    object_ref = f"{ref}:{MANIFEST_NAME}"
    if git_rev_parse(object_ref) is None:
        return empty_manifest()
    result = git(["show", object_ref])
    return normalize_manifest_data(json.loads(result.stdout))


def write_manifest(manifest: dict[str, Any]) -> None:
    payload = {
        "version": MANIFEST_VERSION,
        "roots": sorted(manifest["roots"], key=root_sort_key),
        "directories": dict(sorted(manifest["directories"].items())),
        "files": dict(sorted(manifest["files"].items())),
    }
    (repo_dir() / MANIFEST_NAME).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo_dir()), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or "git command failed"
        raise AppError(message)
    return result


def clone_repo(repo_url: str) -> None:
    ensure_data_root()
    if repo_dir().exists():
        if (repo_dir() / ".git").exists():
            return
        raise AppError(f"repo cache already exists and is not a git checkout: {repo_dir()}")
    result = subprocess.run(["git", "clone", repo_url, str(repo_dir())], capture_output=True, text=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or "git clone failed"
        raise AppError(message)


def current_branch() -> str:
    result = git(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise AppError("unable to determine managed branch")
    return branch


def git_rev_parse(ref: str) -> str | None:
    result = git(["rev-parse", "--verify", ref], check=False)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def fetch_remote() -> str | None:
    git(["fetch", "origin"])
    return git_rev_parse(f"refs/remotes/origin/{current_branch()}")


def remote_tracking_head(branch: str) -> str | None:
    return git_rev_parse(f"refs/remotes/origin/{branch}")


def sync_clone_with_remote() -> str | None:
    branch = current_branch()
    remote_head = fetch_remote()
    local_head = git_rev_parse("HEAD")
    if remote_head and local_head != remote_head:
        git(["pull", "--ff-only", "origin", branch])
    return remote_head


def worktree_dirty() -> bool:
    result = git(["status", "--porcelain"], check=False)
    return bool(result.stdout.strip())


def prune_empty_dirs(path: Path, stop_at: Path) -> None:
    current = path
    while current != stop_at and current.exists():
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def ensure_initialized() -> dict[str, Any]:
    state = load_state()
    clone_repo(state["repo_url"])
    ensure_config_file()
    return state


def format_bytes(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB"]
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            break
        value /= 1024.0
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def scan_summary_template(root_path: Path, root_token: str) -> dict[str, Any]:
    return {
        "root_path": root_path,
        "root_token": root_token,
        "file_count": 0,
        "dir_count": 1,
        "total_size_bytes": 0,
        "symlink_count": 0,
        "unsupported_count": 0,
        "symlink_examples": [],
        "unsupported_examples": [],
    }


def append_example(values: list[str], item: str) -> None:
    if len(values) < MAX_SCAN_EXAMPLES:
        values.append(item)


def scan_directory(root_path: Path, root_token: str) -> dict[str, Any]:
    summary = scan_summary_template(root_path, root_token)
    stack = [root_path]
    while stack:
        current_path = stack.pop()
        for entry in sorted(os.scandir(current_path), key=lambda item: item.name):
            entry_path = Path(entry.path)
            if entry.is_symlink():
                summary["symlink_count"] += 1
                append_example(summary["symlink_examples"], str(entry_path))
                continue
            if entry.is_dir(follow_symlinks=False):
                summary["dir_count"] += 1
                stack.append(entry_path)
                continue
            if entry.is_file(follow_symlinks=False):
                summary["file_count"] += 1
                summary["total_size_bytes"] += entry_path.stat().st_size
                continue
            summary["unsupported_count"] += 1
            append_example(summary["unsupported_examples"], str(entry_path))
    return summary


def print_scan_summary(summary: dict[str, Any], config: dict[str, Any]) -> None:
    print(f"directory: {summary['root_path']}")
    print(f"regular files: {summary['file_count']}")
    print(f"directories: {summary['dir_count']}")
    print(f"total file size: {format_bytes(summary['total_size_bytes'])}")
    if summary["total_size_bytes"] >= int(config["large_directory_warning_bytes"]):
        threshold = format_bytes(int(config["large_directory_warning_bytes"]))
        print(f"warning: directory size exceeds configured threshold ({threshold})")
    if summary["symlink_count"]:
        print(f"warning: encountered {summary['symlink_count']} symlink(s); they will be skipped")
        for example in summary["symlink_examples"]:
            print(f"  symlink: {example}")
        if summary["symlink_count"] > len(summary["symlink_examples"]):
            remaining = summary["symlink_count"] - len(summary["symlink_examples"])
            print(f"  ... and {remaining} more symlink(s)")
    if summary["unsupported_count"]:
        print(f"warning: encountered {summary['unsupported_count']} unsupported entrie(s); they will be skipped")
        for example in summary["unsupported_examples"]:
            print(f"  unsupported: {example}")
        if summary["unsupported_count"] > len(summary["unsupported_examples"]):
            remaining = summary["unsupported_count"] - len(summary["unsupported_examples"])
            print(f"  ... and {remaining} more unsupported entrie(s)")


def confirm_yes_no(prompt: str) -> bool:
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def prompt_dir_action(path: Path) -> str:
    while True:
        answer = input(f"Directory {path}: [a]ll, [e]nter, [s]kip? ").strip().lower()
        if answer in {"a", "e", "s"}:
            return answer
        print("Please answer with a, e, or s.")


def require_interactive_stdin() -> None:
    if not sys.stdin.isatty():
        raise AppError("interactive mode requires a TTY")


def interactive_collect_roots(path: Path, token: str, config: dict[str, Any]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []

    def walk_dir(current_path: Path, current_token: str) -> None:
        action = prompt_dir_action(current_path)
        if action == "s":
            return
        if action == "a":
            summary = scan_directory(current_path, current_token)
            print_scan_summary(summary, config)
            if confirm_yes_no(f"Track whole directory {current_path}?"):
                selected.append(root_entry("dir", current_token))
            return

        for child in sorted(current_path.iterdir(), key=lambda item: item.name):
            child_resolved = child.resolve(strict=False)
            if child.is_symlink():
                print(f"skipping symlink: {child_resolved}")
                continue
            child_resolved, child_token = normalize_user_path(str(child), require_exists=True)
            if child.is_dir():
                walk_dir(child_resolved, child_token)
                continue
            if child.is_file() and confirm_yes_no(f"Track file {child_resolved}?"):
                selected.append(root_entry("file", child_token))
                continue
            if not child.is_file():
                print(f"skipping unsupported entry: {child_resolved}")

    walk_dir(path, token)
    return dedupe_and_normalize_roots(selected)


def gather_effective_roots(state: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, str]]:
    roots = list(manifest["roots"])
    for removed in state["pending_removes"]:
        roots = [entry for entry in roots if root_key(entry) != root_key(removed)]
    roots.extend(state["pending_adds"])
    return dedupe_and_normalize_roots(roots)


def expand_roots(roots: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    directories: dict[str, Any] = {}
    files: dict[str, Any] = {}
    notices: list[str] = []

    for entry in roots:
        local_path = token_to_local_path(entry["path"])
        if not local_path.exists():
            raise AppError(f"tracked {entry['kind']} missing: {local_path}")
        if local_path.is_symlink():
            raise AppError(f"tracked {entry['kind']} is a symlink: {local_path}")

        if entry["kind"] == "file":
            if not local_path.is_file():
                raise AppError(f"tracked file is not a regular file: {local_path}")
            files[entry["path"]] = collect_file_metadata(local_path)
            continue

        if not local_path.is_dir():
            raise AppError(f"tracked directory is not a directory: {local_path}")
        directories[entry["path"]] = collect_metadata(local_path)
        stack = [local_path]
        while stack:
            current_path = stack.pop()
            current_token = normalize_user_path(str(current_path), require_exists=True)[1]
            if current_token not in directories:
                directories[current_token] = collect_metadata(current_path)
            for child in sorted(os.scandir(current_path), key=lambda item: item.name):
                child_path = Path(child.path)
                if child.is_symlink():
                    notices.append(f"skipping symlink during push: {child_path}")
                    continue
                if child.is_dir(follow_symlinks=False):
                    stack.append(child_path)
                    continue
                if child.is_file(follow_symlinks=False):
                    child_token = normalize_user_path(str(child_path), require_exists=True)[1]
                    files[child_token] = collect_file_metadata(child_path)
                    continue
                notices.append(f"skipping unsupported entry during push: {child_path}")
    return dict(sorted(directories.items())), dict(sorted(files.items())), notices


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def clear_repo_files_tree() -> None:
    files_root = repo_dir() / "files"
    if files_root.exists():
        shutil.rmtree(files_root)


def path_under_root(token: str, root_entry_value: dict[str, str]) -> bool:
    return root_entry_value["path"] == token or token_is_within(token, root_entry_value["path"])


def root_statuses(entry: dict[str, str], state: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    if has_exact_root(manifest["roots"], entry["kind"], entry["path"]):
        labels.append(root_label(entry, "tracked"))
    if has_exact_root(state["pending_adds"], entry["kind"], entry["path"]):
        labels.append(root_label(entry, "pending-add"))
    if has_exact_root(state["pending_removes"], entry["kind"], entry["path"]):
        labels.append(root_label(entry, "pending-remove"))

    local_path = token_to_local_path(entry["path"])
    if not local_path.exists():
        labels.append("missing")
        return labels
    if entry["kind"] == "file" and not local_path.is_file():
        labels.append("unsupported")
        return labels
    if entry["kind"] == "dir" and not local_path.is_dir():
        labels.append("unsupported")
        return labels

    file_tokens = [token for token in manifest["files"] if path_under_root(token, entry)]
    if entry["kind"] == "file" and entry["path"] not in state["last_synced_hashes"]:
        labels.append("never-synced")
    for token in file_tokens:
        synced_hash = state["last_synced_hashes"].get(token)
        file_path = token_to_local_path(token)
        if not file_path.exists():
            labels.append("missing")
            break
        if file_path.is_symlink() or not file_path.is_file():
            labels.append("unsupported")
            break
        if synced_hash and hash_file(file_path) != synced_hash:
            labels.append("modified")
            break
    return sorted(set(labels))


def blockers_for_pull(manifest: dict[str, Any], state: dict[str, Any]) -> list[str]:
    return [format_status_item(item) for item in pull_blocker_items(manifest, state)]


def status_item(status: str, token: str, path: Path, *, kind: str, action: str | None = None) -> dict[str, Any]:
    item = {
        "kind": kind,
        "status": status,
        "token": token,
        "path": str(path),
    }
    if action is not None:
        item["action"] = action
    return item


def format_status_item(item: dict[str, Any]) -> str:
    action = item.get("action")
    status = str(item["status"]).replace("_", " ")
    if action:
        return f"pending {action} {item['kind']}: {item['path']}"
    if status == "no longer managed":
        return f"no longer managed remotely: {item['path']}"
    if status in {"modified", "missing", "unsupported"}:
        return f"{status} {item['kind']}: {item['path']}"
    return f"{status}: {item['path']}"


def pull_blocker_items(manifest: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    last_synced_directories = set(state["last_synced_directories"])
    last_synced_hashes = state["last_synced_hashes"]
    items: list[dict[str, Any]] = []

    for token in sorted(manifest["directories"]):
        local_path = token_to_local_path(token)
        if token in last_synced_directories:
            if local_path.exists() and not local_path.is_dir():
                items.append(status_item("path_collision", token, local_path, kind="dir"))
        elif local_path.exists():
            items.append(status_item("new_remote_directory_collides_locally", token, local_path, kind="dir"))

    for token in sorted(manifest["files"]):
        local_path = token_to_local_path(token)
        synced_hash = last_synced_hashes.get(token)
        if synced_hash is None:
            if local_path.exists():
                items.append(status_item("new_remote_file_collides_locally", token, local_path, kind="file"))
            continue
        if not local_path.exists():
            items.append(status_item("missing", token, local_path, kind="file"))
            continue
        if local_path.is_symlink() or not local_path.is_file():
            items.append(status_item("unsupported", token, local_path, kind="file"))
            continue
        if hash_file(local_path) != synced_hash:
            items.append(status_item("modified", token, local_path, kind="file"))
    return items


def remote_diff_items(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []

    for token in sorted(manifest["directories"]):
        local_path = token_to_local_path(token)
        if not local_path.exists():
            differences.append(status_item("missing", token, local_path, kind="dir"))
            continue
        if not local_path.is_dir():
            differences.append(status_item("path_collision", token, local_path, kind="dir"))

    for token in sorted(manifest["files"]):
        local_path = token_to_local_path(token)
        if not local_path.exists():
            differences.append(status_item("missing", token, local_path, kind="file"))
            continue
        if local_path.is_symlink() or not local_path.is_file():
            differences.append(status_item("unsupported", token, local_path, kind="file"))
            continue
        if hash_file(local_path) != manifest["files"][token]["sha256"]:
            differences.append(status_item("modified", token, local_path, kind="file"))

    return differences


def stale_local_path_items(manifest: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    current_files = set(manifest["files"])
    current_directories = set(manifest["directories"])
    for token in sorted(set(state["last_synced_hashes"]) - current_files):
        local_path = token_to_local_path(token)
        if local_path.exists():
            paths.append(status_item("no_longer_managed", token, local_path, kind="file"))
    for token in sorted(set(state["last_synced_directories"]) - current_directories):
        local_path = token_to_local_path(token)
        if local_path.exists():
            paths.append(status_item("no_longer_managed", token, local_path, kind="dir"))
    return paths


def print_status_section(title: str, items: list[dict[str, Any]] | list[str]) -> None:
    if not items:
        return
    print(f"{title}:")
    for line in items:
        if isinstance(line, dict):
            line = format_status_item(line)
        print(f"  - {line}")


def collect_status(state: dict[str, Any], *, offline: bool = False) -> dict[str, Any]:
    branch = current_branch()
    remote_head = remote_tracking_head(branch) if offline else fetch_remote()
    remote_manifest = load_manifest_at_ref(f"refs/remotes/origin/{branch}" if remote_head else None)

    pending: list[dict[str, Any]] = []
    for entry in state["pending_adds"]:
        pending.append(status_item("pending", entry["path"], token_to_local_path(entry["path"]), kind=entry["kind"], action="add"))
    for entry in state["pending_removes"]:
        pending.append(status_item("pending", entry["path"], token_to_local_path(entry["path"]), kind=entry["kind"], action="remove"))

    remote_state = "changed"
    if remote_head != state["last_sync_commit"]:
        remote_state = "changed"
    elif remote_head:
        remote_state = "in_sync"
    else:
        remote_state = "empty"

    differences = remote_diff_items(remote_manifest)
    blockers = pull_blocker_items(remote_manifest, state)
    stale = stale_local_path_items(remote_manifest, state)
    clean = not pending and not differences and not blockers and not stale and remote_state == "in_sync"
    return {
        "version": 1,
        "clean": clean,
        "remote": {
            "head": remote_head,
            "last_sync_commit": state["last_sync_commit"],
            "state": remote_state,
            "branch": branch,
            "mode": "offline" if offline else "online",
        },
        "pending": pending,
        "differences": differences,
        "pull_blockers": blockers,
        "stale_local_paths": stale,
    }


def apply_pending_adds(state: dict[str, Any], manifest: dict[str, Any], new_roots: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    messages: list[str] = []
    effective_roots = gather_effective_roots(state, manifest)
    pending_adds = list(state["pending_adds"])
    pending_removes = list(state["pending_removes"])
    for entry in new_roots:
        pending_removes = remove_exact_root(pending_removes, entry["kind"], entry["path"])
        if entry["kind"] == "file" and file_covered_by_dir_root(effective_roots + pending_adds, entry["path"]):
            messages.append(f"already covered by tracked directory: {token_to_local_path(entry['path'])}")
            continue
        combined = dedupe_and_normalize_roots(effective_roots + pending_adds + [entry])
        if not has_exact_root(combined, entry["kind"], entry["path"]):
            messages.append(f"already covered by tracked directory: {token_to_local_path(entry['path'])}")
            continue
        pending_adds = dedupe_and_normalize_roots(pending_adds + [entry])
        effective_roots = dedupe_and_normalize_roots(effective_roots + [entry])
    state["pending_adds"] = pending_adds
    state["pending_removes"] = pending_removes
    return pending_adds, messages


def handle_init(args: argparse.Namespace) -> None:
    repo_url = args.repo_url
    ensure_config_file()
    if state_path().exists():
        state = load_state()
        if state["repo_url"] != repo_url:
            raise AppError(f"already initialized for {state['repo_url']}")
        clone_repo(repo_url)
        print(f"already initialized: {repo_url}")
        return
    clone_repo(repo_url)
    state = default_state(repo_url)
    state["last_sync_commit"] = git_rev_parse("HEAD")
    save_state(state)
    print(f"initialized: {repo_url}")


def handle_add(args: argparse.Namespace) -> None:
    state = ensure_initialized()
    config = load_config()
    path, token = normalize_user_path(args.path, require_exists=True)
    manifest = load_manifest()

    if args.interactive and not args.recursive:
        raise AppError("-i is only valid together with -r")

    new_roots: list[dict[str, str]] = []
    if path.is_file():
        if args.recursive:
            raise AppError("-r can only be used with directories")
        new_roots = [root_entry("file", token)]
    elif path.is_dir():
        if not args.recursive:
            raise AppError("directory tracking requires -r")
        if args.interactive:
            require_interactive_stdin()
            new_roots = interactive_collect_roots(path, token, config)
            if not new_roots:
                print("nothing selected")
                return
        else:
            summary = scan_directory(path, token)
            print_scan_summary(summary, config)
            print("tracking this directory means future files under it will sync too")
            if not confirm_yes_no(f"Track directory {path} recursively?"):
                print("cancelled")
                return
            new_roots = [root_entry("dir", token)]
    else:
        raise AppError("only regular files and directories are supported")

    _, messages = apply_pending_adds(state, manifest, new_roots)
    save_state(state)
    for message in messages:
        print(message)
    for entry in new_roots:
        if has_exact_root(state["pending_adds"], entry["kind"], entry["path"]):
            print(f"queued add {entry['kind']}: {token_to_local_path(entry['path'])}")


def handle_remove(args: argparse.Namespace) -> None:
    state = ensure_initialized()
    path, token = normalize_user_path(args.path, require_exists=False)
    manifest = load_manifest()

    pending_adds = list(state["pending_adds"])
    pending_removes = list(state["pending_removes"])
    tracked_roots = gather_effective_roots(default_state(), manifest)

    pending_matches = [entry for entry in pending_adds if entry["path"] == token]
    if pending_matches:
        pending_adds = [entry for entry in pending_adds if entry["path"] != token]
        state["pending_adds"] = pending_adds
        save_state(state)
        print(f"removed pending add: {path}")
        return

    tracked_matches = [entry for entry in tracked_roots if entry["path"] == token]
    if not tracked_matches:
        raise AppError(f"not tracked as a root: {path}")

    for entry in tracked_matches:
        if has_exact_root(pending_removes, entry["kind"], entry["path"]):
            print(f"already pending remove {entry['kind']}: {path}")
            continue
        pending_removes.append(entry)
        print(f"queued remove {entry['kind']}: {path}")

    state["pending_removes"] = dedupe_and_normalize_roots(pending_removes)
    save_state(state)


def handle_list(_: argparse.Namespace) -> None:
    state = ensure_initialized()
    manifest = load_manifest()
    roots = dedupe_and_normalize_roots(manifest["roots"] + state["pending_adds"] + state["pending_removes"])
    if not roots:
        print("no tracked roots")
        return
    for entry in roots:
        labels = ", ".join(root_statuses(entry, state, manifest))
        print(f"{token_to_local_path(entry['path'])} [{labels}]")


def handle_status(args: argparse.Namespace) -> None:
    state = ensure_initialized()
    status = collect_status(state, offline=args.offline)

    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return

    remote_state = str(status["remote"]["state"])
    remote_messages = ["remote changed since last sync"]
    if remote_state == "in_sync":
        remote_messages = ["remote matches last sync commit"]
    elif remote_state == "empty":
        remote_messages = ["remote has no commits yet"]
    print_status_section("remote", remote_messages)
    print_status_section("pending", status["pending"])
    print_status_section("remote vs local", status["differences"])
    print_status_section("pull blockers", status["pull_blockers"])
    print_status_section("stale local paths", status["stale_local_paths"])

    if status["clean"]:
        print("clean")


def handle_sync_push(_: argparse.Namespace) -> None:
    state = ensure_initialized()
    remote_head = fetch_remote()
    if remote_head != state["last_sync_commit"]:
        raise AppError("remote changed; run `sync pull` first")

    branch = current_branch()
    local_head = git_rev_parse("HEAD")
    if remote_head and local_head != remote_head:
        git(["pull", "--ff-only", "origin", branch])

    manifest = load_manifest()
    roots = gather_effective_roots(state, manifest)
    directories, files, notices = expand_roots(roots)

    clear_repo_files_tree()
    for token in directories:
        token_to_repo_path(token).mkdir(parents=True, exist_ok=True)
    for token in files:
        repo_path = token_to_repo_path(token)
        ensure_parent_dir(repo_path)
        shutil.copyfile(token_to_local_path(token), repo_path)

    write_manifest({
        "version": MANIFEST_VERSION,
        "roots": roots,
        "directories": directories,
        "files": files,
    })

    if worktree_dirty():
        git(["add", "-A"])
        git(["commit", "-m", "Sync tracked user config files"])
        git(["push", "-u", "origin", branch])

    state["pending_adds"] = []
    state["pending_removes"] = []
    state["last_sync_commit"] = git_rev_parse("HEAD")
    state["last_synced_hashes"] = {token: metadata["sha256"] for token, metadata in files.items()}
    state["last_synced_directories"] = sorted(directories)
    save_state(state)
    for notice in notices:
        print(notice)
    print(f"pushed {len(files)} tracked file(s) across {len(roots)} root(s)")


def handle_sync_pull(args: argparse.Namespace) -> None:
    state = ensure_initialized()
    if state["pending_adds"] or state["pending_removes"]:
        raise AppError("pending add/remove changes exist; push them or clear them first")

    previous_files = set(state["last_synced_hashes"])
    previous_directories = set(state["last_synced_directories"])
    remote_head = sync_clone_with_remote()
    manifest = load_manifest()

    if not args.force:
        blocked = blockers_for_pull(manifest, state)
        if blocked:
            raise AppError(
                "local tracked files changed; rerun with --force to overwrite ("
                + "; ".join(blocked)
                + ")"
            )

    for token in sorted(manifest["directories"]):
        local_path = token_to_local_path(token)
        if local_path.exists() and not local_path.is_dir():
            if not args.force:
                raise AppError(f"path collision: {local_path}")
            if local_path.is_file() or local_path.is_symlink():
                local_path.unlink()
            else:
                shutil.rmtree(local_path)
        local_path.mkdir(parents=True, exist_ok=True)

    for token in sorted(manifest["files"]):
        source_path = token_to_repo_path(token)
        if not source_path.exists():
            raise AppError(f"repo payload missing for {token}")
        destination_path = token_to_local_path(token)
        ensure_parent_dir(destination_path)
        if destination_path.exists() and destination_path.is_dir():
            if not args.force:
                raise AppError(f"path collision: {destination_path}")
            shutil.rmtree(destination_path)
        shutil.copyfile(source_path, destination_path)
        apply_metadata(destination_path, manifest["files"][token])

    for token in sorted(manifest["directories"], key=lambda value: value.count("/"), reverse=True):
        apply_metadata(token_to_local_path(token), manifest["directories"][token])

    removed_files = sorted(previous_files - set(manifest["files"]))
    removed_directories = sorted(previous_directories - set(manifest["directories"]))
    for token in removed_files:
        print(f"no longer managed locally: {token_to_local_path(token)}")
    for token in removed_directories:
        print(f"no longer managed locally: {token_to_local_path(token)}")

    state["last_sync_commit"] = remote_head
    state["last_synced_hashes"] = {token: metadata["sha256"] for token, metadata in manifest["files"].items()}
    state["last_synced_directories"] = sorted(manifest["directories"])
    save_state(state)
    print(f"pulled {len(manifest['files'])} tracked file(s) across {len(manifest['roots'])} root(s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="os-user-conf-sync")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="register a git repo url")
    init_parser.add_argument("repo_url")
    init_parser.set_defaults(handler=handle_init)

    add_parser = subparsers.add_parser("add", help="queue a file or directory for tracking")
    add_parser.add_argument("-r", "--recursive", action="store_true", help="track directories recursively")
    add_parser.add_argument("-i", "--interactive", action="store_true", help="ask about children recursively")
    add_parser.add_argument("path")
    add_parser.set_defaults(handler=handle_add)

    remove_parser = subparsers.add_parser("remove", help="queue a tracked root for removal")
    remove_parser.add_argument("path")
    remove_parser.set_defaults(handler=handle_remove)

    list_parser = subparsers.add_parser("list", help="show tracked roots")
    list_parser.set_defaults(handler=handle_list)

    status_parser = subparsers.add_parser("status", help="show remote and local sync differences")
    status_parser.add_argument("--json", action="store_true", help="print machine-readable status")
    status_parser.add_argument("--offline", action="store_true", help="use cached remote state without fetch")
    status_parser.set_defaults(handler=handle_status)

    sync_parser = subparsers.add_parser("sync", help="synchronize tracked files")
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command", required=True)

    push_parser = sync_subparsers.add_parser("push", help="push local tracked files to remote")
    push_parser.set_defaults(handler=handle_sync_push)

    pull_parser = sync_subparsers.add_parser("pull", help="pull tracked files from remote")
    pull_parser.add_argument("--force", action="store_true", help="overwrite modified local files")
    pull_parser.set_defaults(handler=handle_sync_pull)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
