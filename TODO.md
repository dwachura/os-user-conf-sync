# TODO

## Next steps

1. Add a dedicated `status` command

   Build a clearer read-only overview of the current sync state so the user can tell what action is safe before running `push` or `pull`.
   This should go beyond `list` and focus on actionable states such as modified tracked files, pending adds/removes, missing paths, unsupported entries, and remote-change blockers.
   Good output here will also make the later TUI simpler, because the same state model can drive both the CLI and UI.

2. Add config management commands

   Right now configuration is editable only by changing the JSON file manually.
   Add small CLI helpers for reading and updating config values, starting with `large_directory_warning_bytes`.
   The goal is to keep the config surface small, but make it easier to inspect defaults, adjust thresholds, and validate values without hand-editing files under `~/.config`.

3. Extract a thin service layer for the future TUI

   The current implementation keeps most logic in one CLI-oriented module, which is fine for now.
   Before starting the TUI, split the orchestration into a small reusable service API that exposes operations like init, add, remove, inspect state, push, and pull.
   That keeps the TUI from duplicating business logic, reduces regressions, and makes later testing of UI-independent sync behavior much easier.
