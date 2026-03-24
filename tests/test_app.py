from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from os_user_conf_sync.app import config_path, data_root, load_state, main, repo_dir, state_path, token_to_local_path


def run_cli(
    argv: list[str],
    env: dict[str, str],
    *,
    answers: list[str] | None = None,
    interactive: bool = False,
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=False))
        if answers is not None:
            stack.enter_context(patch("builtins.input", side_effect=answers))
        if interactive:
            stack.enter_context(patch("sys.stdin.isatty", return_value=True))
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def git(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


class OsUserConfSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.remote_repo = base / "remote.git"
        self.home_one = base / "home-one"
        self.home_two = base / "home-two"
        self.data_one = base / "xdg-one"
        self.data_two = base / "xdg-two"
        self.config_one = base / "cfg-one"
        self.config_two = base / "cfg-two"

        for path in [self.home_one, self.home_two, self.data_one, self.data_two, self.config_one, self.config_two]:
            path.mkdir()

        git(["git", "init", "--bare", str(self.remote_repo)])

        author = {
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        self.env_one = {
            "HOME": str(self.home_one),
            "XDG_DATA_HOME": str(self.data_one),
            "XDG_CONFIG_HOME": str(self.config_one),
            **author,
        }
        self.env_two = {
            "HOME": str(self.home_two),
            "XDG_DATA_HOME": str(self.data_two),
            "XDG_CONFIG_HOME": str(self.config_two),
            **author,
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def init_repo(self, env: dict[str, str]) -> None:
        code, _, _ = run_cli(["init", str(self.remote_repo)], env)
        self.assertEqual(code, 0)

    def test_add_tracks_home_file(self) -> None:
        bashrc = self.home_one / ".bashrc"
        bashrc.write_text("export TEST=1\n")

        self.init_repo(self.env_one)
        code, _, _ = run_cli(["add", str(bashrc)], self.env_one)
        self.assertEqual(code, 0)

        with patch.dict(os.environ, self.env_one, clear=False):
            state = load_state()
            self.assertEqual(state["pending_adds"], [{"kind": "file", "path": "$HOME/.bashrc"}])
            self.assertEqual(token_to_local_path("$HOME/.bashrc"), bashrc)

    def test_dirs_lists_important_paths(self) -> None:
        code, stdout, _ = run_cli(["dirs"], self.env_one)
        self.assertEqual(code, 0)

        with patch.dict(os.environ, self.env_one, clear=False):
            self.assertIn(f"home: {self.home_one}", stdout)
            self.assertIn(f"config-root: {self.config_one / 'os-user-conf-sync'}", stdout)
            self.assertIn(f"config-file: {config_path()}", stdout)
            self.assertIn(f"data-root: {data_root()}", stdout)
            self.assertIn(f"state-file: {state_path()}", stdout)
            self.assertIn(f"repo-cache: {repo_dir()}", stdout)
            self.assertIn(f"repo-files: {repo_dir() / 'files'}", stdout)

    def test_directory_requires_recursive_flag(self) -> None:
        config_dir = self.home_one / ".config" / "app"
        config_dir.mkdir(parents=True)
        self.init_repo(self.env_one)

        code, _, stderr = run_cli(["add", str(config_dir)], self.env_one)
        self.assertEqual(code, 1)
        self.assertIn("directory tracking requires -r", stderr)

    def test_recursive_add_warns_for_large_directory_and_symlink(self) -> None:
        config_dir = self.home_one / ".config" / "app"
        config_dir.mkdir(parents=True)
        (config_dir / "big.txt").write_bytes(b"x" * 2048)
        (config_dir / "link.txt").symlink_to(config_dir / "big.txt")
        self.init_repo(self.env_one)

        with patch.dict(os.environ, self.env_one, clear=False):
            cfg_path = config_path()
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps({"large_directory_warning_bytes": 1024}) + "\n")

        code, stdout, _ = run_cli(["add", "-r", str(config_dir)], self.env_one, answers=["y"])
        self.assertEqual(code, 0)
        self.assertIn("warning: directory size exceeds configured threshold", stdout)
        self.assertIn("warning: encountered 1 symlink(s)", stdout)
        self.assertIn("queued add dir", stdout)

    def test_interactive_recursive_add_can_mix_subtree_and_files(self) -> None:
        root = self.home_one / ".config" / "app"
        nested = root / "nested"
        nested.mkdir(parents=True)
        (root / "keep.txt").write_text("keep\n")
        (nested / "all.txt").write_text("all\n")
        (nested / "skip.txt").write_text("skip\n")
        self.init_repo(self.env_one)

        code, stdout, _ = run_cli(
            ["add", "-r", "-i", str(root)],
            self.env_one,
            answers=["e", "y", "a", "y"],
            interactive=True,
        )
        self.assertEqual(code, 0)
        self.assertIn("queued add file", stdout)
        self.assertIn("queued add dir", stdout)

        with patch.dict(os.environ, self.env_one, clear=False):
            state = load_state()
            self.assertEqual(
                state["pending_adds"],
                [
                    {"kind": "file", "path": "$HOME/.config/app/keep.txt"},
                    {"kind": "dir", "path": "$HOME/.config/app/nested"},
                ],
            )

    def test_push_and_pull_preserve_directory_and_file_metadata(self) -> None:
        root = self.home_one / ".config" / "app"
        root.mkdir(parents=True)
        source_file = root / "settings.ini"
        source_file.write_text("value=1\n")
        os.chmod(root, 0o700)
        os.chmod(source_file, 0o640)
        dir_times = (1_700_000_000_023_456_789, 1_700_000_000_123_456_789)
        file_times = (1_700_000_000_223_456_789, 1_700_000_000_323_456_789)
        os.utime(root, ns=dir_times)
        os.utime(source_file, ns=file_times)

        self.init_repo(self.env_one)
        self.assertEqual(run_cli(["add", "-r", str(root)], self.env_one, answers=["y"])[0], 0)
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        self.init_repo(self.env_two)
        self.assertEqual(run_cli(["sync", "pull"], self.env_two)[0], 0)

        pulled_dir = self.home_two / ".config" / "app"
        pulled_file = pulled_dir / "settings.ini"
        self.assertEqual(stat.S_IMODE(pulled_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(pulled_file.stat().st_mode), 0o640)
        self.assertEqual(pulled_dir.stat().st_mtime_ns, dir_times[1])
        self.assertEqual(pulled_file.stat().st_mtime_ns, file_times[1])
        self.assertEqual(pulled_file.read_text(), "value=1\n")

    def test_pull_refuses_local_changes_without_force(self) -> None:
        source_file = self.home_one / ".bashrc"
        source_file.write_text("export ORIGINAL=1\n")

        self.init_repo(self.env_one)
        self.assertEqual(run_cli(["add", str(source_file)], self.env_one)[0], 0)
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        self.init_repo(self.env_two)
        self.assertEqual(run_cli(["sync", "pull"], self.env_two)[0], 0)

        pulled_file = self.home_two / ".bashrc"
        pulled_file.write_text("export LOCAL=1\n")

        code, _, stderr = run_cli(["sync", "pull"], self.env_two)
        self.assertEqual(code, 1)
        self.assertIn("local tracked files changed", stderr)

        code, _, _ = run_cli(["sync", "pull", "--force"], self.env_two)
        self.assertEqual(code, 0)
        self.assertEqual(pulled_file.read_text(), "export ORIGINAL=1\n")

    def test_pull_notifies_about_removed_remote_file_but_keeps_local_copy(self) -> None:
        root = self.home_one / ".config" / "app"
        root.mkdir(parents=True)
        keep_file = root / "keep.txt"
        remove_file = root / "remove.txt"
        keep_file.write_text("keep\n")
        remove_file.write_text("remove\n")

        self.init_repo(self.env_one)
        self.assertEqual(run_cli(["add", "-r", str(root)], self.env_one, answers=["y"])[0], 0)
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        self.init_repo(self.env_two)
        self.assertEqual(run_cli(["sync", "pull"], self.env_two)[0], 0)

        remove_file.unlink()
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        code, stdout, _ = run_cli(["sync", "pull"], self.env_two, answers=[])
        self.assertEqual(code, 0)
        self.assertIn("no longer managed locally", stdout)
        self.assertTrue((self.home_two / ".config" / "app" / "remove.txt").exists())

    def test_status_reports_clean_state(self) -> None:
        source_file = self.home_one / ".bashrc"
        source_file.write_text("export ORIGINAL=1\n")

        self.init_repo(self.env_one)
        self.assertEqual(run_cli(["add", str(source_file)], self.env_one)[0], 0)
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        code, stdout, _ = run_cli(["status"], self.env_one)
        self.assertEqual(code, 0)
        self.assertIn("remote matches last sync commit", stdout)
        self.assertIn("clean", stdout)

        code, stdout, _ = run_cli(["status", "--json"], self.env_one)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["version"], 1)
        self.assertTrue(payload["clean"])
        self.assertEqual(payload["remote"]["state"], "in_sync")
        self.assertEqual(payload["pending"], [])

    def test_status_reports_remote_and_local_differences(self) -> None:
        source_file = self.home_one / ".bashrc"
        source_file.write_text("export ORIGINAL=1\n")

        self.init_repo(self.env_one)
        self.assertEqual(run_cli(["add", str(source_file)], self.env_one)[0], 0)
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        self.init_repo(self.env_two)
        self.assertEqual(run_cli(["sync", "pull"], self.env_two)[0], 0)

        source_file.write_text("export REMOTE=1\n")
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        pulled_file = self.home_two / ".bashrc"
        pulled_file.write_text("export LOCAL=1\n")

        code, stdout, _ = run_cli(["status"], self.env_two)
        self.assertEqual(code, 0)
        self.assertIn("remote changed since last sync", stdout)
        self.assertIn(f"modified file: {pulled_file}", stdout)
        self.assertIn("pull blockers:", stdout)

        code, stdout, _ = run_cli(["status", "--json"], self.env_two)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertFalse(payload["clean"])
        self.assertEqual(payload["remote"]["state"], "changed")
        self.assertIn(
            {
                "kind": "file",
                "path": str(pulled_file),
                "status": "modified",
                "token": "$HOME/.bashrc",
            },
            payload["differences"],
        )
        self.assertIn(
            {
                "kind": "file",
                "path": str(pulled_file),
                "status": "modified",
                "token": "$HOME/.bashrc",
            },
            payload["pull_blockers"],
        )

    def test_status_offline_uses_cached_remote_state(self) -> None:
        source_file = self.home_one / ".bashrc"
        source_file.write_text("export ORIGINAL=1\n")

        self.init_repo(self.env_one)
        self.assertEqual(run_cli(["add", str(source_file)], self.env_one)[0], 0)
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        self.init_repo(self.env_two)
        self.assertEqual(run_cli(["sync", "pull"], self.env_two)[0], 0)

        source_file.write_text("export REMOTE=1\n")
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        code, stdout, _ = run_cli(["status", "--offline"], self.env_two)
        self.assertEqual(code, 0)
        self.assertIn("remote matches last sync commit", stdout)
        self.assertIn("clean", stdout)

        code, stdout, _ = run_cli(["status", "--offline", "--json"], self.env_two)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["clean"])
        self.assertEqual(payload["remote"]["mode"], "offline")
        self.assertEqual(payload["remote"]["state"], "in_sync")

    def test_status_reports_pending_and_no_longer_managed_paths(self) -> None:
        root = self.home_one / ".config" / "app"
        root.mkdir(parents=True)
        keep_file = root / "keep.txt"
        remove_file = root / "remove.txt"
        keep_file.write_text("keep\n")
        remove_file.write_text("remove\n")

        self.init_repo(self.env_one)
        self.assertEqual(run_cli(["add", "-r", str(root)], self.env_one, answers=["y"])[0], 0)
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        self.init_repo(self.env_two)
        self.assertEqual(run_cli(["sync", "pull"], self.env_two)[0], 0)

        code, stdout, _ = run_cli(["add", str(self.home_two / ".gitconfig")], self.env_two)
        self.assertEqual(code, 1)

        extra_file = self.home_two / ".gitconfig"
        extra_file.write_text("[user]\nname = Test\n")
        code, stdout, _ = run_cli(["add", str(extra_file)], self.env_two)
        self.assertEqual(code, 0)

        remove_file.unlink()
        self.assertEqual(run_cli(["sync", "push"], self.env_one)[0], 0)

        code, stdout, _ = run_cli(["status"], self.env_two)
        self.assertEqual(code, 0)
        self.assertIn(f"pending add file: {extra_file}", stdout)
        self.assertIn(f"no longer managed remotely: {self.home_two / '.config' / 'app' / 'remove.txt'}", stdout)

        code, stdout, _ = run_cli(["status", "--json"], self.env_two)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertIn(
            {
                "action": "add",
                "kind": "file",
                "path": str(extra_file),
                "status": "pending",
                "token": "$HOME/.gitconfig",
            },
            payload["pending"],
        )
        self.assertIn(
            {
                "kind": "file",
                "path": str(self.home_two / ".config" / "app" / "remove.txt"),
                "status": "no_longer_managed",
                "token": "$HOME/.config/app/remove.txt",
            },
            payload["stale_local_paths"],
        )


if __name__ == "__main__":
    unittest.main()
