# Implementation plan

This plan turns PLAN.md's phases 1 and 2 into steps that an automated pipeline
can run: for each step, one agent **plans**, one **implements**, and one
**validates**. It builds on phase 0 ([FINDINGS.md](FINDINGS.md)). Where it and
PLAN.md differ on order, this plan is newer. PLAN.md stays the statement of
*what* each phase must achieve.

Step branches (`impl/*`) and any commit reference to this repository point to
its pre-publication history, which is kept privately. References to the
sibling repositories' commits are unaffected.

## How to run this plan

**Step kinds.** Each step has a kind:

- **`offline`**: code, tests and docs only. It needs no OpenShell, no
  Docker and no network. The pipeline runs these.
- **`live`**: needs a real OpenShell host (gate G3). **The pipeline stops at
  a live step** and hands it to the operator, or to an agent running on a
  qualifying host. Nothing after a live step starts until the operator
  records its result.
- **`gate`**: a precondition only the operator can satisfy.

**Section shape.** Every step is a level-3 heading `### <ID>: <title>`
followed by the same fields: Kind, Depends on, Read first, Deliverables,
Constraints, Acceptance criteria, Stop and report if. A workflow can split
this file on `### ` headings and pass one section, plus the "Rules for every
step" section, to each agent.

**The three roles:**

- **Planner.** Reads the step and its "Read first" list and writes a concrete
  plan: files, functions, tests, and the evidence each acceptance criterion
  will need. It resolves any open detail the step leaves to it and records the
  choice in the plan. It does not widen the step's scope.
- **Implementer.** Carries out the plan and commits to the step's branch. If it
  hits a "Stop and report" condition, it stops and reports, and does not
  improvise.
- **Validator.** Checks every acceptance criterion **with evidence**: the
  command it ran and the output, or the file and line. It runs the full test
  suite, not only the new tests. It checks that the diff touches only the
  step's deliverables, and it re-checks the rules below. It returns PASS, or
  FAIL with the criterion that failed. An acceptance criterion it could not
  check is a FAIL, never a PASS.
- **Repair (one round).** On a FAIL, the implementer gets the validator's
  report and fixes only the failing criteria, and a *fresh* validator checks
  the whole step again. A second FAIL stops the run for the operator.

**Branches.** One branch per step, `impl/<ID>-<slug>`, cut from the previous
step's branch. The operator merges.

**Batches.** One pipeline run per batch, each ending at a live gate:

- S1 → S2 → S3a;
- S3b → S4 → S5, then S5b, S5c, S5d and S5e (added by the operator), then L1;
- S6 → S7 → S8, then S7b, S8b and S8c (added by the operator), then L2, then S8d;
- S9 → S10.

A batch is 9 agents, and up to 15 with repair rounds. The operator chose
that scale (Decision 4, 2026-09-28). Before each batch, newer plan commits
on `implementation-plan` are merged into the chain of step branches.

## Rules for every step

These apply to every role in every step. A step that breaks one fails
validation.

1. **The repo's CLAUDE.md hard rules.** Don't modify amap-connector-claude,
   amap-router-local, amap-deploy-sandy or amap-spec. They are read-only
   siblings. Don't invent wire fields. Don't carry the spool over a network
   transport. Keep the DESIGN.md tags honest: `[verified]` requires a live
   run, cited.
2. **sandy's conventions carry over** (amap-deploy-sandy `CLAUDE.md`):
   - Python 3.9+, standard library only. Tests use pytest.
   - Every writing verb is a dry run without `--apply`.
   - `verify` has three outcomes: PASS, FAIL and UNKNOWN. UNKNOWN never counts
     as a pass.
   - Absent and empty are different answers.
   - No pass or fail totals in any file.
   - Comments describe the present. History goes in commit messages.
3. **Identifier-clean.** Shipped text has no absolute host paths, no personal
   names, and no real domains. Use `example.org`, and placeholders such as
   `$AMAP_OPENSHELL_HOME`.
4. **No credentials, and no network.** Don't install anything, and don't fetch
   anything. If a step seems to need either, stop and report it.
5. **Tests never run OpenShell, Docker or the router.** Offline tests stub the
   `openshell` and `docker` binaries with fakes, as sandy stubs `sandy`. A test
   that could reach a live binary is a defect.
6. **Missing siblings fail the test run.** If the router, the connector, the
   sandy checkout or the amap-spec mount is absent, the run fails loudly. It
   never passes as a smaller set of green tests.
7. **Spec conformance is reported, not asserted.** Any artifact governed by
   amap-spec is checked with `fixtures/validate.py`'s own checks, or its
   `schemas/`. The report names the schema and says which checks ran. See
   amap-spec's `CONFORMANCE.md`.
8. **Lane names and leaves come from the router.** Take them from
   `router.config.LANES` and `LANE_LEAVES` (amap-router-local), never
   from a literal. DESIGN.md §3's `inbound`/`outbound` names are
   illustrative. The router's names are `inbox`, `peer` and `outbox`.
9. **OpenShell facts are cited.** Any behaviour the code relies on cites the
   OpenShell file or doc page, in a comment or a test docstring, at a named
   revision.

## Decisions taken as defaults

The operator can overturn any of these before the pipeline starts. Each names
the step that depends on it.

