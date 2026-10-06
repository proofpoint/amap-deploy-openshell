# CLAUDE.md — amap-deploy-openshell

This repo deploys AMAP on OpenShell hosts. It is the sibling of
amap-deploy-sandy, and it is **host-side only**: nothing here runs inside a
sandbox, and nothing here sends or receives a message.

## Hard rules

- **Don't change the connector or the router.** amap-connector-claude and
  amap-router-local are used unmodified. If one of them seems to need an
  OpenShell-specific change, stop and report it as a finding for that repo.
  Don't patch it here.
- **Don't invent wire fields.** The contract is amap-spec: `draft/draft-amap.xml`
  plus `schemas/` and `fixtures/`. A capability gap goes to amap-spec as a
  proposal first.
- **No network transport for the spool.** If OpenShell can't share a directory
  with the right read-only and read-write split, the deployment is unsupported.
  Say so, and don't tunnel the spool over HTTP.
- **Mark assumptions.** DESIGN.md tags every OpenShell claim `[docs]` or
  `[unverified]`. When you confirm one, change the tag and cite the file, page or
  command output. Never upgrade a claim without evidence.
- **Verify, don't assert.** UNKNOWN is never a pass. Claim conformance only with
  the fixture run *and* the operational checks, as amap-spec's CONFORMANCE.md
  requires.

## Publication

This repository is public. Of projects this work could be associated with,
name only OpenShell and the AMAP repos (amap-spec, amap-router-local,
amap-connector-claude, amap-deploy-sandy, amap-deploy-openshell) in anything
shipped. Real dependencies may be named where they are used: Docker, Ubuntu,
Proxmox, Claude Code, Anthropic, NVIDIA as OpenShell's author, and Python
packages, for example. Private projects, people and hosts may not: no personal
names or handles (outside `.github/CODEOWNERS`), no personal email addresses, no
private host names or addresses, and no absolute host paths. Refer to a test
machine as "the test host" and write `<host>`, `<vm>` or `<user>` in examples.
Fake credentials in tests must not match a real credential format.

## Where to start

Read [README.md](README.md), then [docs/TUTORIAL.md](docs/TUTORIAL.md) for the
deployment end to end, and [DESIGN.md](DESIGN.md) for the five properties AMAP
needs from a host and how OpenShell supplies each. Phases 0 to 2 are done:
[docs/POC-REPORT.md](docs/POC-REPORT.md) records the live runs. What remains is
in [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md) (L3, the publication gate),
PLAN.md phase 3 (optional), and DESIGN.md section 9 (widening the scope).
amap-deploy-sandy's README, `docs/TUTORIAL.md` and the "Why sandy" screen in its
`docs/index.html` show the same five properties supplied another way.

## Tests

Run `python3 -m pytest tests -q`. `pytest.ini` pins the rootdir, so the root
`conftest.py` loads from any working directory, including `tests/`.

The suite checks against four read-only sources. Each is found by walking up
from `tests/_workspace.py` and taking the nearest directory beside an ancestor
that has the confirming file, or by an environment variable.

| Source | Directory name(s) beside an ancestor | Override variable | Confirming file |
|---|---|---|---|
| router | `amap-router-local` | `AMAP_ROUTER_REPO` | `router/reset.py` |
| connector | `amap-connector-claude` | `AMAP_CONNECTOR_REPO` | `bin/inbox-delivery` |
| sandy | `amap-deploy-sandy` | `AMAP_SANDY_REPO` | `fleet_policy.py` |
| amap-spec | `amap-spec`, `.amap-spec` | `AMAP_SPEC_DIR` | `fixtures/validate.py` |

- A variable that is set is the only place searched.
- An empty variable counts as unset.
- A directory without the confirming file is not a match.

**A missing source fails the run.** `pytest_configure` raises a usage error
(exit 4) that names every missing source, before anything is collected. The run
never passes as a smaller set of tests.

**The four sources are read-only.** Their git trees are fingerprinted before and
after the session: status (with untracked files), the unstaged and staged
diffs, and a hash of each untracked file. The fingerprint runs git against a
private copy of the index, because `git diff` rewrites a stale index even under
`--no-optional-locks`. amap-spec, which is not a git tree, gets a content hash.
Bytecode writing is off for the whole session.

**The suite never runs OpenShell, Docker or Podman.** `subprocess.Popen` is
guarded for the session, and a call to one of those programs that no test
stubbed raises `UnstubbedBinary`. To stub one, write a fake with
`_harness.write_fake` into the `fake_bin` fixture's directory, or into a
directory given to `_harness.stubbed()`. The guard does not cover `os.system`,
`os.exec*` or `os.posix_spawn`, and for an argv list it checks only the first
word.

No pass or fail totals go in any file.
