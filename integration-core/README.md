# integration-core

Everything an Orbit integration needs that is not about one host.

Orbit is reached the same way from anywhere: find the Runtime serving a
project, start one if none is, speak MCP to it, decode what comes back, and
say something useful when any of that fails. None of that is about panels,
slash commands, or composers — and all of it was written twice the first time
somebody built a second integration. It lives here so it is written once.

## What belongs here

A module belongs here if it imports nothing from a host SDK and touches no
DOM. That is a mechanical test, and it is the only one:

- `gateway.ts` — discovery, auto-start, MCP over HTTP, and the readiness and
  protocol checks that decide a Runtime is usable.
- `codecs.ts`, `types.ts` — the wire shapes and how to read them.
- `error-text.ts` — every failure this integration can meet, classified into
  `ORBIT_ERROR_KEYS`. **The vocabulary is here; the wording is the host's.**
- `orbit-model.ts`, `run-progress.ts` — a Run's shape for anything that draws.
- `workflow-catalog.ts` — what a model is told can run.
- `authoring-claim.ts`, `authoring-progress.ts` — the writer loop.
- `artifact-export.ts` — handing an Artifact over as a file.
- `session-bridge.ts` — mirroring Runs into a host's transcript, through
  interfaces the host supplies.

## What does not

Anything that knows what a panel is. Floating-window geometry, caret placement
in a composer, React components, host RPC decorators — those are a host's
shape, and every host has a different one.

## Two consumers, on purpose

`integrations/deepseek-harness` and `integrations/claude-app`. A module with
one caller was never shown to be reusable; the error vocabulary is the clearest
case, because the two hosts word the same failure differently and neither
could have owned the list.

## Running it

    npm install && npm test

Tests run against `lib/`, not `src/`: Node's type stripping cannot parse
constructor parameter properties, so `npm test` compiles first.
