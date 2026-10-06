# amap-deploy-openshell

**Status: verified live on one host, under the scope below.** A fresh VM, set up
by `provision-guest.sh` and `bring-up`, has passed every check
[docs/POC-REPORT.md](docs/POC-REPORT.md) records:
- the proof of concept (L1);
- the full bring-up with this repository's tooling (L2);
- the rerun with the mounts interceptor on (L3's precondition, 2026-10-05).

**Site:** [the explainer](https://proofpoint.github.io/amap-deploy-openshell/)
(`docs/index.html`) walks through AMAP on OpenShell with animated flows.
GitHub Pages publishes it from `docs/`.

## What it is

This repo deploys AMAP (the Agent Mailbox Access Protocol, specified in
[amap-spec](https://github.com/proofpoint/amap-spec)) on hosts that run
[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) (Apache-2.0).

It is the OpenShell sibling of
[amap-deploy-sandy](https://github.com/proofpoint/amap-deploy-sandy). That
repo's own README states the premise: sandy is "one isolation choice, not a
requirement", and any host that provides credential, network and filesystem
isolation can replace it, "with a deployment of its own in place of this repo".
This is that deployment.

## Scope

One OpenShell host, one operator. The router, the connector's payload and every
sandbox run on that host, and everything that can call its gateway acts for
that operator. The repository protects agents from the host and from each other.
It does not protect the host from its operator. Running several operators, or a
fleet of hosts, needs the work listed in
[DESIGN.md "Widening the scope"](DESIGN.md#9-widening-the-scope) first.

## What stays the same

- **[amap-connector-claude](https://github.com/proofpoint/amap-connector-claude)**,
  unmodified. It provides the two MCP servers the agent talks to and the delivery
  daemon that runs beside the session.
- **[amap-router-local](https://github.com/proofpoint/amap-router-local)**,
  unmodified. It runs in its own container on the host and has no network.
- **The wire contract.** Nothing OpenShell-specific enters `amap-spec`.

What changes is only this: how each sandbox gets its lanes, its mounts, its
identity, its MCP configuration and its delivery daemon. OpenShell does that
instead of sandy.

## Requirements

- Linux 6.2 or later (Landlock ABI 3) with Docker. Docker is the only compute
  driver (D3).
- OpenShell `v0.1.2` or later, with the gateway configured as `gateway-config`
  prints (D6, DESIGN.md section 6).
- **Three sibling checkouts**, each beside an ancestor of this repository or
  named by its variable: `amap-router-local` (`$AMAP_ROUTER_REPO`),
  `amap-connector-claude` (`$AMAP_CONNECTOR_REPO`) and `amap-deploy-sandy`
  (`$AMAP_SANDY_REPO`). The last is not optional: this repository imports the
  pure core of sandy's `fleet_policy.py` (D1), so every verb needs it.
  `$AMAP_SPEC_DIR` is needed only by the tests and by the L1 document checks.
- The mounts interceptor, running and registered in `gateway.toml`
  (docs/INTERCEPTOR.md). On the example VM,
  `examples/vms/proxmox/provision-guest.sh --home "$AMAP_OPENSHELL_HOME" --apply`
  deploys it.
- Python 3.9 or later, standard library only. pytest for the tests.
- A Console API key for this deployment's provider profile `amap-claude-code`,
  exported as `ANTHROPIC_API_KEY` in the terminal that provisions (D10).
- The Claude Code version you have measured. There is no default.

## What gets installed

Everything lives under `$AMAP_OPENSHELL_HOME`. `install --apply` and
`provision-guest.sh --home DIR --apply` record that path in
`${XDG_CONFIG_HOME:-$HOME/.config}/amap-openshell/home`. After that,
`amap-openshell.py` finds the home without `--home` or the variable, in any
shell or over plain `ssh` (D25). Order: `--home`, then
`$AMAP_OPENSHELL_HOME`, then the record. `teardown --apply` removes the record
when it names the home torn down.

| Path | What it is | Whose |
|---|---|---|
| `$AMAP_OPENSHELL_HOME/fleet.json` | the fleet **policy**: `fleet_domain`, `groups`, `default_peers`, `peers`, `task_graph`, `task_deny`, plus this deployment's `workspace` | seeded by `install` from `--fleet`, with `fleet_domain` `openshell.<host>.<base>` (D21); then yours to edit, never rewritten |
| `$AMAP_OPENSHELL_HOME/payload` | the connector's binaries, the main-process wrapper, the session lister, the seeder, `mcp-servers.json`, `claude-settings.json`, `INBOX-POLICY.md`. Mounted read-only at `/opt/amap/payload` | `install` |
| `$AMAP_OPENSHELL_HOME/roster` | the roster the router writes. Mounted read-only at `/opt/amap/roster` | created empty by `install`, written by the router |
| `$AMAP_OPENSHELL_HOME/instances/<name>/inbox`, `peer`, `outbox` | the member's lanes, under the lanes directory in the sandbox. `inbox` and `peer` read-only, `outbox` read-write | `provision` |
| `$AMAP_OPENSHELL_HOME/policies/<name>.yaml` | the sandbox's OpenShell policy | `provision` |
| `$AMAP_OPENSHELL_HOME/commands/create-<name>.sh` | the `openshell sandbox create` argument vector, as run | `provision` |
| `$AMAP_OPENSHELL_HOME/membership.json` | `(workspace, name, id)` for each member, the ID read back from `sandbox get --output json` | `provision`, `deprovision` |
| `$AMAP_OPENSHELL_HOME/selected.json` | the router's discovery verdict (D2) | written once every member is recorded |
| `$AMAP_OPENSHELL_HOME/router.json` | the router's config, **generated**; never hand-edit | same |
| `$AMAP_OPENSHELL_HOME/router-state` | the router's private state, including each instance's first sight | same |
| `$AMAP_OPENSHELL_HOME/providers/amap-claude-code.json` | the provider profile with the image's real binary paths | `l1-kit profile` |
| `$AMAP_OPENSHELL_HOME/gateway-fragment.toml` | the gateway fragment, for reference | `l1-kit prepare` (`gateway-config` prints the same text) |

**Nothing is written inside a sandbox.** `$AMAP_OPENSHELL_HOME` must not be
inside, or contain, this repository or any sibling checkout.

## Bring-up

Every command runs from this repository's root, after `siblings --apply`. Set
`AMAP_OPENSHELL_HOME` and `CLAUDE_CODE_VERSION`, or pass `--home` and
`--claude-code-version`. The provider key is `ANTHROPIC_API_KEY`, or one line on
stdin with `--api-key-stdin`.

```sh
python3 amap-openshell.py --home "$AMAP_OPENSHELL_HOME" --fleet examples/fleet.json bring-up --claude-code-version "$CLAUDE_CODE_VERSION"
python3 amap-openshell.py --home "$AMAP_OPENSHELL_HOME" --fleet examples/fleet.json bring-up --claude-code-version "$CLAUDE_CODE_VERSION" --apply
```

The first is a dry run: it reads the host and prints each stage's plan, and
which stages it would skip and why. The second runs the lines below in order and
stops at the first that fails. Each stage skips what is done: an image labelled
with this version and uid:gid, an identical profile, a member that is present
and recorded, a running router. `bring-up` never deletes a sandbox, a container,
an image or a profile, never edits `gateway.toml` and never restarts the
gateway, and it refuses a router container that exists but is not running.
Preflight also refuses when the mounts interceptor is not accepting connections
on its socket. It names `examples/vms/proxmox/provision-guest.sh`, which on the
example VM does the `gateway.toml` merge and the gateway restart that
`bring-up` leaves alone. The
key is needed only to create a member, reaches `sandbox create` and nothing
else, and is never written or printed. The last line is `AMAP is up` only when
`verify` exits 0. Otherwise it names the stage that stopped it, and the exit
code is not 0.

### What it runs, one line at a time

Set the variables first (`AMAP_OPENSHELL_HOME`, the three sibling variables if
the checkouts are not beside this one, `CLAUDE_CODE_VERSION`, and
`ANTHROPIC_API_KEY`).

```sh
docker build --build-arg CLAUDE_CODE_VERSION="$CLAUDE_CODE_VERSION" --build-arg SANDBOX_UID="$(id -u)" --build-arg SANDBOX_GID="$(id -g)" -t amap-openshell-agent image/
python3 amap-openshell.py gateway-config
python3 amap-openshell.py install --apply
python3 amap-openshell.py l1-kit profile --home "$AMAP_OPENSHELL_HOME" --binary "<node path>" --binary "<claude path>" --apply
python3 amap-openshell.py provision alpha --apply
python3 amap-openshell.py provision beta --apply
"$AMAP_ROUTER_REPO/docker/build.sh"
"$AMAP_ROUTER_REPO/docker/run.sh" --config "$AMAP_OPENSHELL_HOME/router.json" --detach
python3 amap-openshell.py verify
```

`bring-up` also labels the image with the version and the uid:gid, so that it
can tell a later run to reuse it. The gateway merge (TUTORIAL step 4) stays by
hand.

The full walk-through, with the gateway merge, the provider import and the
experiments, is [docs/TUTORIAL.md](docs/TUTORIAL.md).

## Verbs

Host-wide options (`--home`, `--fleet`, `--image`, `--run-as`,
`--restart-policy`, `--openshell`, `--gateway-toml`) go **before** the verb.
Every writing verb is a dry run without `--apply`. `verify` reports PASS, FAIL
or **UNKNOWN**, and UNKNOWN is a problem, never a pass: it exits 1. `list` and
`router-config` exit 1 when something is not clean.

| Verb | What it does |
|---|---|
| `install` | write the payload, the `fleet.json` template and the roster directory; `router.json` once every member is recorded. A new `fleet.json` gets `fleet_domain` `openshell.<host>.<base>`; `--fleet-domain-base` sets the base |
| `provision <name>` | create the sandbox's lanes, policy and command, create the sandbox, record its ID in `membership.json` |
| `deprovision <name>` | delete the sandbox, empty its lanes and de-enrol it |
| `verify` | report PASS, FAIL or UNKNOWN, including the in-sandbox write attempt |
| `list` | list the fleet's sandboxes against `membership.json` |
| `router-config` | render the router's config; reports drift |
| `gateway-config` | print the gateway fragment and compare it with `gateway.toml` (read-only) |
| `teardown` | remove what `install` wrote |
| `bring-up` | build the image, check the gateway config, `install`, import the provider profile, `provision` every member, render the router config and start the router, then `verify`. A dry run without `--apply`, rerunnable; the last line is `AMAP is up` only when `verify` passed |
| `siblings` | clone or fetch the sibling checkouts beside this repository and check out the pins in `siblings.json`; amap-spec is cloned or fast-forwarded |
| `l1-kit` | proof-of-concept tooling: write the host-side files for L1 (`prepare`, `record`, `profile`). Also renders the provider profile used above |
| `l1-run` | proof-of-concept tooling: run `docs/L1-RUNBOOK.md` steps 0 to 9 on this host |

## Everyday tasks

- **A member joins.** Add it to `fleet.json`, then `install --apply`, then
  `provision <name> --apply`.
- **A member leaves.** `deprovision <name> --apply`, remove it from
  `fleet.json`, then `router-config --apply`. `deprovision` empties that
  member's lanes and removes its first-seen marker under `router-state`, so a
  recreated name is a **new instance** with a fresh first sight.
- **Who may task whom changes.** Edit `task_graph` in `fleet.json`, then
  `router-config --apply`. Never edit `router.json`.

## Rules that do not change

- **One name.** The OpenShell sandbox name is the router instance and the local
  part of the address (`<name>@<fleet_domain>`). `fleet_domain` is `openshell.<host>.internal` unless you chose otherwise (D21).
- **The identity is the pair (name, OpenShell ID).** A deleted and recreated
  sandbox is a different member.
- **The boundary is the read-only mount and the Landlock entry.** A write to a
  read-only lane gives `EROFS` (`docs/POC-REPORT.md`, Unknown 2).
- **Authorisation is the router's.** The roster lists who exists, never who may
  task whom.
- **Never create a lane or a sandbox directory by hand.** `provision` does.
  Dry run is the default.

## Why OpenShell

OpenShell contains the agent: kernel-level filesystem and process confinement,
a default-deny network and placeholder credentials. It deliberately leaves
agent-to-agent communication, per-agent identity and fleet governance out of
scope. AMAP covers exactly that gap, at the message level, with an open,
testable contract.

In one line: **OpenShell contains the agent; AMAP governs what it says and to
whom.**

## Files

| File | Contents |
|---|---|
| [docs/TUTORIAL.md](docs/TUTORIAL.md) | The operator's runbook: bring-up, verify, a first delegation, and "Break it on purpose" |
| [docs/L1-RUNBOOK.md](docs/L1-RUNBOOK.md) | The proof-of-concept procedure, with every OpenShell command cited |
| [docs/POC-REPORT.md](docs/POC-REPORT.md) | The result of the live proof of concept |
| [providers/README.md](providers/README.md) | The provider profile this deployment imports |
| [image/Dockerfile](image/Dockerfile) | The sandbox image recipe |
| [siblings.json](siblings.json) | The sibling checkouts and the commit each is pinned to (amap-spec is not pinned) |
| [examples/fleet.json](examples/fleet.json) | The example fleet policy: alpha may task beta |
| [examples/vms/proxmox](examples/vms/proxmox) | A guest-provisioning example for a Proxmox host |
| [DESIGN.md](DESIGN.md) | The five properties AMAP needs from a host, how sandy provides each, and the proposed OpenShell mechanism for each, with every assumption marked |
| [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) | The questions about OpenShell's code and docs that the design depended on, each answered in phase 0 |
| [FINDINGS.md](FINDINGS.md) | Phase 0 results: the answer status of each question, the design changes they caused, and whether phase 1 can go ahead |
| [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md) | Phases 1 and 2 as steps for a plan, implement and validate pipeline, with gates, live stops and acceptance criteria |
| [PLAN.md](PLAN.md) | Phased work: a manual proof of concept, then tooling, then optional hooks and Kubernetes support |
| [CLAUDE.md](CLAUDE.md) | Standing instructions for the agent that picks this up |

## Tests

```sh
python3 -m pytest tests -q
```

The suite checks against four read-only sources: `amap-router-local`
(`$AMAP_ROUTER_REPO`), `amap-connector-claude` (`$AMAP_CONNECTOR_REPO`),
`amap-deploy-sandy` (`$AMAP_SANDY_REPO`) and amap-spec (`$AMAP_SPEC_DIR`). A
missing source fails the run. It never passes as a smaller set of tests. The
suite never runs OpenShell, Docker or the router.
`python3 amap-openshell.py siblings --apply` sets them up beside this repository.

The gRPC tests of the interceptor run in its hash-locked virtualenv, not in the
suite above: `interceptor/tests/conftest.py` gives the command. CI's
`interceptor` job runs them.

## License

Apache License 2.0. See `LICENSE`, and `NOTICE` for the third-party and derived
material. [SECURITY.md](SECURITY.md) says how to report a vulnerability, and
[CONTRIBUTING.md](CONTRIBUTING.md) how to contribute.
