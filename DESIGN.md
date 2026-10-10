# Design

Every OpenShell claim below is tagged:

- **[docs]**: stated in OpenShell's public documentation, but not yet tried.
- **[code]**: read in OpenShell's source at the cited revision, but not yet
  tried. It carries the same weight as [docs]. The source is cited because the
  docs don't say it.
- **[verified]**: confirmed by running it, on a live OpenShell host. Each
  cites the L1 run of 2026-09-30 and the line of `docs/POC-REPORT.md` that
  holds its evidence.
- **[unverified]**: assumed, and must be confirmed. Each links to a question in
  [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

**Nothing here has been run.** Phase 0 settled the tags by reading OpenShell
`main` at `acbac9c` and the release `v0.1.2`. OPEN-QUESTIONS.md holds the full
answers with file and line citations. Paths cited here without a repo name are
OpenShell's.

## 1. The five properties AMAP needs from a host

This list comes from amap-deploy-sandy's explainer ("Why sandy"). The runtime and
the connector need no changes between environments; only the deployment knows
where agents run.

| # | AMAP needs | Why | sandy provides it as | OpenShell, proposed |
|---|---|---|---|---|
| P1 | **A space per agent.** Each agent's lanes (inbox, peer, outbox) are its own | So no agent can read another's mail or write into another's outbox | One sandbox per workspace; `instances/<slug>/` lanes created at launch | One sandbox per agent **[docs]**. Our tooling creates each agent's lane directories on the host and passes them to that sandbox alone as `bind` mounts in `--driver-config-json` **[docs: `docs/how-it-works/sandboxes/runtimes.mdx:117-131`]**. This works on the Docker and Podman drivers only, and only after the operator enables it on the gateway (§6). OpenShell does not stop two sandboxes from being given the same host path, so exclusivity is our tooling's job, and `verify` checks it (§5) **[code: no such check in `crates/openshell-driver-docker/src/lib.rs:3844-3910`]** |
| P2 | **An identity the agent cannot forge** | The runtime binds a sender to its outbox. The address must come from the host, never from the agent | The slug, reported by sandy | The sandbox **name**, read by our tooling on the host and mapped to `<name>@<fleet_domain>`. The name is a DNS-1123 label of at most 19 characters, unique per workspace, and it survives stop, start and restart **[docs: `workspaces.mdx:178-190`, `sandboxes/overview.mdx:36-48`] [code: `crates/openshell-server/src/grpc/validation.rs:241`, `grpc/mod.rs:140`, `persistence/tests.rs:1027`]**. The agent cannot rename it: the API has no rename RPC, and a sandbox's credential reaches only supervisor RPCs **[code: `proto/openshell.proto`; `architecture/gateway.md:379-385`]**. **Changed in phase 0:** a deleted name can be created again with a new ID, and names are unique only within one workspace. So `membership.json` pins `(workspace, name, id)`, `verify` fails when the ID changes, and one fleet uses one OpenShell workspace. Name reuse is intended: "Names can be reused, so use them only for display and logging" **[docs: `docs/extensibility/supervisor-middleware/operations.mdx:27`]** |
| P3 | **Read-only mounts.** Inbound lanes, the connector and the agent's instructions are out of the agent's reach | So a compromised agent can't forge an inbound message or rewrite its own policy | Feature mounts; a write fails with `EROFS` | Two independent layers. (1) **The mount flag.** A `bind` mount with `read_only: true` (the default) becomes a Docker `:ro` bind on the agent's container **[code: `lib.rs:3745-3781`, `lib.rs:5770`]**. The agent can't remount it: the container runs with every capability dropped and `no-new-privileges` **[code: `lib.rs:5784-5797`]**. The host, and the router's own container, keep writing to the source directory, as with sandy. (2) **Landlock.** The filesystem policy lists `inbound/`, `peer/`, the roster and the payload as `read_only` and the outbox as `read_write`. Every other path is inaccessible, and the policy is locked at creation **[docs: `docs/security/best-practices.mdx:32,156-161`]**. The policy applies to mount targets like any other path **[code: `crates/openshell-sandbox/src/sandbox/linux/landlock.rs:224` ff.]**. The host's writes appear inside a running sandbox with no delay: the notice the router wrote is listed inside the running receiver **[verified: L1, 2026-09-30, `docs/POC-REPORT.md`, Unknown 1]**. A write gets `EROFS`, not `EACCES`: the kernel checks the read-only mount before Landlock's hooks run, on every write path (open, truncate, create, unlink, rename) **[verified: L1, 2026-09-30, `docs/POC-REPORT.md`, Unknown 2 and Pass criterion 3]**. Two pitfalls. If the policy's `read_only` and `read_write` lists are both empty, there is no Landlock policy layer at all **[code: `landlock.rs:241-242`]**. And a mount under a `read_write` parent, such as the workdir, gets write access from Landlock, so only the `:ro` flag protects it. Mount targets therefore stay outside every `read_write` path |
| P4 | **A supervised process** that injects deliveries into the session and is restarted if it dies | Otherwise delegations only surface when the agent happens to check its inbox | The manifest's `entry`, run by sandy's relay supervisor | **Changed in phase 0.** OpenShell supervises only one process, the main process: the command after `--` **[docs: `sandboxes/overview.mdx:23`]**. `sandbox exec` is not a supervisor **[docs: `overview.mdx:331`]**. So the main process is **our wrapper** on the read-only payload. It starts `inbox-delivery` in its own restart loop, then runs `claude`. On `main`, `--restart-policy on-failure` replaces the whole runtime if the wrapper exits. Release `v0.1.2` predates that flag, and there the exit is terminal **[docs: `overview.mdx:36-48`; `v0.1.2:crates/openshell-driver-docker/README.md:83`]**. The daemon injects over Claude Code's own Unix socket. OpenShell's seccomp filter leaves `AF_UNIX` alone, and its Landlock code scopes no sockets **[code: `crates/openshell-sandbox/src/sandbox/linux/seccomp.rs:187-215,855-890`]**. **New piece:** the daemon finds its session through `AMAP_DELIVERY_SESSION_SOURCE`, and sandy's lister reads tmux panes, which don't exist here. This deployment ships its own lister, which emits the single `claude` process's socket, read from a record that a SessionStart hook on the payload writes from `CLAUDE_CODE_MESSAGING_SOCKET`, and its key file, which is the session's own peer key `$HOME/.claude/sessions/<pid>.<id>.key` that Claude Code writes (the path amap-deploy-sandy's `payload/handoff-sessions` reads at `94a6372`). The hook never writes `CLAUDE_CODE_MESSAGING_TOKEN`: that token proves a connection comes from the session's own child, which the daemon is not (amap-connector-claude `37875a5`). End-to-end injection is **[verified: L1, 2026-09-30, `docs/POC-REPORT.md`, Pass criterion 2 and Unknown 4]**: one agent tasked the other, the daemon reported `delivered`, and the reply reached the sender. L1 ran with the hook's record as the key file; the peer key replaces it and is checked in L2. A receiver set to refuse yields `held`, never a false `delivered` |
| P5 | **Operator-owned membership.** Joining the fleet is a security decision | An agent must never add itself | Include/exclude rules, evaluated by sandy at each launch; `selected.json` | Our tooling decides, from the fleet policy, which OpenShell sandboxes get lanes. A sandbox without lanes is simply not in the fleet. Optionally, a gateway interceptor bound to `CreateSandbox` and `DeleteSandbox` does it **[docs: `docs/extensibility/gateway-interceptors.mdx:28-42`] [code: `crates/openshell-gateway-interceptors/src/routes.rs:18,21`]**. Its `post_commit` phase is fail-open, so it can miss events **[docs: `gateway-interceptors.mdx:135`]**. Polling `openshell sandbox list` or `WatchSandbox` stays as the backstop **[docs: `proto/openshell.proto:83,631`]** |

AMAP's own rule that **a sender is bound to its outbox** does most of the
identity work. The deployment only has to make sure each outbox is reachable from
exactly one sandbox.

## 2. Layout on the host

This mirrors amap-deploy-sandy. The paths are illustrative.

```
$AMAP_OPENSHELL_HOME/
  fleet.json            the operator's fleet policy (same model as the sandy
                        feature's `feature` section: fleet_domain, task_graph,
                        peers, groups)
  router.json           GENERATED from fleet.json; never hand-edited
  payload/              connector bin/, mcp-servers.json, INBOX-POLICY.md,
                        the wrapper (main process) and the session lister.
                        Read-only in every sandbox
  roster/               written by the router; read-only in every sandbox
  instances/<name>/     each agent's lanes, created by our tooling before the
                        sandbox; the router reads and writes these
  membership.json       our record of which sandboxes are in the fleet, as
                        (workspace, name, id)
  selected.json         GENERATED from membership.json: the router's
                        discovery verdict (router `selected_json`); the
                        router derives roster/ beside it
```

## 3. Inside each sandbox (the agent's view)

| Path in the sandbox | Access | Contents |
|---|---|---|
| `/opt/amap/payload` | read-only | connector binaries, MCP config, policy prompt, wrapper, session lister, Claude Code settings, SessionStart hook, first-run seeder |
| `/opt/amap/roster` | read-only | `roster.json` |
| `/opt/amap/lanes/inbox` | read-only | mail notices and messages |
| `/opt/amap/lanes/peer` | read-only | delegation notices |
| `/opt/amap/lanes/outbox` | read-write | the drop-box that `inbox-submit` writes |

The lane names are the router's (`router.config.LANES` in amap-router-local), and `render.py` renders exactly these targets.

**One uid for everything.** Every sandbox runs as the same non-root uid:gid
as the router container. It is set as `process.run_as_user` and
`run_as_group` in each policy **[docs: `runtimes.mdx`, "Sandbox User
Identity"]**. The router creates agent-visible files 0600 and checks no
ownership, so a different uid fails as `EACCES` (amap-router-local
`router/attachments.py:477`, `docker/run.sh:81`).

Each row is one `bind` mount **and** one entry in the filesystem policy. The
mount's `read_only` flag and the policy entry must agree. A target that isn't in
the policy is inaccessible to the agent **[docs: `best-practices.mdx:161`]**.
The targets avoid OpenShell's reserved roots (`/opt/openshell`, `/etc/openshell`,
`/run/openshell` and others) **[code: `crates/openshell-core/src/container_paths.rs:32-41`]**.

The connector's environment variables (`MAILBOX_ROOT_DIR` or the per-tool ones,
`INBOX_LANE`, the eight `AMAP_DELIVERY_*`, and optionally `AMAP_DELIVERY_SELF`)
point at these paths. They are passed with `openshell sandbox create --env`,
and every process in the sandbox sees them **[docs: `overview.mdx:366-376`]**.
Claude Code receives the MCP config and the policy prompt through the same
arguments sandy passes today (`--mcp-config`, `--append-system-prompt-file`).
Here they go on the wrapper's command line after `--` **[docs: `overview.mdx:23`]**.
Claude Code also gets `--settings` (the payload's `claude-settings.json`) and
`--dangerously-skip-permissions` (D8). The settings file sets
`crossSessionInbound` to `accept` from the read-only payload. Per Claude Code's
docs, the project and local settings files can only tighten that value, so the
agent can hold or refuse its own delegations through its writable
`.claude/settings.local.json`, but cannot undo the operator's value.
**[verified: L1, 2026-09-30, `docs/POC-REPORT.md`, Unknown 5]**: a write to the payload's settings file
fails with `EROFS`, the delegation was delivered under `accept`, and
tightening through `settings.local.json` produced `held`. Whether a
`settings.json` of the agent's own changes the value was not exercised.

**Unattended start.** The wrapper merges D9's first-run answers into
`.claude.json` before `claude` starts. They were observed, not documented, on
Claude Code 2.1.284, and L1 checks that no prompt blocks. The API-key answer is
the suffix of the placeholder the sandbox sees **[docs:
`providers/overview.mdx:366-369`]**. The create command has `--detach --tty
--auto-providers` **[code: `crates/openshell-cli/src/main.rs:1523-1555`;
`run.rs:641-653`; `commands/provider.rs:481-497`; also
`v0.1.2:crates/openshell-cli/src/main.rs:1523-1547`]**.

## 4. Network

- **Connector:** no network rules at all. It makes no network calls; mail leaves
  only through the outbox.
- **Claude Code:** only its model endpoint. The default policy denies all egress,
  and attaching this deployment's provider, `amap-claude-code`
  (`providers/amap-claude-code.json`, adapted from OpenShell's
  `providers/claude-code.yaml`), adds only its one endpoint, for the binaries
  rendered from the image's real paths **[docs: `providers/README.md`;
  `docs/how-it-works/providers/profiles.mdx:477-490`;
  `docs/how-it-works/policies/network-rules.mdx:57-77`;
  `docs/how-it-works/policies/default-policy.mdx:42-47`,
  `docs/how-it-works/inference.mdx:11-24`]**. The image paths and the profile
  import are **[verified: L1, 2026-09-30, `docs/POC-REPORT.md`, step 3]**, and the one endpoint holds: a
  call to another host was denied and logged **[verified: L1, 2026-09-30, `docs/POC-REPORT.md`,
  Pass criterion 4]**. The provider needs a Console API
  key, not a subscription token **[docs: `docs/how-it-works/providers/overview.mdx:458`]**.
  Network denials are logged as OCSF `NET:OPEN ... DENIED` **[docs:
  `docs/observability/logging.mdx:120-123`]**.
- **Router:** in its own container on the host, with no network, as today.

## 5. Verify

Port the `verify` verb with the same three outcomes: PASS, FAIL and **UNKNOWN**.
UNKNOWN is a problem, never a pass. The router checks carry over. The
per-sandbox checks read OpenShell's state instead of sandy's:

- the sandbox exists, and its `(workspace, name, id)` matches `membership.json`;
- its policy has the required `read_only` and `read_write` paths, and allows no
  mail egress;
- the container's own mount table shows each lane mounted from that sandbox's
  own `instances/<name>/`. Inbound lanes and the payload must be `ro`. Nothing
  else in the fleet may mount the same source.

**Changed in phase 0.** The sketch planned a check that sandy's version can't
make: attempt a write to `inbound/` from inside the sandbox, and confirm that
OpenShell both denies it **and logs the denial**. The logging half can't pass.
OpenShell logs no filesystem access. Landlock produces `CONFIG:` events at
startup and when it skips a path, and nothing when it denies a read or write
**[docs: `docs/observability/logging.mdx:55-64,235-253`]**. The check becomes:

- attempt the write from inside the sandbox (`openshell sandbox exec`), and
  expect it to fail with `EROFS` **[verified: L1, 2026-09-30,
  `docs/POC-REPORT.md`, Unknown 2]**;
- confirm from host state that **both** layers are in place: the `:ro` mount in
  the container's mount table, and the `read_only` entry in the effective
  policy.

What OpenShell adds over sandy is therefore not a logged filesystem denial. It is:

- Landlock as a second, host-enforced layer under the read-only mount;
- default-deny egress, where every denial *is* logged;
- credentials the agent never holds.

## 6. Gateway configuration this deployment requires

Found in phase 0. Host bind mounts are off by default, and turning them on is
explicitly an "unsafe operator override" **[code: `architecture/compute-runtimes.md:348-355`]**.
The deployment needs, in `gateway.toml`:

```toml
[openshell.drivers.docker]          # or podman
allow_driver_config = true
enable_bind_mounts = true

[openshell.drivers.docker.resource_admission]
enabled = false
```

**[docs: `runtimes.mdx:120-131`] [code: `lib.rs:1049-1058`, `lib.rs:3852-3856`]**

Disabling resource admission applies to the whole driver. Any caller allowed to
create sandboxes on that gateway can then bind-mount any host path the container
runtime can see, including another agent's outbox, read-write. It also switches off the approval-label checks on existing volumes, not just the bind check **[code: `crates/openshell-driver-docker/src/lib.rs:1185-1205`, `crates/openshell-core/src/resource_admission.rs:222-230`]**. OpenShell has no planned mechanism for admitting specific host paths while admission is on. Agents can't do
this themselves, because a sandbox's credential can't call `CreateSandbox`
**[code: `architecture/gateway.md:379-385`]**. Every workspace user can, and a
local gateway without OIDC treats every authenticated user as a Platform Admin
**[docs: `docs/how-it-works/workspaces.mdx`]**. So:

- **This is the designed scope, not a temporary risk (D17, 2026-10-05).** The
  deployment is for a dedicated gateway on one host with one operator (section
  8). Every caller of the gateway acts for that operator.
- **The interceptor (S9, S10).** A `CreateSandbox` `validate` interceptor
  rejects any bind whose source is not exactly one of the rendered sources of
  the sandbox being created: its own `instances/<name>/` lanes, the payload or
  the roster. Under the scope, it protects the operator from a wrong mount by
  their own hand or tooling. It is not a boundary between people.
  [docs/INTERCEPTOR.md](docs/INTERCEPTOR.md) has the design. The interceptor is
  deployment code, not OpenShell code.

## 7. Out of scope, on purpose

- **Tunnelling the spool over HTTP**, if shared directories turn out to be
  impossible. That turns AMAP into a transport, and it loses the read-only tree,
  atomic commits and single-writer guarantees. Declare the deployment
  unsupported instead.
- **OpenShell-specific code in connector-claude or router-local.**
- **Mail logic in OpenShell's supervisor middleware.**
- **Kubernetes, remote gateways and the MicroVM driver**, until the single-host
  proof of concept passes (see PLAN.md, phase 3). The VM driver documents no
  host mounts at all **[docs: `runtimes.mdx`, MicroVM section]**.

## 8. Scope: one host, one operator

**Decided 2026-10-05 (D17).** This repository deploys amap-router-local and the
amap-connector-claude payload onto **one OpenShell host, under one operator**.
The router, the payload, every lane and every sandbox are on that host, and
every account and process on the host acts for that operator.

What it protects:

- **Agents from the host.**
  - Each agent's inbox and peer lanes are read-only to it, in both the mount
    and Landlock.
  - Its only egress is the provider's endpoint.
  - It has no route to the gateway (POC-REPORT, Pass criteria 3 and 4).
- **Agents from each other.**
  - Each member's lanes are its own.
  - Delegation is directed by the task graph.
  - The router binds each reply to the sender it answers (POC-REPORT, Pass
    criterion 5, and the L2 step 10 record).

What it does not protect: **the host from its own operator.** Anything that
can call the gateway on loopback is trusted, because it is the operator. The
interceptor (section 6) guards against the operator's mistakes, not against
another person.

## 9. Widening the scope

The scope above is a design boundary. Each widening below needs the listed
work first. None of it is planned.
[Issue #1](https://github.com/proofpoint/amap-deploy-openshell/issues/1)
tracks it.

**Several operators on one host.**

- **Gateway authentication.**
  - Turn on mTLS user auth or OIDC, and use OpenShell's workspace roles.
  - Give each operator their own workspace. The (workspace, name, id) pin
    already assumes names are unique only per workspace.
  - Today the gateway builds no authenticator chain, so the caller's principal
    is `unknown`, a fixed local-dev user, or an mTLS subject
    **[code: v0.1.2 `crates/openshell-server/src/multiplex.rs:876-985`]**.
- **The interceptor becomes a boundary.** Add:
  - a membership gate: deny a create for a name `membership.json` records, as
    `provision` already does;
  - a principal check, once callers are authenticated;
  - extension-JWT verification, which needs a dependency D5 did not approve;
  - a `validate` binding on `UpdateConfig`, since a global-scope update
    replaces every sandbox's policy, which is the Landlock layer.

  Admission stays off gateway-wide (section 6), so the interceptor would be
  all that keeps one operator's sandbox off another's files.
- **The one shared uid (D7).**
  - One uid:gid owns every lane, the router and every sandbox. Several
    operators need their own uids, or the lanes stop being private.
  - Whether one router can write lanes owned by several uids is a question for
    amap-router-local, not for this repository.
- **Checks that assume the host is ours.** Each becomes per-workspace, or is
  dropped:
  - `l1-run`'s "no sandbox exists but the fleet's";
  - `verify`'s "no sandbox in the workspace is outside the fleet";
  - `teardown` removing the whole install;
  - the single `$AMAP_OPENSHELL_HOME`.
- **One router or several.** Either one router per operator, with its own home
  and container, or one router with multi-tenant configuration, agreed with
  amap-router-local.

**A fleet of OpenShell hosts, one operator.**

- **No delegation between hosts.**
  - The spool is local directories, and a network transport for it is out of
    scope (section 7).
  - amap-router-local routes within one host.
  - So a fleet of hosts is **independent fleets, one per host**.
  - **Between hosts, the runtime's trusted components speak standard mail
    protocols (D26, the operator's decision, 2026-10-07).** That means SMTP
    submission or relay between hosts, and LMTP for local delivery.
    Authenticated submission establishes which agent and router a message
    comes from. There is no new AMAP-shaped hop.
  - AMAP stays at the agent boundary: inside each sandbox the spool
    directories are unchanged.
  - The draft's Non-goals (section 1.5) say AMAP "is not a mail transport"
    and "defines no network protocol". Per-hop domain authority, next-hop
    routing and loop detection are what SMTP relaying already standardises.
  - On OpenShell the hop is a network path between trusted, host-side
    components, which the host's own policy allows explicitly. It is not
    egress from a sandbox, and the spool is still never tunnelled
    (section 7).
  - Every agent-facing invariant still binds the composite runtime. Each is
    already an obligation in draft -00, cited by section name (numbers may
    change):
    - exactly one Result per consumed request, even when an inner component
      fails ("Outbound: Result", 6.1);
    - accepted only after the provider accepts ("Outbound: Result", 6.1);
    - the recipient set handed to the provider equals the set evaluated
      ("Outbound: Submit Request", 5.1);
    - enforcement by absence ("Inbound: Deliver Notice and Inbound
      Message": its preamble, and 7.3 "The Inbound Message (body spool)");
    - the peer write-authority partition ("Volume Layout", 4.1);
    - attribution by namespace, carried by configuration and never by
      anything the agent wrote ("Volume Layout", 4.1).
  - amap-spec PR #17 (issue #12) proposes, for -01, an informative section
    "Runtimes Built from Several Components" that summarises these. It is
    not merged, and its wording may change.
- **Addresses.** Settled by D21. A new fleet's domain is `openshell.<host>.<base>`, derived once by `install` with amap-deploy-sandy's `fleet_policy.derived_fleet_domain`, so two hosts' `alpha` have different addresses, and a sandy fleet on the same machine (`sandy.<host>.<base>`) is a separate namespace. The default base, `internal`, is non-routable, which amap-spec `spec/peer-origin.md` §3 (lines 180-182) allows for a same-host fleet without mail. Cross-host traffic is mail and needs a routable base (`install --fleet-domain-base`).
- **Operations.** The verbs are per host. A fleet needs per-host inventory and
  a way to roll out sibling pins. The repin workflow proposes pins; it does
  not roll them out.

**Several hosts and several operators.** Both lists above.