| # | Decision | Default | Why | Affects |
|---|---|---|---|---|
| D1 | Share sandy's policy code, or copy it | **Import only the pure core of** `fleet_policy.py` from the sibling amap-deploy-sandy checkout, never editing it, behind a thin adapter here. The core is `PolicyError`, `load_policy`, `default_policy`, `resolve_peers`, `one_sided`, `resolve_task_graph`, `resolve_task_deny`, `transpose_task_graph`, `overlapping_pairs`, `address_for` and `router_address`, plus the constants `FLEET_DOMAIN_KEY`, `TASK_GRAPH_KEY`, `TASK_GRAPH_ALL`, `TASK_DENY_KEY`, `ALLOW_ANY`, `GROUP_SIGIL`, `ALL_GROUP` and `SCHEMA_VERSION`. Use nothing else from the module. **Don't import `policy_checks.py`**: its checks take sandy's `amap_sandy` module as `prov`, and `check_ratification` enforces sandy's recreation cadence. Call `resolve_peers`, `resolve_task_graph` and `overlapping_pairs` directly instead. Don't use `fleet_policy`'s repo-discovery helpers. `load_policy` accepts a bare policy, but it injects sandy's `sandboxes` and `agents` selection blocks, which this deployment ignores. Its errors mention sandy's manifest, so the adapter rewords them. | **Agreed with amap-deploy-sandy's operator**: this list is a stable interface, pinned on their side by `tests/test_shared_policy_surface.py` (PR #7, merged as `cfdf9f5`), and their CLAUDE.md names this repo as its consumer. Anything outside it can change without notice, and a change inside it will be announced first. There is no shared package for now. Two copies would drift (PLAN.md phase 2). **The second dependency is reduced to an agreed core (S7b).** `verify` loads amap-deploy-sandy's `router_health.py` through `router_health_link.py` for exactly eleven names, `PASS`, `FAIL`, `UNKNOWN`, `Verdict`, `Unresolved`, `Fact`, `Check`, `check`, `unknown`, `same_set` and `CannotRun`, which sandy pins in its PR #9 with the same advance-message promise. Nothing else of the module is read. The two router sections, the context, the section runner, the subprocess wrapper and the eleven fact derivations are this repository's own copy in `router_sections.py`, taken from `router_health.py` at `cd03903`. The missing-router remedy is `router_link`'s, so sandy's `fleet_policy` repo-discovery helpers are never reached. | S2, S7, S7b |
| D2 | How the router learns the instances | **Discovery mode**: `instances_dir` plus `selected_json`, where our tooling writes the verdict file from `membership.json` | amap-router-local's maintainers: an explicit `instances` map gets **no roster**, because `roster_dir()` is derived only from `selected_json`, and there is no config key for it (`router/roster.py`, `router/config.py:819-860`). The verdict is the router's generic host interface ("written by the host", `SELECTED_SCHEMA = 1`), not something only sandy may write. **Locked by the operator (Decision 2).** F1 is not raised. This is reopened only if amap-router-local says `selected_json` is private to sandy | S5, S6 |
| D3 | Compute driver | **Docker** first. Podman may work the same way, but it is untested and unsupported until run | Phase 0 read the Docker driver's code in full | S4, L1 |
| D4 | Sandbox image | This repo ships a **Dockerfile recipe**: non-root `USER`, `python3`, Claude Code. The operator builds it | OpenShell ships no Claude Code image. Its `bring-your-own-container` example is the model | S3b, L1 |
| D5 | Language for the `CreateSandbox` interceptor | **Python with `grpcio` and `protobuf`, confined to `interceptor/`** with its own pinned, hash-locked requirements. It uses stubs generated from OpenShell v0.1.2's protos, vendored and pinned. It is the one stated exception to the standard-library rule; everything outside `interceptor/` stays standard library only. A JWT is checked only if the gateway signs one, and a `unix://` socket with file permissions is the boundary otherwise | **Locked by the operator (2026-10-05), option B.** The rule enforces this repository's own layout, so it reuses `render.py` and `policy.py` and is tested in the same suite. That removes the likeliest failure, a rule that drifts from the mounts `provision` renders. Rust (OpenShell's example) was the alternative; converting the whole repository to Rust was considered and rejected, because D1 and the router-config check rest on importing Python siblings | S9, S10 |
| D6 | Gateway posture | A dedicated gateway with admission off, reachable on loopback only, one operator, no other sandboxes. Since D17 (2026-10-05) this is the **designed scope**, not a temporary accepted risk (DESIGN.md section 8) | **Locked by the operator (Decision 1).** The interceptor's rule depends on the lane layout L1 confirms | G3, L1, L2 |
| D7 | UID model | One shared uid:gid, the operator's own, for the router and every sandbox. It is rendered from one config value. F2 is not raised | **Locked by the operator (Decision 3).** Agents are separated by container and per-sandbox mounts, not by uid, and the router writes 0600 | S4, S5, L1 |
| D8 | The agents' permission mode | **Bypass permissions** (`--dangerously-skip-permissions`, or the pinned version's equivalent) for `claude` in every sandbox | **Locked by the operator (2026-09-29):** the sandbox is the safety boundary. It is the same model sandy runs under. Without it, an unattended delegation stalls on permission prompts | S5c, S5d, L1 |
| D9 | Claude Code's first-run prompts | **Pre-seed them.** Each first-run answer is written before `claude` starts: onboarding, workspace trust, the API-key confirmation and the bypass-mode confirmation. The key names come from what Claude Code writes itself, **observed, not documented**, on version 2.1.284. In `~/.claude.json`: `hasCompletedOnboarding`, `lastOnboardingVersion`, `projects[<dir>].hasTrustDialogAccepted` and `customApiKeyResponses.approved`. In settings: `skipDangerousModePermissionPrompt` | **Locked by the operator (2026-09-29), over a one-time manual pause.** Brittle across Claude Code versions by nature, so `CLAUDE_CODE_VERSION` is pinned, and L1 verifies that no prompt blocks | S5c, S5d, L1 |
| D10 | The model credential | **An Anthropic Console API key** (`ANTHROPIC_API_KEY`), sent as `x-api-key`. An OAuth token from `claude setup-token` is possible through a custom bearer profile, but it is unverified and deferred | **Locked by the operator (2026-09-29).** It is the path OpenShell documents (`providers/claude-code.yaml`; `docs/how-it-works/providers/overview.mdx:458`) | S5e, L1 |
| D11 | How the G3 VM gets this repository | **Push from the operator's machine to a bare repo on the VM.** The VM holds no GitHub credential: nothing to revoke, nothing to re-register per rebuild. `examples/vms/proxmox/README.md` has the commands | **Locked by the operator (2026-10-01).** It is the narrowest of the routes considered, and the VM is rebuilt rather than kept, so a credential on it would be re-created each time | L2, the L1 close-out |
| D12 | What the default branch holds | **`main` carries the work.** It fast-forwarded to the chain tip on 2026-10-01. The `impl/*` step branches stay as the reviewable record, and later fixes land on `main` or a short branch off it | **Locked by the operator (2026-10-01).** The default branch had stopped describing the repository, and D11 means naming a branch on every push to the VM | every step from here |
| D13 | Which sibling versions L2 runs on | **The shipping stack:** amap-router-local `9854a5e`, amap-connector-claude `37875a5`, amap-deploy-sandy `0ec0ce3`, amap-spec unpinned, with `AMAP_ROSTER_DIR` and `AMAP_SELF` exported so the connector's new `peers` tool works | **Locked by the operator (2026-10-02).** L2 proves step 10, where an agent chooses to delegate and reply, and `37875a5`'s reply guidance is part of that path. Proving it on instructions about to be replaced would spend L2 on the wrong version. Sandy's bump changes nothing we load and adds its interface-pin tests | S8b, L2 |
| D14 | How the siblings get set up | **A `siblings` command and one pins file** (`siblings.json`). The docs quote it and a docs test checks them. It refuses a checkout with local changes. It is a dry run without `--apply` | **Locked by the operator (2026-10-02), on the operator's suggestion.** The pins were held only in prose that nothing checked, and they change with D13. One checked source, reusable on every rebuild | S8b, L2 |
| D15 | How L2 brings the fleet up | **A `bring-up` verb** runs README "Bring-up" lines 1-8 as one dry-run-by-default, rerunnable command, and L2 runs through it. TUTORIAL steps 10 and 11 and the break-it experiments stay by hand. The operator's provisioning script calls it unattended, with the API key on stdin | **Locked by the operator (2026-10-03), option B.** It reverses the 2026-10-01 "L2 is run by hand" rule for steps 3-9: the operator wants the box scriptable end to end, with the key the only manual input. The cost, accepted: the tutorial's steps 3-9 lose their only human run | S8c, L2 |
| D16 | The router pin after L2 | **amap-router-local `e43dbba`**, one commit after D13's `9854a5e`. It changes only the reply path's logging, a finding from L2: a reply addressed only to its bound sender logs at DEBUG, and any other `to`/`cc` gives a WARNING with a count. The binding and the discard are unchanged. The connector and sandy pins stay at D13's | **Locked by the operator (2026-10-05).** It is the fix L2's finding asked for, and nothing this deployment checks reads that log line. Sandy's PR #24 is bumped separately once it merges | S8d's follow-up 5 |
| D17 | The deployment's scope | **One OpenShell host, one operator.** Every caller of the gateway acts for the operator. The repository protects agents from the host and from each other, not the host from its operator. DESIGN.md section 9 lists what widening needs: several operators, a fleet of hosts, or both. One GitHub issue points to that list | **Locked by the operator (2026-10-05).** It is the intended use. Under it, the interceptor guards against the operator's mistakes rather than other people, so the membership gate and the principal check go on the widening list, not into S10 | S9, S10, L3 |
| D18 | `grpcio-tools` | **Approved as a generation-only dependency**, hash-locked in `interceptor/requirements-gen.txt`. The run-time lock stays `{grpcio, protobuf}` | **Locked by the operator (2026-10-05).** Generated stubs keep an OpenShell bump mechanical and testable, which the repin watcher needs | S10 |
| D19 | A symlinked `$AMAP_OPENSHELL_HOME` | **Not supported.** The home must be its own real path, and `install` refuses otherwise, naming the real path | **Locked by the operator (2026-10-05).** The interceptor's R7(d) and `verify`'s `_real` already assume it. This makes it a stated requirement that fails early | S10 |
| D20 | CI and repins | **`tests.yml`** runs the suite on every push and PR, on Python 3.9 and 3.13. **`repin.yml`** runs daily and on demand. It opens or refreshes one PR per pinned sibling whose `main` moved, with the commit list and the suite's result, and **never merges**. The OpenShell release watcher is part of S10 | **Locked by the operator (2026-10-05).** A pin is the operator's decision, and the suite proves only the offline properties. The live checks need a host | every step |
| D21 | Fleet domain | **`openshell.<host>.<base>`**, base defaulting to `internal`, as amap-deploy-sandy's scheme (its PR #26) does for `sandy.<host>.<base>`. It follows amap-spec `spec/peer-origin.md:180-182`: a same-host fleet without mail may use a non-routable domain, and cross-host needs a routable one. It is derived once, at `install`, for a new fleet; an existing `fleet.json` is never rewritten. `install --fleet-domain-base` matches sandy's option. The derivation is **imported from sandy's `fleet_policy`** (requested of amap-deploy-sandy's maintainers 2026-10-05), not copied. The test host moves at its next rebuild, because every address and every sandbox's `AMAP_SELF` changes | **Locked by the operator (2026-10-05).** Agreed across runtimes, it gives a sandy fleet and an OpenShell fleet on one machine separate namespaces. It also settles DESIGN.md section 9's "addresses need a host or fleet qualifier". Importing keeps D1 | S11 |
| D22 | When S10 reaches main | **Merged before its deployment path exists.** Until S10b lands, `verify`, and so `bring-up`'s last stage, FAILs V1-V3 on any host without the interceptor. The test host is not rerun in the meantime. The operator rebuilds it from scratch once S10b, the sandy upgrade and S11 (D21's domain) are in | **Chosen by the operator (2026-10-05), option B.** The next live host is a fresh rebuild on the full stack, not a rerun of today's test host | S10, S10b, S11 |
| D23 | Our MCP registration vs sandy's `submit-server` | **A fifth, named difference.** Our `payload/mcp-servers.json` stays sandy's registration with the payload root swapped, except that `inbox-submit` runs the connector's own `bin/inbox-submit`, not sandy's wrapper `submit-server` (sandy #19, `c629e90`). The test names this difference, and any other drift still fails | **Locked by the operator (2026-10-05).** `submit-server` unsets `AMAP_SELF` and rebuilds it from `/etc/sandy-session.json`, which an OpenShell sandbox does not have. Shipping it would erase the `AMAP_SELF` our render exports, the path that worked live in L2 | S10c |
| D24 | The gateway's signed calls to the interceptor | **The registration opts out with `allow_insecure_transport = true`.** The interceptor still refuses a signed `Describe`, so removing the opt-out stops the gateway starting, and V1 FAILs without it. Verifying the token stays on the widening list | **Locked by the operator (2026-10-05).** The rebuild's first live run showed that the packaged gateway signs extension calls by default (`gateway-minted sandbox JWT enabled`). The interceptor refused the call, so under `fail_closed` the gateway would not start. Under D17 the boundary is the 0600 socket. The opt-out is OpenShell's documented mechanism, and the gateway logs it at every start | S10, S10b |
| D25 | Finding `$AMAP_OPENSHELL_HOME` | **The CLI remembers it.** `install --apply` and `provision-guest.sh --home DIR --apply` write the path to `${XDG_CONFIG_HOME:-$HOME/.config}/amap-openshell/home` (`home_record.py`). `amap-openshell.py` takes `--home`, then the variable, then the record. `teardown --apply` removes a record that names its home. Shell rc files are never touched | **Locked by the operator (2026-10-05), option A.** It works for every caller (interactive shells, plain `ssh`, cron) without writing into the dotfiles, which are shared across hosts and own the rc files | L2 rerun, every verb |

## Gates (the operator)

### G1: The repo has its own home

- **Kind:** gate
- **What:** move this staging copy to its own repository, as README.md says,
  and merge `phase-0`. The pipeline runs in that repository.
- **Done when:** the repository exists, `phase-0` is merged, and the README's
  "staging copy" note is updated.

### G2: A test environment

- **Kind:** gate
- **What:**
  - pytest is available to the pipeline's agents.
  - amap-router-local, amap-connector-claude and amap-deploy-sandy are checked
    out beside the repo, or named by `$AMAP_ROUTER_REPO`,
    `$AMAP_CONNECTOR_REPO` and `$AMAP_SANDY_REPO`.
  - The amap-spec mount is at `~/.amap-spec`.
- **Done when:** `python3 -m pytest --version` works, and all four sources can
  be read from the pipeline's environment.

### G3: A live OpenShell host

- **Kind:** gate. Needed only by the live steps.
- **What:**
  - Linux 6.2 or later (Landlock ABI 3), with Docker.
  - OpenShell `v0.1.2` or later, installed with the operator's approval, and
    configured as in DESIGN.md §6 (admission off).
  - **Operator's accepted posture (Decision 1)** for L1 and L2:
    - the gateway is dedicated to this work and reachable on loopback only;
    - it has no OIDC, and no other users hold its credentials;
    - no other sandboxes run on it.
    The accepted risk is recorded in `docs/POC-REPORT.md`. If the host is
    shared, the interceptor (S9, S10) moves ahead of L1.
  - A Console API key for the `claude-code` provider.
  - The router image built from amap-router-local.
- **Done when:** `openshell sandbox create --name probe -- true` succeeds on
  that host, and the operator records the host's OpenShell version.
- **Met 2026-09-29.** The probe succeeded on a Proxmox VM built from
  `examples/vms/proxmox`: Ubuntu 24.04, kernel `6.8.0-142-generic`, Docker
  Engine `29.8.1`, OpenShell `0.1.2`. OpenShell 0.1.2 has no
  `--restart-policy`, so `l1-kit prepare` runs without it. Two setup
  problems came up, and both are now documented:
  - the systemd user manager needed a reboot to pick up the docker group
    (`provision-guest.sh`);
  - the CLI needed `openshell gateway add ... --local --name openshell`
    (`docs/L1-RUNBOOK.md` step 2).

## Steps

### S1: Scaffold and test harness

- **Kind:** offline
- **Depends on:** G1, G2
- **Read first:**
  - amap-deploy-sandy: `CLAUDE.md`, `conftest.py`, `tests/_workspace.py`,
    `tests/test_sibling_checkouts_untouched.py` and `amap-sandy.py`.
  - This repo: `CLAUDE.md` and `DESIGN.md`.
- **Deliverables:**
  - `amap-openshell.py`, the launcher, and `amap_openshell.py`, the module,
    with an argparse skeleton. Every verb from PLAN.md phase 2 is registered
    and prints "not implemented" with exit 2.
  - `conftest.py`.
  - `tests/_workspace.py`, which finds the router, the connector, sandy and the
    amap-spec directory.
  - `tests/test_siblings_untouched.py`.
  - A "Tests" section in `CLAUDE.md`.
- **Constraints:**
  - Sibling discovery works like sandy's: walk up the directory tree, or use
    the environment variable, and confirm by a named file.
  - The harness installs a session-wide guard. Any `subprocess` call to
    `openshell`, `docker` or `podman` that no test has stubbed raises an
    error.
- **Acceptance criteria:**
  1. `python3 -m pytest tests -q` passes with every sibling present.
  2. With `$AMAP_ROUTER_REPO` pointing at an empty directory, and nothing
     beside the repo, the run **fails**. A test demonstrates this.
  3. A test proves the siblings' git trees are unchanged after the suite runs.
  4. A test proves that calling the real `openshell` binary without a stub
     raises.
- **Stop and report if:** a sibling cannot be imported without changing it.

### S2: Fleet policy and addresses

- **Kind:** offline
- **Depends on:** S1
- **Read first:**
  - amap-deploy-sandy: `fleet_policy.py`, and `examples/feature.json`. Read
    `policy_checks.py` only to see which calls to make directly (D1).
    Don't import it.
  - This repo: DESIGN.md P2 and P5.
  - amap-spec: `schemas/roster.schema.json` and `fixtures/validate.py`.
  - OpenShell `crates/openshell-server/src/grpc/validation.rs:241`, and
    `grpc/mod.rs:140` for the sandbox-name rules.
- **Deliverables:**
  - `policy.py`, an adapter over sandy's `fleet_policy` (D1). It loads
    `fleet.json`, a top-level policy with no sandy manifest wrapper.
  - `membership.py`, which reads and writes `membership.json` entries of
    `(workspace, name, id)`.
  - `examples/fleet.json` and the tests.
- **Constraints:**
  - A member name must be a DNS-1123 label of at most 19 characters. Anything
    else is refused with the reason.
  - A policy that spans more than one OpenShell workspace is refused.
  - Addresses come from `fleet_policy.address_for`.
- **Acceptance criteria:**
  1. Every member address produced for `examples/fleet.json` passes the
     roster address check, run through `fixtures/validate.py`'s own checking
     code, not a copied regex.
  2. A 20-character name is refused, and so are names with uppercase
     letters, underscores, or a leading or trailing `-`.
  3. A name recorded with a different ID is reported as a mismatch, not
     silently accepted.
  4. sandy's `fleet_policy.py` is imported, not copied. `grep` finds no copy of
     its functions in this repo.
- **Stop and report if:** `fleet_policy` cannot be used without its sandy
  manifest shape unless it is changed.

### S3a: The main-process wrapper

- **Kind:** offline
- **Depends on:** S1
- **Read first:**
  - amap-connector-claude: the `bin/inbox-delivery` docstring (the eight
    required `AMAP_DELIVERY_*` variables, plus `AMAP_DELIVERY_SELF`).
  - amap-deploy-sandy: `payload/relay`, `tests/_wrapper.py`, and the
    daemon-required-set test.
  - This repo: DESIGN.md P4 and §3.
  - OpenShell: `docs/how-it-works/sandboxes/overview.mdx` (main process,
    `--env`, restart policy).
- **Deliverables:**
  - `payload/amap-main`, a POSIX shell script. It is the sandbox's main
    process. It derives the eight `AMAP_DELIVERY_*` variables from
    `AMAP_INBOX_DIR`, `AMAP_PEER_DIR` and `AMAP_OUTBOX_DIR` (which `--env`
    sets), plus `AMAP_DELIVERY_SELF` from `AMAP_SELF_ADDRESS`. It runs
    `inbox-delivery` in a restart loop with capped backoff, then runs
    `claude` with the arguments it was given.
  - Its tests.
- **Constraints:**
  - The list of daemon variables is read from the connector's source at test
    time, never restated.
  - If a required input variable is missing, the wrapper exits nonzero and
    names the variable.
  - The daemon's restart loop never spins: a daemon that exits immediately is
    retried with growing delays.
  - When `claude` exits, the wrapper exits with its status. That exit is what
    OpenShell's restart policy acts on.
  - The daemon's private state lives under `$HOME`, as in sandy's relay.
  - The wrapper stops the daemon with SIGTERM, so the daemon releases its
    consumer claims. The daemon installs its handlers before it takes its
    claims (amap-connector-claude `bin/inbox-delivery`, `run`, at `d34ccbf`),
    and a build without that ordering can leak claims across restarts. The
    wrapper never deletes a claim file itself.
- **Acceptance criteria:**
  1. With a fake daemon and a fake `claude`, the environment the fake daemon
     receives equals the connector's required set plus the optional variable.
     The test derives both from the connector's source.
  2. Killing the fake daemon causes a restart. A daemon that always exits
     immediately produces growing delays, which the test observes.
  3. A missing `AMAP_PEER_DIR` exits nonzero and names the variable.
  4. The wrapper's exit status equals `claude`'s.
- **Stop and report if:** the daemon needs something the wrapper cannot
  supply without a connector change.

### S3b: Session lister, MCP config and image recipe

- **Kind:** offline
- **Depends on:** S3a
- **Read first:**
  - amap-deploy-sandy: `payload/handoff-sessions`,
    `tests/test_handoff_sessions.py` and `payload/mcp-servers.json`.
  - amap-connector-claude: `find_claude_targets` in `bin/inbox-delivery`, and
    `.mcp.json.example`.
  - OpenShell: `examples/bring-your-own-container/Dockerfile`, and
    `docs/how-it-works/sandboxes/runtimes.mdx` ("Sandbox User Identity").
- **Deliverables:**
  - `payload/openshell-sessions`. It prints at most one `claude` row,
    `agent pane_index pane_pid agent_pid socket keyfile`, with `-` for the
    pane fields. It finds `claude` among the descendants of the main process,
    using the same socket and key-file locations as sandy's lister.
  - `payload/mcp-servers.json`, with paths under the payload's mount target.
  - `image/Dockerfile` (D4).
  - The tests.
- **Constraints:**
  - The lister needs no tmux.
  - Row contract, confirmed by amap-connector-claude's maintainers:
    - The daemon splits rows on any whitespace and reads only fields 0, 4
      and 5. Unused fields are the literal `-`, never empty.
    - Socket and key-file paths must contain no whitespace.
    - With no `claude` running, the lister exits 0 and prints nothing. A
      nonzero exit means "helper failed" to the daemon.
    - The key file must be JSON with a non-empty string `peerToken`.
    - The daemon binds its receipt socket in the target socket's directory,
      so that directory must be writable by the sandbox user.
  - Two or more `claude` processes produce two rows, so the daemon reports
    `ambiguous_target`. The lister never picks one.
  - The Dockerfile sets a non-root `USER` and installs only `python3`, bash
    and Claude Code. It carries no credentials.
- **Acceptance criteria:**
  1. Tests against a fake `/proc` tree cover these cases: no `claude`; one
     `claude` with its socket; one without its socket (socket `-`); and two
     `claude` processes (two rows).
  4. With no `claude` running, the lister exits 0 with empty output. Every row
     has exactly six whitespace-separated fields.
  2. Every path in `mcp-servers.json` resolves under the payload mount target
     from DESIGN.md §3.
  3. The Dockerfile's final `USER` is not root, a test says so, and nothing in
     it names a registry credential.
- **Stop and report if:** Claude Code's socket or key-file location can't be
  established from sandy's lister and the connector alone.

### S4: Sandbox rendering

- **Kind:** offline
- **Depends on:** S2, S3b
- **Read first:**
  - This repo: DESIGN.md P1, P3, §3 and §6.
  - OpenShell:
    - `docs/how-it-works/sandboxes/runtimes.mdx` (Docker mounts);
    - `docs/how-it-works/policies/schema.mdx` (the `filesystem_policy`
      fields and `landlock.compatibility`);
    - `crates/openshell-core/src/container_paths.rs` (reserved roots);
    - `docs/how-it-works/providers/overview.mdx` (`--provider claude-code`).
  - amap-router-local: `router/config.py` (`LANES`, `LANE_LEAVES`,
    `handoff_dir` layout).
- **Deliverables:** pure functions that render, for one member:
  - the `openshell sandbox create` argument vector, with `--name`, `--from`,
    `--provider claude-code`, `--env`, `--driver-config-json` (bind mounts)
    and `--restart-policy` when the installed OpenShell supports it, then
    `-- <payload>/amap-main claude --mcp-config ... --append-system-prompt-file ...`;
  - the sandbox policy YAML;
  - the tests.
- **Constraints:**
  - Each mount's `read_only` flag and the policy's `read_only`/`read_write`
    lists are generated from **one** table, so they cannot disagree.
  - Policy compatibility is `hard_requirement`.
  - The policy's `read_only` and `read_write` lists are never both empty.
    If they are, OpenShell applies no Landlock policy layer at all
    (OpenShell `landlock.rs:241-242`).
  - No mount target is at or under any `read_write` path, including the
    workdir (`/sandbox`) and `/tmp`. Landlock grants write access beneath a
    `read_write` parent, which would leave only the `:ro` flag.
  - No mount target overlaps a reserved root.
  - Mount sources are only this member's `instances/<name>/` lanes, the
    payload and the roster.
  - Every sandbox runs as **one shared uid:gid**, the same one the router
    container runs as (`docker/run.sh` passes `--user "$(id -u):$(id -g)"`).
    It is rendered as `process.run_as_user` and `run_as_group` in every
    policy. The router creates agent-visible files 0600 and checks no
    ownership (amap-router-local `router/attachments.py:477`), so any
    mismatch fails silently as `EACCES`. Per-agent uids would need a router
    change (finding F2), so they are out of scope.
  - The functions write no files.
- **Acceptance criteria:**
  1. A property test over several members: every inbound lane, the payload
     and the roster are `read_only: true` in the mount **and** listed in
     `read_only`. The outbox is `read_only: false` in the mount **and**
     listed in `read_write`.
  2. No two members' renderings share a mount source.
  3. Every mount target avoids OpenShell's reserved roots. The test's list
     cites `container_paths.rs` at a named revision.
  4. The rendered policy has no network rules. Egress comes only from the
     provider.
  5. A test shows that no rendered mount target is at or under a rendered
     `read_write` path, and that the policy lists are non-empty.
  6. DESIGN.md §3's table uses the router's lane names.
  7. Every rendered policy sets the same `run_as_user` and `run_as_group`,
     taken from one configured value, and none of them is 0.
- **Stop and report if:** the policy schema has no way to express a path this
  design needs.

### S5: Router config

- **Kind:** offline
- **Depends on:** S2
- **Read first:**
  - amap-router-local: `router/config.py`, `router/provision.py`,
    `router/README.md` ("Layout") and `docker/run.sh`.
  - amap-deploy-sandy: `render_router_sibling`, `validate_with_router` and
    `sibling_diff` in `amap_sandy.py`.
- **Deliverables:**
  - `router.json` rendering from `fleet.json` in discovery mode (D2):
    `instances_dir`, `selected_json`, the task graph and `fleet_domain`.
  - The **verdict file** at `selected_json`, rendered from `membership.json`
    in the shape the router's `_discover_instances` reads.
  - Lane creation for one member: every lane root and every leaf from
    `router.config.LANES` and `LANE_LEAVES`, under
    `instances_dir/<name>/`.
  - A drift comparison.
  - The tests.
- **Constraints:**
  - The rendering is checked by the **router's own loader**
    (`router.config.load_obj`), as sandy does.
  - In discovery mode, the router's `provision` refuses to create lanes
    (`router/provision.py:154-200`). **Our tooling creates every lane and
    leaf, before the sandbox exists**: a bind mount needs its source, and
    the connector's daemon refuses to start if a leaf is missing
    (`provision.py:170-178`).
  - The verdict carries only what the router's loader accepts. The
    OpenShell ID stays in `membership.json`.
  - `state_dir` is not nested with `instances_dir` or `selected_json`
    (the router refuses that).
- **Acceptance criteria:**
  1. The router's loader accepts the rendering of `examples/fleet.json`. Its
     discovery report admits exactly the members in `membership.json`, with
     no `no_verdict` or `verdict_without_directory` entries.
  2. A changed task graph shows up as drift, and the drift names the key.
  3. After lane creation, every root and leaf the router lists exists, and
     the router's own discovery presence check passes on it.
  4. `roster.roster_dir()` for the rendered config is not `None`, and it lies
     outside every member's lanes.
- **Stop and report if:** the verdict format the router reads can't express
  this deployment's membership without a field the loader rejects. That is a
  finding for amap-router-local, not something to work around here.

### S5b: The L1 kit

- **Kind:** offline
- **Depends on:** S5
- **Why:** added by the operator on 2026-09-29. Without it, L1 would mean
  calling S4 and S5's Python functions by hand on the live host. This step
  wraps them in one command and one runbook, and adds no new rendering logic.
- **Read first:**
  - This repo: `render.py`, `router_config.py`, `policy.py`, `membership.py`,
    `payload/`, `image/Dockerfile`, `amap_openshell.py` and CLAUDE.md "Tests".
  - This plan: the L1 section, and DESIGN.md §2, §3 and §6.
  - PLAN.md phase 1.
  - amap-deploy-sandy: `payload_sources` and `install_feature_payload` in
    `amap_sandy.py`, and `docs/TUTORIAL.md`.
  - amap-router-local: `docker/run.sh` and `docker/build.sh`.
- **Deliverables:**
  - A verb, `l1-kit`, with two actions. Both are dry runs without `--apply`.
    - **`l1-kit prepare --home H --fleet F --run-as UID:GID --image I`.** It
      writes, under `H`:
      - the payload, copied byte for byte from the connector and this repo;
      - every member's lanes;
      - each member's policy YAML, and its `openshell sandbox create` command
        as a one-line shell file;
      - the §6 `gateway.toml` fragment, for reference.
    - **`l1-kit record --home H NAME ID`.** It records a created sandbox's
      OpenShell ID in `membership.json`. When every member in the fleet is
      recorded, it writes `selected.json` and `router.json`.
  - `docs/L1-RUNBOOK.md`: the L1 procedure as exact commands, in order.
  - The tests.
- **Runbook contents, in order:**
  1. Build the image (`image/Dockerfile`, with the build arguments from D4
     and D7).
  2. Configure the gateway (§6 and D6).
  3. Run `l1-kit prepare`.
  4. Create each sandbox with its rendered command.
  5. Read each ID with `openshell sandbox get --output json`, and run
     `l1-kit record` with it.
  6. Start the router with amap-router-local's `docker/run.sh --config`.
     The router starts last, so first sight follows the sandboxes.
  7. Run the alpha→beta delegation.
  8. Check each PLAN.md phase-1 pass criterion, and settle each L1 unknown.
     The unknowns include the refusing-receiver negative case and the
     shared-uid check.
  9. Fill in a `docs/POC-REPORT.md` template, which the runbook includes. It
     has a line for D6's accepted posture and the NOT-CHECKED mapping table.
- **Constraints:**
  - The step reuses S4 and S5's functions. It doesn't restate their rules.
  - The kit never runs `openshell` or `docker`. It writes commands for the
    operator to run.
  - It never invents an ID. `selected.json` is written only from recorded IDs.
  - The payload copy fails loudly if a connector source file is missing.
  - `INBOX-POLICY.md` is copied byte for byte from **this repo's**
    `payload/INBOX-POLICY.md`. That is the operator-approved text
    (2026-09-29): amap-deploy-sandy's version with exactly four
    substitutions. The roster path is `/opt/amap/roster/roster.json`, the
    address noun is `<sandbox name>`, the text says "find an agent" instead
    of "find a workspace", and identity is `$AMAP_SELF_ADDRESS`, not
    `/etc/sandy-session.json`. The kit and its tests never edit that text.
  - Every path the kit writes is under `--home`, and it refuses a `--home`
    that overlaps a sibling repo or this repo.
- **Acceptance criteria:**
  1. Without `--apply`, neither action writes anything. A test snapshots the
     tree before and after.
  2. `prepare --apply` into a temporary home:
     - the payload's bytes equal the sources';
     - every mount source named in every rendered create command exists;
     - every rendered policy passes S4's checks.
  3. After `record` of every member, the router's own loader accepts the
     written `router.json`, and discovery admits exactly the recorded
     members. Before the last member is recorded, no `selected.json` exists.
  4. Every `amap-openshell.py` invocation in the runbook parses against
     argparse, and every file path the runbook names is one the kit writes
     or one in this repo.
  5. The runbook names every PLAN.md phase-1 pass criterion, and every
     unknown in the L1 section. A test compares the lists, so neither can
     drift.
  6. A test diffs `payload/INBOX-POLICY.md` against amap-deploy-sandy's
     `payload/INBOX-POLICY.md` and allows exactly the four approved
     substitutions. A change to sandy's text then fails the test instead of
     drifting. The same test asserts that every `$AMAP_*` name the text
     uses is a rendered `--env` name, and that every `/opt/amap/...` path
     it names is at or under a read-only mount target.
- **Stop and report if:** sandy's `INBOX-POLICY.md` has changed, so the four
  approved substitutions no longer account for the difference. Adapting the
  text is the operator's call.

### S5c: Unattended Claude sessions

- **Kind:** offline
- **Depends on:** S5b
- **Why:** added by the operator on 2026-09-29, so L1 can run as a script
  (S5d). A sandbox's `claude` must come up and act on a delegation with no
  human at the keyboard. Claude Code's documentation
  (`https://code.claude.com/docs/en/cross-session-messaging`, read 2026-09-29)
  establishes four facts:
  - `crossSessionInbound` can be set from `--settings`, and project or local
    settings files can only tighten it;
  - an idle session starts a new turn when a message arrives;
  - the inbox socket's directory is not fixed (`/tmp/cc-socks-<uid>` is a
    fallback);
  - Claude Code exports the socket path and a token to hooks and Bash commands
    as `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN`.
