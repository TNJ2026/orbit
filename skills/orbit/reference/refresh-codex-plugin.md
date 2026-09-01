# Refresh the local Orbit plugin in Codex

Use this procedure when the user asks to rebuild, refresh, reinstall, or make
Codex reload the locally developed Orbit plugin. It prepares everything that
must happen before the user fully quits and reopens Codex. Do not quit, reopen,
or otherwise control Codex from the active task.

## Preconditions

1. Resolve the absolute Orbit plugin root from the current workspace. It must
   contain `.codex-plugin/plugin.json`; do not assume a username-specific path.
2. Read the `plugin-creator` skill and its plugin update instructions before
   changing installation metadata. Use its scripts rather than editing the
   cachebuster or marketplace files by hand.
3. Inspect the working tree and preserve unrelated user changes. This workflow
   updates the cachebuster in `.codex-plugin/plugin.json`, so report that
   expected change.
4. Resolve the personal marketplace name with
   `scripts/read_marketplace_name.py`. Confirm that the installed Orbit entry's
   source resolves to the plugin root. Stop if it points at another checkout.

## Validate and reinstall

1. Run the plugin validator from `plugin-creator`. Use the Orbit project's
   `.venv/bin/python` when available because the validator requires PyYAML.
2. Run focused tests for the files changed in the current task. At minimum,
   validate the Orbit skill when it changed. Do not reinstall a known-broken
   plugin.
3. Run `scripts/update_plugin_cachebuster.py <absolute-plugin-root>`. Never
   hand-edit the generated `+codex.<timestamp>` suffix.
4. Reinstall with `codex plugin add orbit@<resolved-marketplace-name>`. Do not
   add the default personal marketplace again and do not edit Codex config by
   hand.
5. Run `codex plugin list`. Confirm Orbit is enabled, its source resolves to the
   intended plugin root, and its installed version exactly matches the version
   now present in `.codex-plugin/plugin.json`.

## Stop the old Runtime safely

The running Runtime can keep serving the previous MCP App bundle even after the
plugin is reinstalled.

1. Inspect Orbit runs before stopping the Runtime. If a run is active or waiting
   for attention, explain that stopping the Runtime will interrupt it and ask
   for confirmation unless the user already explicitly authorized that
   interruption.
2. Resolve the exact listener with `lsof -nP -iTCP:8848 -sTCP:LISTEN`. Verify
   that the PID belongs to the Orbit Runtime for the intended workspace. Never
   use a broad process-name kill, a glob, or an unresolved PID.
3. If the verified listener exists and it is safe to interrupt, run
   `kill <exact-pid>`, then repeat the `lsof` check until the listener is gone.
   If no listener exists, skip this step.

## Handoff boundary

Stop here. Tell the user that preparation is complete and ask them to:

1. fully quit Codex, not merely close its window;
2. reopen Codex; and
3. start a new task before testing the Orbit dashboard or another Orbit card.

Do not claim that restarting only Orbit or opening another card reloads the
plugin. Codex loads installed plugin metadata and skills at application/task
boundaries, so the clean restart and new task are part of verification.
