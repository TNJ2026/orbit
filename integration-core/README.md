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

## One consumer, so far

`integrations/deepseek-harness`. That is worth stating plainly: a module with a
single caller has been *separated*, not yet *shown to be reusable*, and the two
are easy to confuse.

The separation still earns its keep — the boundary is checkable rather than a
matter of taste, and it is what made `ORBIT_ERROR_KEYS` possible: the set of
things that can go wrong used to be typed as `keyof typeof en`, which made it a
property of one panel's copy. It is not. A host supplies the wording, and the
panel's "Reopen the panel to start it" is not a sentence a background process
can say.

A second consumer is the thing that would prove the rest. Note that Orbit
already carries one route for other hosts — `orbit agent-app mcp-proxy`, a
stdio adapter in Python — so a second *TypeScript* host is not automatically
the next thing to build.

## Running it

    npm install && npm test

Tests run against `lib/`, not `src/`: Node's type stripping cannot parse
constructor parameter properties, so `npm test` compiles first.
