# TODO

## Next steps

1. Add config management commands

   Right now configuration is editable only by changing the JSON file manually.
   Add small CLI helpers for reading and updating config values, starting with `large_directory_warning_bytes`.
   The goal is to keep the config surface small, but make it easier to inspect defaults, adjust thresholds, and validate values without hand-editing files under `~/.config`.

2. Extract a thin service layer for the future TUI

   The current implementation keeps most logic in one CLI-oriented module, which is fine for now.
   Before starting the TUI, split the orchestration into a small reusable service API that exposes operations like init, add, remove, inspect state, push, and pull.
   That keeps the TUI from duplicating business logic, reduces regressions, and makes later testing of UI-independent sync behavior much easier.