- **Read first:**
  - this repo: `render.py`, `payload/amap-main`, `payload/openshell-sessions`,
    `payload/mcp-servers.json`, `image/Dockerfile`, `l1_kit.py` and
    `docs/L1-RUNBOOK.md`;
  - decisions D8 and D9;
  - amap-connector-claude's `bin/inbox-delivery` (`find_claude_targets`,
    `_read_token`, the reply-socket rules);
  - OpenShell `crates/openshell-cli/src/main.rs` (`sandbox create`: `--detach`,
    `--tty`, `--auto-providers`).
  - The installed `claude --help` output shows this environment's version's
    flags; cite it with its version.
- **Deliverables:**
  - **A settings file on the payload**, passed as `claude --settings <file>`
    by the wrapper's command line. It holds:
    - `crossSessionInbound: "accept"`;
    - `skipDangerousModePermissionPrompt: true`;
    - a `SessionStart` hook that runs a new payload script.
  - **That payload script** records the session's `CLAUDE_CODE_MESSAGING_SOCKET`
    and `CLAUDE_CODE_MESSAGING_TOKEN` where the lister reads them. The token
    goes into a key file in the connector's format (JSON, non-empty
    `peerToken`, mode 0600).
  - **`payload/openshell-sessions`** takes the socket and key file from that
    record, not from sandy's guessed locations. Its row contract (S3b) is
    unchanged.
  - **The wrapper (`amap-main`) seeds the first-run answers** before it runs
    `claude` (D9). It merges into `$HOME/.claude.json`, never replaces it, and
    never removes a key:
    - `hasCompletedOnboarding`, and `lastOnboardingVersion` set to the running
      Claude Code version;
    - the workdir's `hasTrustDialogAccepted`;
    - the last 20 characters of `$ANTHROPIC_API_KEY`, as the sandbox sees it,
      in `customApiKeyResponses.approved`. This is undocumented and flagged for
      L1. The full key is never written.
  - **The rendered claude command** adds bypass permissions (D8), using the
    pinned version's flag.
  - **The rendered create command** adds `--detach`, `--tty` and
    `--auto-providers`, so a script can create a sandbox without a terminal.
  - **`docs/L1-RUNBOOK.md`** and DESIGN.md are updated to match. Unknown 5
    now records that `--settings` pins `accept`, and what L1 must still
    confirm.
  - The tests.
