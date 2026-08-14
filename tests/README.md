# Running the tests

```bash
.venv/bin/python -m unittest discover -s tests
node --test tests/ui/client_modules.test.mjs
cd ui/editor && node --test src/*.test.mjs
```

The browser and visual suites skip themselves unless Playwright is installed.
`VISUAL_UPDATE=1` regenerates the baselines in `visual_baselines/` — review the
resulting images, since a regenerated baseline asserts nothing about what it
now contains.

## Conventions

**`TemporaryDirectory(ignore_cleanup_errors=True)` wherever a test opens a
database.** A SQLite connection opened during a test may still be waiting to
be collected when the directory goes, and closing it writes `-wal` back into a
directory `rmtree` has already walked. That failed roughly one run in ten, in
`tearDown`, in whichever test happened to be last — never in an assertion and
never the same test twice. The flag is for that specific race; a test that
opens no database does not need it.

**Fixtures that reach the network are the network's flake, not the
Runtime's.** `test_browser_e2e` serves third-party requests empty for this
reason: the page asked a font CDN for typefaces, that request 404'd about one
run in six, and it was reported as the Runtime raising an error on every view.

**A schema validated only against samples written beside it agrees with itself
for ever.** `test_ui_contract_goldens` checks the served payload too, which is
what caught `allowed_commands` drifting away from its frozen shape.
