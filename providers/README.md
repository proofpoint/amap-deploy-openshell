# providers/

This deployment's own OpenShell provider profile, `amap-claude-code.json`.

## Where it came from

It is adapted from OpenShell's `providers/claude-code.yaml` (OpenShell
main@acbac9c, identical at v0.1.2). OpenShell does not load that example: a
profile must be imported explicitly, and OpenShell's own `providers/README.md`
asks an adapted copy to take an ID of its own.

The upstream file carries this notice, which applies to the adapted material
(see also `NOTICE` at the repository root):

```text
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
```

## What differs from the example

- The ID is `amap-claude-code`, not `claude-code`.
- The credential is the Console API key only (decision D10):
  `ANTHROPIC_API_KEY`. The alternative variable the example also lists is
  dropped.
- The only endpoint is the Anthropic API, on port 443. The example's telemetry
  endpoints are dropped, as its header allows. The settings file in `payload/`
  also switches off Claude Code's non-essential traffic.
- `binaries` is empty on purpose. OpenShell matches the real path of the
  executable that opens a connection (OpenShell
  main@acbac9c:docs/how-it-works/policies/network-rules.mdx:57-77, "Binary
  Matching"), and a rule also covers processes that a listed binary starts
  (network-rules.mdx:72-73). An empty list matches nothing and allows nothing,
  so the shipped file grants no access until it is rendered.

## How `binaries` gets filled

`l1-kit profile` renders a copy under `$AMAP_OPENSHELL_HOME/providers/` with
`binaries` set to the real paths of `node` and of the `claude` entry point in the
built image, read with `readlink -f`. Nothing is guessed: with no path, nothing is
rendered. Which of the two opens the connection depends on how the npm package
installs Claude Code, so both are listed; a listed path that is never an
executable is harmless.

## Importing it

Run `l1-run` step 3, or the commands in section 3 of `docs/L1-RUNBOOK.md`. The
import is create-only, so a copy that differs is updated with its
`resource_version`. The profile is imported into the fleet's workspace, not
globally.