- **Constraints:**
  - Only undocumented key names observed on a real install, cited to D9, are
    used.
  - The seed is a merge: a key already present keeps its value, except the
    appended approved-key suffix. A test proves nothing is clobbered.
  - The hook and the lister never log the token.
  - S3b's row contract and the connector's key-file contract hold. A test
    runs the connector's own `find_claude_targets` and `_read_token` on the
    new record.
  - S4's checks still pass on every rendered policy and command.
- **Acceptance criteria:**
  1. The rendered claude command carries `--settings <payload file>` and the
     bypass flag. The settings file parses, has exactly the listed keys, and
     its hook command resolves under the payload's mount target.
  2. With a fake `claude` that runs the `SessionStart` hook with fake
     `CLAUDE_CODE_MESSAGING_*` values, the lister prints one row. The
     connector's parser gets that socket, and `_read_token` gets that token.
  3. Seeding an existing `.claude.json` keeps every key and value it had, adds
     the listed keys, and puts only a 20-character suffix of the key in it. A
     test greps for the full fake key and doesn't find it.
  4. The rendered create command has `--detach`, `--tty` and
     `--auto-providers`, and still passes S4's checks.
  5. The runbook and the l1-kit tests still pass, updated for the new
     command.
- **Stop and report if:**
  - the installed `claude --help` has no `--settings`, or no
    bypass-permissions flag;
  - the connector's key-file contract can't be met from
    `CLAUDE_CODE_MESSAGING_TOKEN`.

### S5d: The L1 runner

- **Kind:** offline
- **Depends on:** S5c
- **Why:** the operator prefers scripts to manual steps (2026-09-29). This
  runs `docs/L1-RUNBOOK.md` steps 0–9 as one command. Unlike `l1-kit`, it runs
  `openshell`, `docker` and the router. That is the whole point of it, so it
  is a separate verb and the kit stays pure.
- **Read first:**
  - `docs/L1-RUNBOOK.md` in full, with every pass criterion and unknown;
  - `l1_kit.py`, `render.py`, `router_config.py`, and S5c's changes;
  - `examples/vms/proxmox/provision-guest.sh`, for the house style of an
    operator script (dry run, safe to rerun, evidence);
  - amap-connector-claude's `bin/inbox-submit` CLI;
  - amap-router-local's `docker/run.sh`;
  - amap-spec's `fixtures/validate.py`.
- **Deliverables:**
  - A verb, **`l1-run`**, with `--from N`, `--only N` and `--apply`. It is a
    dry run without `--apply`, which prints each command.
  - Each step reuses `l1-kit` for everything it renders:
    - **0.** Prerequisites: the variables, `ANTHROPIC_API_KEY` present (never
      printed), `CLAUDE_CODE_VERSION` set, and D6's posture.
    - **1.** Build the image.
    - **2.** Check the gateway.
    - **3.** `prepare`.
    - **4.** Create both sandboxes (detached), and wait until the lister shows
      a ready session in each (`sandbox exec`), with a timeout.
    - **5.** `record` both IDs from `sandbox get --output json`.
    - **6.** Build and start the router.
    - **7.** Plant alpha's delegation to beta through the connector's
      `inbox-submit` CLI inside alpha, the same drop-box the agent's tool
      writes. Wait for beta's `delivered` outcome, and for beta's reply
      notice in alpha's peer lane, with a timeout.
    - **8.** Every pass criterion and unknown, each an automated check or an
      explicit UNKNOWN with its reason.
    - **9.** Draft `POC-REPORT.md` from the evidence, into the evidence
      directory, for the operator to review and move into `docs/`.
  - The evidence directory `$AMAP_OPENSHELL_HOME/evidence/`, with one
    subdirectory per step: each command's argv, output, exit status and time.
  - A "Scripted run" section at the top of the runbook. The manual steps stay
    as the reference for what the runner does.
  - The tests.
- **Constraints:**
  - Every external command goes through one executor. It logs to evidence,
    and it redacts `ANTHROPIC_API_KEY`'s value and any
    `CLAUDE_CODE_MESSAGING_TOKEN`.
  - Steps 0–7 stop at the first FAIL. Step 8 runs every check. UNKNOWN is
    never a pass.
  - The runner refuses to go on if D6's posture fails.
  - It is safe to rerun: a step that is already done is detected and skipped,
    with a note. It never deletes a sandbox or a lane unasked.
  - Tests never run `openshell`, `docker` or the router: fakes only, under the
    S1 guard.
- **Acceptance criteria:**
  1. A dry run executes nothing (the fakes record no call) and writes nothing.
  2. `--apply` against fakes walks steps 0–9, writes evidence for each, and
     drafts the report.
  3. Every pass criterion and unknown in the runbook maps to exactly one
     runner check, or to an explicit UNKNOWN with a reason. A test compares
     the runbook's headings with the runner's table.
  4. With a fake key in the environment, no evidence file or report contains
     it or the fake token.
  5. `--from N` resumes, and a rerun after success changes nothing.
- **Stop and report if:** a pass criterion can't be checked without reading
  something inside the agent's reach, or without weakening D6.

### S5e: This deployment's provider profile

- **Kind:** offline
- **Depends on:** S5d
- **Why:** found on 2026-09-29, before the first live run. Step 4 creates
  each sandbox with `--provider claude-code`, and that would fail for two
  reasons the fakes couldn't show:
  1. OpenShell's `providers/claude-code.yaml` is an example that "OpenShell
     does not load"; it must be imported explicitly. Neither the kit nor the
     runner imports a profile.
  2. Its `binaries` list `/usr/bin/claude` and `/usr/local/bin/claude`.
     OpenShell matches the real path of the executable that opens a
     connection (`docs/how-it-works/policies/network-rules.mdx`, "Binary
     Matching"). The image installs Claude Code with npm, so that executable
     is `node`, and the API calls would be denied.
- **Read first:**
  - OpenShell at acbac9c:
    - `providers/claude-code.yaml` and `providers/README.md`;
    - `docs/how-it-works/providers/overview.mdx` and `profiles.mdx`;
    - `docs/how-it-works/policies/network-rules.mdx` ("Binary Matching");
    - `crates/openshell-cli/src/main.rs`, for the provider, profile and
      sandbox-create flags. Confirm each against the `v0.1.2` tag too; the
      live host runs 0.1.2.
  - This repo: `image/Dockerfile`, `render.py` (the `--provider` argument),
    `l1_kit.py`, `l1_run.py` and `docs/L1-RUNBOOK.md`.
  - Decision D10.
- **Deliverables:**
  - **A provider profile in this repo**, under `providers/`, adapted from
    OpenShell's `claude-code.yaml` under a distinct profile ID of this
    deployment's own:
    - the `ANTHROPIC_API_KEY` credential only (D10);
    - `api.anthropic.com` as the only endpoint. The telemetry endpoints are
      dropped, as the example's header allows;
    - `binaries` rendered from the image's real paths, never guessed.
  - **The image records its interpreter's real path.** After the build, the
    runner reads the real path of `node`, and of the `claude` entry point,
    from the built image. `docker run --rm <image> readlink -f` is enough.
    The runner renders the profile's `binaries` from those paths.
  - **The runner imports the profile before step 4:**
    - lint it (`openshell provider profile lint`);
    - import it, or update it when the imported copy differs;
    - let step 4's `--provider` create the provider from it.
    Every command goes through the executor, the key is never printed, and a
    rerun is a no-op.
  - **The rendered create command names this deployment's profile ID**
    instead of `claude-code`.
  - **If the pinned Claude Code documents a way to switch off its
    non-essential traffic**, set it in `payload/claude-settings.json`'s `env`
    and cite the docs. Otherwise record that there is none. Either way the
    endpoint list stays at `api.anthropic.com`.
  - **The runbook and the Scripted run section** gain the import step. The
    NOT-CHECKED and pass-criterion mappings are unchanged, unless a check
    moves.
  - The tests.
- **Constraints:**
  - The OpenShell CLI flags must exist in `v0.1.2`, not only at acbac9c. Cite
    both.
  - The profile file must pass OpenShell's documented schema:
    - required fields present;
    - the ID in lowercase kebab-case;
    - no reserved `v<digits>_` credential names.
  - Rendering stays pure. Only the runner runs `docker` or `openshell`.
  - Tests use fakes only, under the S1 guard.
- **Acceptance criteria:**
  1. The shipped profile parses, has the fields `profiles.mdx` requires, one
     credential (`ANTHROPIC_API_KEY`), and one endpoint (`api.anthropic.com`).
  2. Given a fake `docker run` that reports a node path, the rendered profile's
     `binaries` equal that path. With no path reported, the runner stops with
     FAIL, never a default.
  3. Against fakes, the runner lints, imports, and on a second run imports
     nothing. Every command appears in the evidence, and the fake key appears
     nowhere.
  4. The rendered create command's `--provider` is this deployment's profile
     ID, and S4's checks pass.
  5. Every OpenShell subcommand and flag the step adds is cited in `main.rs`
     at both acbac9c and v0.1.2.
- **Stop and report if:**
  - OpenShell 0.1.2's CLI cannot import a profile;
  - a provider can't be created from an imported profile with the key taken
    from the environment.

### L1: Proof of concept on a live host (PLAN.md phase 1)

- **Kind:** live. The pipeline stops here.
- **Depends on:** G3, S3a, S3b, S4, S5, S5b, S5c, S5d, S5e
- **Read first:** `docs/L1-RUNBOOK.md`, PLAN.md phase 1, DESIGN.md, FINDINGS.md and
  OPEN-QUESTIONS.md.
- **What:**
  1. Build the image, and render both members' create commands, policies and
     `router.json` with S4 and S5.
  2. Create the lanes, and start alpha and beta with the rendered commands.
     Start the router.
  3. Alpha tasks beta, and beta replies.
  4. Run the pass criteria in PLAN.md phase 1.
- **Settle these unknowns, and record the evidence for each:**
  - Router writes appear inside a running sandbox (Q1).
  - The errno of an agent's write to `inbox/` is `EROFS`, as expected from
    the OpenShell agent's reading of the kernel (Q1).
  - A lane that is mounted but missing from the policy is unreadable (Q1).
  - Injection works over `AF_UNIX` (Q5). **Also test a negative case**: set
    the receiver to refuse, send one notice, and confirm the outcome is
    `refused` or `held`, not `delivered`. A socket directory the receiver
    doesn't recognise produces no receipt, and the daemon counts silence as
    `delivered` (amap-connector-claude's maintainers).
  - How Claude Code's cross-session receiver setting is fixed to `accept` in a
    place the agent cannot write (Q4). Read sandy's
    `verify_cross_session_inbound` for the two files involved.
  - The accepted gateway posture (Decision 1) is written into the report.
  - The shared uid holds end to end. The router (`--user`), the lane
    directories and every sandbox's `run_as_user` are the same uid, and the
    agent can read a delivered 0600 notice.
- **Deliverables:**
  - `docs/POC-REPORT.md`, with each pass criterion's evidence and the
    NOT-CHECKED mapping table from PLAN.md phase 1.
  - DESIGN.md tags moved to `[verified]` with citations, only where the run
    showed it.
- **Close-out: remove the L1 host's GitHub credentials.** Once L1 works,
  the L1 host's credentials are removed, and later updates are delivered by
  pushing from the operator's machine (D11). A rebuilt VM holds no GitHub
  credential.
- **Ran 2026-09-30, and every check passed.** `l1-run --apply` walked steps
  0 to 9 on the G3 VM: OpenShell 0.1.2, Docker 29.8.1, kernel
  6.8.0-142-generic, Claude Code 2.1.284. One agent tasked the other, the
  receiver did the work and replied, and the reply reached the sender. All
  five pass criteria and all seven unknowns are PASS; `docs/POC-REPORT.md`
  holds the evidence. The runner left Pass criterion 1 UNKNOWN over document
  classes with no live document; the report settles it (`directory` and
  `binding-record` are emitted by nothing here, and `deliver-notice` needs a
  mail-carrying fleet). DESIGN.md's claims are now `[verified]`.
- **The operator accepted the report on 2026-09-30.** L1's outcome stands as
  `docs/POC-REPORT.md` records it.
- **Closed out 2026-10-01, so L1 is done.** On the L1 host, `deprovision`
  ran for both members and `teardown` ran after the router was stopped.
  That was the first live run of both verbs; `docs/POC-REPORT.md` records
  it. Then the L1 host's credentials were removed, and the VM was returned
  to the operator's inventory for rebuild.
- **Done when:** the operator accepts the report and the close-out is done.
  Any failure feeds back into S3a–S5 before S6 starts.

### S6: Operator verbs

- **Kind:** offline
- **Depends on:** L1
- **Read first:**
  - amap-deploy-sandy: `README.md` (Verbs), and `run_provision` and
    `run_teardown` in `amap_sandy.py`.
  - This repo: `docs/POC-REPORT.md`.
- **Deliverables:** these verbs:
  - `install` (payload copied byte for byte from the connector and this repo,
    the `fleet.json` template, the roster directory and `router.json`);
  - `provision <name>` and `deprovision <name>` (lanes, the create command,
    and the membership record including the ID read back from
    `openshell sandbox get --output json`);
  - `list`, `router-config`, `gateway-config` (prints the §6 fragment and
    compares it with an existing `gateway.toml`, read-only), and `teardown`;
  - the tests, with a fake `openshell`.
- **Constraints:**
  - Every verb is a dry run without `--apply`.
  - `deprovision` empties that member's lanes. A recreated name is a new
    instance, so it gets a fresh first sight at the router.
  - Nothing is written inside a sandbox.
- **Acceptance criteria:**
  1. Every verb without `--apply` writes nothing. A test snapshots the file
     tree before and after.
  2. `provision --apply` records the ID the fake `openshell` returns.
     Reprovisioning when the fake returns a new ID refuses until the operator
     deprovisions.
  3. The installed payload's bytes equal the sources'.
  4. `gateway-config` reports each of the three settings as present, absent or
     unreadable. Unreadable is UNKNOWN.
- **Stop and report if:** L1 showed a behaviour that contradicts S4 or S5.

### S7: verify

- **Kind:** offline
- **Depends on:** S6
- **Read first:**
  - amap-deploy-sandy: `router_health.py` (Verdict, Unresolved, Check),
    `tests/test_no_false_green.py` and `tests/test_no_hardcoded_expectations.py`.
  - This repo: DESIGN.md §5 and `docs/POC-REPORT.md`.
- **Deliverables:** `verify`, with PASS, FAIL and UNKNOWN, and the tests. It
  runs these checks:
  - the install;
  - `router.json` drift;
  - the gateway configuration;
  - for each member:
    - it exists, and `(workspace, name, id)` matches the membership record;
    - the effective policy lists the lanes correctly and has no mail egress;
    - the container's mount table shows `ro` and `rw` as rendered, from the
      member's own sources only, with no source shared across the fleet;
    - an in-sandbox write to `inbox/` fails, with the errno L1 recorded;
    - `inbox-delivery` is running;
  - the router's checks: reuse sandy's `router_health` where it doesn't read
    sandy's facts, otherwise port them (D1).
- **Constraints:**
  - The same discipline as sandy's: UNKNOWN is a problem, and absence is
    decided by a command that succeeded and listed nothing.
  - No OpenShell log line is expected for a filesystem denial (FINDINGS.md).
  - The write attempt uses a scratch filename and cleans up after itself,
    whatever the outcome.
- **Acceptance criteria:**
  1. With every fake reporting healthy state, `verify` exits 0.
  2. For each of these, `verify` reports FAIL naming the member, and exits 1:
     - a `rw` inbox mount;
     - a lane missing from the policy;
     - an ID mismatch;
     - a mount source shared by two members;
     - a successful in-sandbox write.
  3. A missing or failing `openshell` or `docker` produces UNKNOWN, never
     PASS, and `verify` exits 1.
  4. The no-false-green and literal-budget tests, ported from sandy, pass.
- **Stop and report if:** a check can only be made by reading something
  inside the agent's reach.

### S7b: Own the router sections

- **Kind:** offline
- **Depends on:** S7
- **Why:** added by the operator on 2026-10-01. amap-deploy-sandy agreed to
  keep only `router_health`'s three-outcome core stable: `PASS`, `FAIL`,
  `UNKNOWN`, `Verdict`, `Unresolved`, `Fact`, `Check`, `check`, `unknown`,
  `same_set` and `CannotRun`. That core is pinned in their PR #9, with the
  advance-message promise. Everything else S7 reuses stays internal to
  sandy, and they will change it without notice:
  - `Ctx`, `Section`, `Outcome`, `run_sections`, `problem_lines`, `run` and
    `Proc`;
  - `FACT_SOURCES` and its keys, and `WIRE_NAMES`;
  - `verify_container` and `verify_health`, with `SECTION_*`,
    `CONTAINER_RUNNING` and `HEALTH_STATUS`.

  Two problems follow:
  1. `verify` stays correct only by overriding `Ctx` internals (`fact()`,
     `_facts`, the constructor) so that sandy's derivations read this
     deployment's `router.json`.
  2. A latent D1 violation. When the router's status can't be read **and**
     the amap-router-local checkout can't be found, sandy's `verify_health`
     builds its remedy through `fleet_policy_mod()`. That puts sandy's
     checkout on `sys.path`, imports `fleet_policy` by name, and calls
     `router_not_found_message`, a helper D1 excludes. From then on,
     `amap_sandy` is importable in this process (`router_health.py:319-337`
     and `:953-962` at `cd03903`).
- **Read first:**
  - this repo: `verify.py`, `router_health_link.py`,
    `tests/test_verify.py`, `tests/test_router_health_link.py`,
    `tests/test_no_false_green.py`, `tests/test_no_hardcoded_expectations.py`,
    `router_link.py` and decision D1;
  - amap-deploy-sandy at `cd03903`: `router_health.py`, the whole file, read
    to copy from, never imported beyond the core.
- **Deliverables:**
  - **A module of this repo's own holding the two router sections**,
    adapted to this deployment's facts:
    - `verify_container` and `verify_health`, with every constant, claim
      string and helper they use;
    - the supporting machinery `verify` needs, now owned here: a context
      class with a supported way to supply facts, plus sections, outcomes,
      the section runner, the problem lines and the subprocess wrapper;
    - the fact derivations S7 took by reference (`router_doc`,
      `config_instances`, `state_dir`, `selected_json`, `status_doc`,
      `announced_interval`, `admitted`, `max_poll_age`, `true`, `zero` and
      `empty`).
    Its header names its origin (amap-deploy-sandy `router_health.py` at
    `cd03903`) and which functions were copied, and each copied claim keeps
    its docstring's account of where it comes from.
  - **`router_health_link.py` reduced to the agreed core.** `USED_NAMES` is
    exactly the eleven names, and `GENERIC_FACTS` is gone.
  - **The missing-router remedy is this repo's own:** `router_link`'s
    message, never sandy's `router_not_found_message`.
  - The tests, and decision D1 updated.
- **Constraints:**
  - `verify`'s outcome on every existing test case is unchanged. This step
    changes where code lives, not what it reports.
  - Nothing reads, imports or calls any name of sandy's outside the eleven.
    Nothing puts any checkout on `sys.path`.
  - The sandy conventions still hold: three outcomes, UNKNOWN never a pass,
    absent is not empty, and the literal budget.
  - Python 3.9, standard library only. amap-deploy-sandy is Apache-2.0, like
    this repo, so the copy keeps its attribution in the header.
- **Acceptance criteria:**
  1. `router_health_link.USED_NAMES` is exactly the eleven agreed names. A
     test proves `verify`, across every section, reads no other attribute of
     the loaded module; for example, wrap it in a proxy that records
     attribute access.
  2. A test runs `verify` with the router's status unreadable **and** the
     router checkout missing. It asserts UNKNOWN with this repo's remedy.
     It asserts that `sys.path` is unchanged before and after, and that
     neither `fleet_policy` nor `amap_sandy` appears in `sys.modules`.
  3. Every test that existed before passes unchanged in expectation, and the
     full suite passes.
  4. The copied module's header names amap-deploy-sandy, `router_health.py`,
     `cd03903` and the functions copied.
  5. D1 records the reduced dependency: only the agreed core, pinned by
     sandy's PR #9.
- **Stop and report if:** a section can't be owned without changing what
  `verify` reports on a healthy host.

### S8: Operator docs

- **Kind:** offline
- **Depends on:** S7
- **Read first:** amap-deploy-sandy `README.md`, `docs/TUTORIAL.md` and
  `tests/test_docs_agree.py`, and this repo's `docs/POC-REPORT.md`.
- **Deliverables:**
  - `README.md`, the operator's map.
  - `docs/TUTORIAL.md`, a runbook mirroring sandy's, including "Break it on
    purpose" with the OpenShell-specific experiments.
  - `tests/test_docs_agree.py`.
- **Constraints:**
  - Every command in the docs exists and takes the flags shown.
  - The docs keep the "staging copy" and "unsupported until verified"
    wording until L2 passes.
- **Acceptance criteria:**
  1. The docs-agree test parses every `amap-openshell.py` invocation in the
     docs against argparse.
  2. No identifier leak. A grep for absolute paths and the operator's
     identifiers finds nothing.
- **Stop and report if:** a documented step has no verb or check behind it.

### S8b: Before L2 — the token fix, the siblings command, the new pins

- **Kind:** offline
- **Depends on:** S8, S7b
- **Why:** three things found or decided on 2026-10-02 must land before L2.
  1. **The daemon authenticates with the wrong credential.** S5c's
     SessionStart hook writes `CLAUDE_CODE_MESSAGING_TOKEN` into a record as
     `peerToken`, and that record is the key file the lister hands
     `inbox-delivery`. Claude Code's cross-session-messaging docs make that
     token the proof that a connection comes from the session's **own child**.
     So the daemon presents a peer's delegation as the session's own output,
     which the connector's ancestry guard exists to prevent and cannot see.
     Today it is masked, because `--settings` pins `crossSessionInbound` to
     `accept`; it becomes a bypass if that value is ever absent.
     (amap-connector-claude's maintainers; its CLAUDE.md now says never send that
     token.)
  2. **The tutorial's step 2 is broken for L2.** It clones this repository,
     private at the time, from GitHub, which fails on a host with no GitHub credential
     (D11). It clones the siblings unpinned, and it never clones amap-spec,
     which `l1-run --only 8` needs.
  3. **D13 and D14**, above.
- **Read first:**
  - this repo: `payload/claude-session-start`, `payload/openshell-sessions`,
    `payload/claude-settings.json`, `render.py` (`env_pairs`, `SELF_ENV`),
    `amap_openshell.py`, `docs/TUTORIAL.md` (step 2),
    `examples/vms/proxmox/README.md` (the clone block), `README.md`, and the
    tests for each;
  - amap-connector-claude at `37875a5`: `bin/inbox-delivery`
    (`find_claude_targets`, `_read_token`, the ancestry guard), `bin/inbox-submit`
    (the `peers` tool, `AMAP_ROSTER_DIR`, `AMAP_SELF`), and the README's "What
    the host must provide";
  - Claude Code's cross-session-messaging docs, "The session's inbox socket"
    (`https://code.claude.com/docs/en/cross-session-messaging`).
- **Deliverables:**
  1. **The token fix.**
     - The hook records the socket (`CLAUDE_CODE_MESSAGING_SOCKET`), the pid
       and the start time, and nothing else. `CLAUDE_CODE_MESSAGING_TOKEN` is
       never written anywhere.
     - The lister's keyfile column is the session's own peer key,
       `~/.claude/sessions/<pid>.*.key`, validated as JSON with a non-empty
       string `peerToken`. It is never a file built from the hook's
       environment. The socket still comes from the record.
     - DESIGN.md, the runbook and the tutorial stop describing the record as
       the key file.
  2. **`siblings.json`** at the repository root: each sibling's name, its
     clone URL and its pinned full commit, with amap-spec unpinned. The pins:
     - amap-router-local `9854a5e8e932ef2989237de7b0a15ac322f38e5a`;
     - amap-connector-claude `37875a5ddd55a266a1545c5a03c8ce3bf3018b9e`;
     - amap-deploy-sandy `0ec0ce37c4acc3feeb338b17f77950f229d1eddf`.
  3. **`amap-openshell.py siblings [--apply]`.**
     - It clones each sibling beside this repository, or fetches if it is
       already there, and checks out its pin. amap-spec is cloned, or
       fast-forwarded, and its commit is reported.
     - It refuses a checkout with local changes, naming it.
     - Without `--apply` it changes nothing and prints what it would do.
     - It must work before sandy exists, so it imports neither `policy` nor
       anything that loads sandy.
  4. **The exports.** The rendered `--env` adds
     `AMAP_ROSTER_DIR=/opt/amap/roster`, which is S4's roster target, and
     `AMAP_SELF=<the member's address>`, beside the existing
     `AMAP_SELF_ADDRESS`.
  5. **The docs.**
     - Tutorial step 2 runs `siblings`, and says that this repository itself
       arrives by the operator's push while it was private
       (`examples/vms/proxmox/README.md`, D11).
     - The Proxmox README's clone block becomes the same command.
     - A docs test asserts that every pin quoted anywhere in the docs equals
       `siblings.json`.
  6. The tests.
- **Constraints:**
  - No test sends, stores or builds a file from a messaging token, beyond
    the one fake value the hook is given to prove it is never written. Build
    no "with token" variant: the connector's maintainers asked us not to, and the
    docs already settle it.
  - Tests never reach the network. `git` is faked on PATH, like `openshell`
    and `docker`.
  - Every existing behaviour is unchanged except the keyfile column and the
    new exports. S4's checks still pass on every rendered command and policy.
- **Acceptance criteria:**
  1. The real hook runs with a fake `CLAUDE_CODE_MESSAGING_TOKEN`. That
     value then appears in no file under the fake home or the record
     directory, and in no lister output.
  2. For a session with a peer key, the lister's keyfile column is that key,
     and the connector's own `_read_token` (at `37875a5`) returns the peer
     key's `peerToken`. Without a valid peer key, the column is `-`.
  3. `siblings` without `--apply` makes no change: the fake `git` records
     only read-only calls, and no file is written. With `--apply`, against a
     fake remote, it leaves every sibling at its pin. It refuses a dirty
     checkout, naming it. It runs with no sandy checkout present.
  4. The rendered create command carries `AMAP_ROSTER_DIR=/opt/amap/roster`
     and `AMAP_SELF=<address>`, and S4's checks pass.
  5. The docs test passes, and tutorial step 2 runs `siblings`.
  6. The full suite passes with the siblings at the new pins.
- **Stop and report if:**
  - a session's peer key cannot be identified unambiguously from its pid;
  - connector `37875a5` needs something from the host that this deployment
    cannot provide.

### S8c: The `bring-up` verb (D15)

- **Kind:** offline
- **Depends on:** S8b
- **Why:** D15. The operator's unattended provisioning script
  runs it as the `amap` user from this repository's root after
  `provision-guest.sh` and `siblings --apply`. It is the only manual input
  besides the key. The interface below is what that script already calls, so
  it is fixed:

  ```sh
  python3 amap-openshell.py --home <dir> --fleet examples/fleet.json \
    bring-up --claude-code-version <ver> [--api-key-stdin] --apply
  ```

  The script detects the verb with `--help | grep -w bring-up`, passes
  `--api-key-stdin` only when the operator typed a key (the key is then the
  only thing on stdin, one line), and treats any exit but 0 as a failure.
- **Read first:** `amap_openshell.py`, `verbs.py` (`run_install`,
  `run_provision`, `run_router_config`, `run_gateway_config`), `l1_run.py`
  (`step_1`'s image reuse by label, `import_profile`, `cmd_image_paths`,
  `router_state`, `step_6`), `verify.py`, `openshell_cli.py` (how the key
  reaches `sandbox create` only), `provider_profile.py`, `README.md`
  ("Bring-up"), `docs/TUTORIAL.md`, and the tests for each. Reuse these;
  do not write a second copy of any of them.
- **Deliverables:**
  1. **`bring-up [--claude-code-version V] [--api-key-stdin] [--apply]`**,
     using the host-wide `--home`, `--fleet`, `--image`, `--run-as`,
     `--openshell` and `--gateway-toml`. The version defaults to
     `$CLAUDE_CODE_VERSION`; with neither, it refuses (there is no default
     version). In order, each stage idempotent:
     1. build the image with `SANDBOX_UID`/`SANDBOX_GID` from the run-as
        uid:gid (D7), reusing an image whose labels already match this
        version and run-as, as `l1-run` step 1 does;
     2. check the gateway config (`gateway-config`'s comparison), and
        refuse to go on if it is not clean;
     3. `install`;
     4. render the provider profile from the node and claude paths read
        from inside the built image, and import it, or update the gateway's
        copy when it differs, as `import_profile` does;
     5. `provision` every member of the fleet, leaving members already
        provisioned and recorded alone;
     6. render the router config, then build and start the router unless
        a container of that name is already running. A container that exists
        but is not running is refused, never removed;
     7. `verify`. Its exit code is `bring-up`'s.
  2. **The key.**
     - It is needed only when a member must be created, because only
       `sandbox create --auto-providers` reads it (D10).
     - With `--api-key-stdin`, it reads one line from stdin; otherwise it
       takes `$ANTHROPIC_API_KEY`.
     - It is passed only in the environment of the `sandbox create` call.
       It is never written to disk, logged or printed, including in the dry
       run and in error messages.
     - When no member needs creating and no key was given, it carries on.
       When a member needs creating and there is no key, `--apply` refuses
       before changing anything.
  3. **Output.**
     - Without `--apply` it changes nothing. It prints each stage's plan,
       which stages would be skipped, and why.
     - With `--apply` it prints each stage's outcome.
     - The last line is exactly `AMAP is up` if and only if `verify` passed
       (exit 0). Otherwise the last line names the stage that stopped it.
  4. **Docs.**
     - README "Bring-up" leads with the one command and keeps the
       individual lines as what it does.
     - The tutorial says which of its steps `bring-up` replaces, and that
       steps 10 and 11 and the break-it experiments stay by hand.
     - The verbs table lists `bring-up`.
  5. The tests.
- **Constraints:**
  - It does not run TUTORIAL step 10, any experiment, or `l1-run`.
  - Nothing it does is destructive: it never deletes a sandbox, a
    container, an image or a profile. It never restarts the gateway, and it
    never edits `gateway.toml`.
  - Every other verb's behaviour is unchanged.
  - The tests fake `openshell`, `docker`, the router's scripts and stdin.
    No test uses a real key, and a test proves that a fake key's value
    appears in no file under the home, no output and no argv.
- **Acceptance criteria:**
  1. `--help` lists `bring-up`, and the verb accepts exactly the interface
     above.
  2. The dry run makes no change: the fakes record only read-only calls, and
     no file is written under the home.
  3. On a fresh fake host, `--apply` runs the seven stages in order and ends
     `AMAP is up` with exit 0 when the faked `verify` passes. When a stage
     fails it stops there, exits non-zero, and does not print `AMAP is up`.
  4. Rerun on that host, it builds nothing, creates nothing, imports
     nothing, starts nothing, and ends with `verify`.
  5. The key rules hold:
     - `--api-key-stdin` reaches only the create call's environment;
     - a missing key with a member to create refuses before any change;
     - a missing key with nothing to create carries on;
     - the fake key's value appears nowhere it must not.
  6. A non-zero `verify` makes `bring-up` exit non-zero.
  7. The full suite passes.
- **Stop and report if:**
  - an existing verb would need a behaviour change, other than being
    factored for reuse, to be called from `bring-up`;
  - the key would have to reach any process other than `sandbox create`.

### L2: A full live run with the tooling

- **Kind:** live. The pipeline stops here.
- **Depends on:** G3, S8, S8b, S8c
- **The fresh gateway is a rebuilt VM (operator, 2026-10-01).** Not a
  snapshot rollback: destroying the VM and running
  `examples/vms/proxmox/create-vm.sh` and `provision-guest.sh --apply`
  again is the only thing that proves that G3 automation, which has never
  run for real in its current form. Before destroying the outgoing VM, run
  `deprovision` on each member and then `teardown`, so those verbs are
  exercised live on state that is about to go; the tutorial has no teardown
  step, so nothing else covers them. Record the kernel and Docker versions
  the rebuild lands on: they may be newer than L1's.
- **Bring-up is scripted; step 10 stays by hand (D15, operator,
  2026-10-03).** This replaces the 2026-10-01 rule that L2 is run by hand
  throughout. `bring-up --apply` (S8c) does TUTORIAL steps 3-9. It may be run
  by the operator's unattended bootstrap, and it must end `AMAP is up`.
  Step 10 is still the operator asking alpha, in English, to find beta and delegate. That is
  the first test of the agent *choosing* to use `inbox-submit`: `l1-run`
  planted its request through the CLI, so L1 never tested it. Steps 10 and
  11 and the break-it experiments are by hand. Afterwards,
  `l1-run --only 8 --apply` re-checks the same properties against the
  verb-provisioned fleet, at no extra cost. The cost the operator accepted:
  no human walks the tutorial's steps 3-9 in L2.
- **What to watch for at step 10 (from amap-connector-claude's maintainers,
  2026-10-02).** S8b's lister names a session's peer key only when exactly
  one valid candidate exists. Otherwise it writes `-`, and then every
  delegation's outcome is `inject_failed`, with the detail "key file -
  unreadable or has no peerToken", retried with backoff. A stream of those
  means the key search under `~/.claude/sessions/` found zero or several
  keys. The connector's live probes (Claude Code 2.1.278) also say:
  - no receipt means delivered;
  - a message arriving mid-turn is folded into that turn, and one arriving
    while idle starts a new turn;
  - `refuse` comes back as `held`, with "receiver refuses cross-session
    messages". Our pin, `37875a5`, has the same `bin/` and `tests/` as the
    connector's main; only the README has moved on.
- **What:**
  1. Follow `docs/TUTORIAL.md` exactly on a fresh gateway.
  2. `verify` exits 0.
  3. Each "break it" experiment produces the FAIL or UNKNOWN the tutorial
     predicts.
- **Deliverables:** an addendum to `docs/POC-REPORT.md`, and any tutorial fixes
  (fixes go back through S8).
- **Done when:** the operator accepts the addendum.
- **Done: run 2026-10-04, and the operator accepted the addendum the same day.**
  `docs/POC-REPORT.md`, "L2", has the record. In brief:
  - `bring-up` ended `AMAP is up`, and a rerun skipped every stage.
  - Step 10 passed both ways, from the sessions and the router's audit log.
  - The six experiments behaved as the tutorial predicts, now corrected
    for the last one.
  - After `l1-run --only 7`, `--only 8` gave PASS on criteria 2-5 and
    Unknowns 1-7. Criterion 1 is UNKNOWN for L1's three classes.
  - Two corrections to this section. `--only 8` is not free: without a
    step 7 record, five checks are UNKNOWN. The image was 2.1.286, not
    D13's 2.1.284.
- **Follow-ups from L2.** None blocks acceptance.
  1. `deprovision` deletes held requests without the warning `teardown`
     gives. This carries over from L1.
  2. `l1-run` needs explicit `AMAP_*_REPO` and `AMAP_SPEC_DIR` exports,
     where every other verb finds the siblings by discovery.
  3. The bring-up router stage waits the whole roster timeout while the
     container crash-loops. Proposed: stop as soon as the container is
     restarting or exited, and report its exit code and last log line.
     This awaits the operator.
  4. (Done in S8d. amap-deploy-sandy's maintainers fixed its own copy in amap-deploy-sandy PR
     #24, not yet merged, and confirmed the eleven imported names are
     unchanged.) The router sections, copied from sandy, print "(do not: do
     not ...)".
     Their stopped-router remedy says `run.sh --detach`, which fails on the
     stopped container's name. Fix our copy and tell amap-deploy-sandy's maintainers.
  5. Report to amap-router-local and amap-connector-claude: every correct
     reply logs a WARNING, because the connector's guidance has agents fill
     in `to`. **Sent 2026-10-04.** The connector keeps `to` required (it
     answered on 2026-10-05). The spec requires `to` on every request:
     `submit-request.schema.json` `draft.required` and `minItems: 1`, and
     `fixtures/invalid/request-empty-to.json` must fail, which was checked
     here. So any fix belongs in the router: warn only when `to` differs
     from the bound sender. **amap-router-local's maintainers chose that
     (2026-10-05).** A `to`/`cc` naming only the bound sender logs at
     DEBUG, compared casefolded. Anything else WARNs, with "addressed N
     recipient(s) other than its bound sender": a count, not the addresses,
     because the router never echoes agent-supplied recipients into its
     log. The ledger binding and the discard are unchanged. It is commit
     `e43dbba`, local to that agent until a push problem is cleared.
     Bumping our router pin waits for it to be on main with CI passing.
  6. Optionally set `DISABLE_AUTOUPDATER` in the rendered environment. It
     would quiet Claude Code's startup download attempt, which the policy
     denies.

### S8d: The L2 follow-ups

- **Kind:** offline
- **Depends on:** L2
- **Why:** L2's follow-ups 1 to 4. The operator said go on 2026-10-04.
  Follow-up 5 was sent to the router and connector agents as a finding.
  Follow-up 6 stays optional and is not part of this step.
- **Read first:**
  - `docs/POC-REPORT.md`, the L2 section;
  - `l1_run.py`: `REQUIRED_VARIABLES`, `load_inputs`, `step_6`,
    `router_state`, and how `known_repos` finds the checkouts;
  - `router_link.py` and `l1_kit.find_connector`, for discovery;
  - `verbs.py`: `run_deprovision`, and `run_teardown`'s held warning;
  - `router_sections.py`: the line formatter, and the remedy of "the
    router container is running";
  - `bring_up.py`, which calls `step_6`;
  - the tests for each.
- **Deliverables:**
  1. **The router stage fails fast.** While `step_6` waits for the roster,
     it also checks the container's state. If the container is
     `restarting`, `exited` or `dead`, the step stops at once. It does not
     wait out `router_timeout`. Its note gives the state, the container's
     exit code and the last non-blank line of its log, read with `docker`.
     The container is never removed or restarted. `bring-up` and `l1-run`
     both get this, because both use `step_6`.
  2. **`l1-run` finds the siblings.** The router, the connector and the
     spec are found the way the other verbs and the tests find them:
     - when `AMAP_ROUTER_REPO`, `AMAP_CONNECTOR_REPO` or `AMAP_SPEC_DIR` is
       set and not empty, it is the only place searched;
     - otherwise the search walks up for `amap-router-local`,
       `amap-connector-claude`, or `amap-spec` / `.amap-spec`;
     - each must contain its confirming file.

     Step 0 reports where each was found. A sibling that is not found is
     still a step 0 FAIL, naming the variable to set.
     `CLAUDE_CODE_VERSION` and `ANTHROPIC_API_KEY` stay required.
  3. **`deprovision` warns about held requests**, as `teardown` does. For
     each held request it would remove, it prints
     "held request <path> will be lost", in the dry run and with
     `--apply`. It removes nothing more and nothing less than before.
  4. **The router sections' text.**
     - The formatter no longer prints "(do not: do not ...)".
     - The remedy for "the router container is running" says to
       `docker start` the container when it exists but is stopped, and to
       use `docker/run.sh --detach` from the router checkout only when there
       is no container.
     - The attribution note says that our copy now differs from sandy's
       text in these two places, and that amap-deploy-sandy's maintainers was told
       (2026-10-04).
  5. Docs: `docs/TUTORIAL.md` and `docs/L1-RUNBOOK.md` stop telling
     operators to export the three sibling variables for `l1-run`. Each
     says the variables are needed only when a checkout is not beside this
     one.
  6. The tests.
- **Constraints:**
  - No check's PASS, FAIL or UNKNOWN logic changes. Only the line text and
    the remedy text change.
  - Every other behaviour stays as it is.
  - Tests fake `docker`. They never need a real sibling outside the
    existing discovery.
- **Acceptance criteria:**
  1. With the fake router container crash-looping (`restarting`), and
     separately `exited`, `step_6` and `bring-up`'s router stage stop well
     inside `router_timeout`. The note names the state, the exit code and
     the last log line. No call removes or restarts the container.
  2. A healthy start still waits for the roster and passes. An existing
     running container is still left alone.
  3. Step 0 passes with none of the three sibling variables set when the
     checkouts are beside the repository. Each variable, when set, is the
     only place searched. A missing sibling is a FAIL naming its variable.
  4. `deprovision` dry run and `--apply` both print the held warning for a
     held request, and its removal set is unchanged.
  5. No verify line contains "do not: do not". The container remedy names
     `docker start`.
  6. The full suite passes.
- **Stop and report if:**
  - the container state cannot be read without a call that changes the
    container;
  - sibling discovery for `l1-run` would need to import a sibling's code.

### S9: Design the CreateSandbox interceptor

- **Kind:** offline
- **Depends on:** L2
- **Read first:**
  - OpenShell: `docs/extensibility/gateway-interceptors.mdx`,
    `proto/gateway_interceptor.proto`, `examples/governance-interceptor/`
    and `crates/openshell-gateway-interceptors/src/routes.rs`.
  - This repo: DESIGN.md §6, and FINDINGS.md "S9: gateway interceptors in
    OpenShell v0.1.2", which already answers acceptance criteria 1, 3 and
    4 and the stop condition, with citations. Build on it; re-read the
    cited source only to confirm a point you rely on.
- **Deliverables:**
  - `docs/INTERCEPTOR.md`, which settles:
    - D5, the language and dependency policy (this is an exception to the
      standard-library rule, stated as one);
    - the `validate` rule: a bind source must lie under the creating sandbox's
      own `instances/<name>/`, the payload or the roster;
    - fail-closed binding (`binding_policy = "exact"`);
    - how `verify` confirms the interceptor is registered;
    - a test plan.
- **Acceptance criteria:**
  1. The document names each RPC and phase it binds, citing `routes.rs`.
  2. It shows how a request that mounts another member's outbox is refused.
  3. It states which gateway misconfiguration would silently disable the
     interceptor, and how `verify` detects it.
  4. It lists every RPC that can change a sandbox's driver mounts after
     creation, if any, citing the proto. Binding `CreateSandbox` alone is
     enough only if there are none. The OpenShell agent flagged this as
     unchecked.
- **Stop and report if:** an interceptor cannot see the driver config it has
  to check.

### S10: Implement the interceptor

- **Kind:** offline
- **Depends on:** S9, and the operator's approval of `docs/INTERCEPTOR.md`
  (given 2026-10-05)
- **Deliverables:**
  - the interceptor as `docs/INTERCEPTOR.md` specifies it, its `verify`
    checks (V1 to V3) and its tests, with the stubs and hash locks generated
    by `interceptor/regen.sh --apply` in S10 itself (D18);
  - `install` refusing a home that is not its own real path (D19);
  - the OpenShell release watcher (D20). It follows OpenShell release tags,
    regenerates the stubs from the new tag's protos, runs the suite, and opens
    a PR marked as needing a live run, because merging it means upgrading the
    gateway first.
- **Acceptance criteria:** those stated in S9's test plan, plus rules 1–7
  above.
- **Stop and report if:** the implementation needs a dependency S9 did not
  approve.

### S10b: Deploy the interceptor

- **Kind:** offline
- **Depends on:** S10
- **Why:** under D22, a host's `verify` FAILs V1-V3 until the interceptor is
  running and registered, so a fresh host cannot end `AMAP is up`. Turning the
  interceptor on means building its virtualenv, starting `interceptor/server.py`
  **before** the gateway (under `fail_closed`, the gateway does not start
  without it), merging the fragment into `gateway.toml`, and restarting the
  gateway. `bring-up` never edits `gateway.toml` or restarts the gateway, and
  `provision-guest.sh` already owns the gateway's configuration.
- **Deliverables:**
  1. `examples/vms/proxmox/provision-guest.sh` (dry run by default, as now):
     - builds the hash-locked virtualenv with
       `pip install --require-hashes --only-binary=:all:`;
     - installs a systemd unit for `interceptor/server.py`, ordered before the
       gateway;
     - merges `gateway-config`'s interceptor fragment into `gateway.toml`, then
       restarts the gateway.
  2. `bring-up` checks that the interceptor is running before stage 2. If it is
     not, it stops and names `provision-guest.sh`. It still never edits
     `gateway.toml`.
  3. CI: a `tests.yml` job that runs `interceptor/tests` in the hash-locked
     virtualenv.
  4. The validator's open items from S10:
     - a test that a non-object mount is denied (R6);
     - a test that an absent `gateway.toml` makes V1 FAIL.
  5. The docs: the tutorial and the README say how the interceptor is
     deployed.
- **Acceptance criteria:**
  - Against the fakes, `provision-guest.sh --apply` orders the unit before the
    gateway and writes the fragment; the dry run changes nothing.
  - `bring-up` stops on a missing interceptor.
  - The CI job runs the gRPC tests.
  - The two new tests fail against a mutant.
  - The full suite passes.
- **Stop and report if:** the gateway's systemd unit cannot be ordered after
  another unit without editing OpenShell's own unit file.

- **Follow-up found live (2026-10-05).** Step 8 cannot upgrade its own
  interceptor block. When the fragment changes, as D24's did, an existing
  host's `gateway.toml` holds the old block. Steps 5 and 8 then stop on
  "exists with different settings", and need `--replace-gateway-config` by
  hand. The block is ours, identified by `name = "amap-openshell-mounts"`, so
  step 8 should replace that block in place and leave the base settings as
  they are. Until then, a host whose fragment changed is recreated, or rerun
  with `--replace-gateway-config`.

### S10c: Upgrade amap-deploy-sandy to `eeecad0`

- **Kind:** offline
- **Depends on:** S10b
- **Why:**
  - S11 imports `fleet_policy.derived_fleet_domain`, which sandy added in
    `eeecad0` (its PR #27, merged 2026-10-05; the change was verified here).
  - Moving from `0ec0ce3` also brings sandy's #18-#20, which changed the
    `INBOX-POLICY.md` and the MCP registration that our payload takes from
    sandy. Three drift tests fail until we take them again.
- **Deliverables:**
  1. The sandy pin goes to `eeecad0b5674c5771d0ed366f01b5e31144d2e89`, set by
     `python3 repin.py apply amap-deploy-sandy <commit>`.
  2. Our payload's copies of sandy's text are taken again from `eeecad0`, with
     the four approved substitutions and no others. The drift tests pass
     again.
  3. `router_sections.py`'s attribution note is updated. Sandy's
     `router_health.py` now has the two text fixes (its #24), so say whether
     our copy still differs, and where.
  4. Every other change in sandy between `0ec0ce3` and `eeecad0` is read,
     and each one that touches what we import or copy is either handled or
     reported. The eleven `router_health` names and the `fleet_policy` surface
     we use are confirmed unchanged by sandy's interface tests.
- **Acceptance criteria:**
  - The full suite passes with the sandy checkout at `eeecad0`.
  - The drift tests still fail against a mutant of the copied text.
  - `siblings.json` and every shipped pin quote agree.
- **Stop and report if:**
  - taking sandy's text again needs a substitution beyond the four approved
    and D23's named difference;
  - anything we import changed its behaviour.

### S11: The derived fleet domain (D21)

- **Kind:** offline
- **Depends on:** S10c, and amap-deploy-sandy's `fleet_policy` gaining
  `host_label` and `derived_fleet_domain`, pinned by its interface test, with
  the sandy pin bumped to that commit.
- **Deliverables:**
  - `install` gives a new fleet the domain
    `fleet_policy.derived_fleet_domain("openshell", <host>, base)`;
  - `install --fleet-domain-base B`;
  - an existing `fleet.json` is never rewritten;
  - `examples/fleet.json` and the docs updated;
  - DESIGN.md section 9's address item marked as settled by D21.
- **Acceptance criteria:**
  - A new install's domain matches `FLEET_DOMAIN_RE` and is
    `openshell.<host>.internal` for an ordinary hostname.
  - A hostname with nothing usable gives `openshell.internal`.
  - An existing `fleet.json` is byte-identical after `install`.
  - No copy of the derivation exists in this repository.
- **Stop and report if:** sandy's derivation is not in `fleet_policy`, or is
  not pinned by its interface test.

### L3: Publication gate (PLAN.md phase 4)

- **Kind:** gate
- **Depends on:** S10, and a live rerun of L2 with the interceptor enabled.
  That rerun passed on 2026-10-05 (POC-REPORT, "L2 rerun with the mounts
  interceptor on"), and the README's status line is updated. What remains is
  the operator's decision to publish.
- **Done when:**
  - the operator publishes;
  - README no longer says "staging copy";
  - DESIGN.md has no `[unverified]` tag that the phase-1 pass criteria
    depend on.
