# Open questions for the OpenShell side

Answer each from OpenShell's **code or docs**, and cite the file or page.
"Probably" is not an answer: mark it unknown instead. Q1, Q3 and Q5 are
blocking. The others shape the design but don't stop the proof of concept.

## How the answers below were reached

Phase 0 answered these questions on 2026-09-28 by **reading** NVIDIA/OpenShell.
Nothing in OpenShell was installed or run. Each answer says whether it rests on
reading or on running something. "Reading" means the file says so. It does not
mean anyone has watched it happen.

Citations use these revisions:

- **`main@acbac9c`**: OpenShell `main` at commit `acbac9c` (2026-09-29 UTC).
  Paths without a prefix refer to this commit.
- **`v0.1.2`**: the latest release tag (`6648bd0`). It is cited only where it
  differs from `main`.

Summary of the answers is in [FINDINGS.md](FINDINGS.md).

## Q1. Host directories in a sandbox (blocking: P1 and P3)

- Can a host directory be mounted or bind-mounted into a sandbox? How is that
  declared, and in which file?
- Can the filesystem policy make a mounted path **read-only to the agent** while
  the host process (the router) keeps writing into it?
- Do the host's writes show up inside a running sandbox, with no restart and no
  caching layer?
- Does a write to a read-only path fail with `EROFS` or `EACCES`, and does it
  appear in OpenShell's deny log?
- Does the filesystem policy apply to mounted paths, or only to paths inside the
  image?

### Answer: yes, on the Docker and Podman drivers, behind an operator opt-in (reading)

**Mounting a host directory: yes, per sandbox, but only if the operator enables
it on the gateway.**

- The directory is declared at sandbox creation, in
  `openshell sandbox create --driver-config-json '{"docker":{"mounts":[...]}}'`.
  A mount has `type: "bind"`, `source` (absolute host path), `target`, and
  `read_only`, which defaults to `true`. Source:
  `docs/how-it-works/sandboxes/runtimes.mdx:117`. Podman takes the same fields
  under the `podman` key (`runtimes.mdx:156`).
- The gateway rejects the request unless three settings are made in
  `gateway.toml`, in `[openshell.drivers.docker]`: `allow_driver_config = true`,
  `enable_bind_mounts = true`, and `resource_admission.enabled = false`. Sources:
  `runtimes.mdx:120-131`,
  `crates/openshell-driver-docker/src/lib.rs:1049-1058` (rejects a bind when
  admission is on) and `lib.rs:3852-3856` (rejects a bind without
  `enable_bind_mounts`).
- The same behaviour is present in the release `v0.1.2`
  (`v0.1.2:docs/how-it-works/sandboxes/runtimes.mdx:115-120`).
- **Not on the VM driver.** The MicroVM section documents no mounts. On
  **Kubernetes**, only existing PVCs can be mounted (`runtimes.mdx:230-263`),
  which is Q7.

**Read-only to the agent while the router writes: yes, but the mount flag
enforces it, not the policy.**

- With `read_only: true`, the driver emits a Docker bind string with `:ro`
  (`lib.rs:3745-3781`, the `opts.push("ro")` at `lib.rs:3767`). It attaches
  the string to the **workload** container, which runs the agent
  (`lib.rs:5770`, in `build_container_create_body_for_image` at
  `lib.rs:5638`). A read-only bind mount is read-only only inside the
  container. The host directory underneath stays writable to host processes,
  including the router's own container, which mounts the same host path
  read-write.
- The agent cannot remount the directory. The workload container runs with
  `cap_drop: ALL`, `no-new-privileges:true` and `network_mode: none`
  (`lib.rs:5784-5797`).
- The *filesystem policy* can also list the target under
  `filesystem_policy.read_only`. That adds Landlock as a second, independent
  layer (see the last point below).
- Mount targets must avoid OpenShell's reserved roots (`/opt/openshell`,
  `/etc/openshell`, `/etc/openshell-tls`, `/run/openshell`,
  `/run/openshell-sidecar`, `/run/netns`, `/var/run/netns`):
  `crates/openshell-core/src/container_paths.rs:1-41`, `runtimes.mdx:133`. The
  `/opt/amap/...` targets in DESIGN.md §3 do not collide with them.

**Host writes visible live: unknown.** OpenShell adds no layer between a bind
source and its target. It passes a plain Docker bind (`lib.rs:3745-3781`). Live
visibility is therefore Docker's bind-mount behaviour on the gateway host, not
OpenShell's. That is expected on a Linux host. On macOS, it also depends on the
Docker VM's file sharing. Neither was run. **To settle it:** with a sandbox
running, have the router place a notice, then list `inbound/notices` from
inside the sandbox with `openshell sandbox exec`. Do it on the target host
platform.

**`EROFS` or `EACCES`: unknown. OpenShell's deny log: no, not for filesystem
access (reading).**

- The `:ro` flag makes the kernel refuse writes with `EROFS`. That is Docker
  and kernel behaviour, not something OpenShell documents. If the path is
  also listed in the policy's `read_only` list, Landlock would refuse with
  `EACCES`. When both apply, the agent sees **`EROFS`**. The OpenShell agent
  read the kernel's code paths: `mnt_want_write` runs before Landlock's
  `security_file_open` and `security_path_*` hooks on open, `O_TRUNC`,
  create, unlink, rename and truncate. That is reading, not a run, so L1
  confirms it.
- **OpenShell does not log individual filesystem denials.** Its OCSF event
  classes are Network, HTTP, SSH, Process, Detection Finding, Config State
  Change, Application Lifecycle and Base Event. None of them is a File System
  Activity class (`docs/observability/logging.mdx:55-64`). The "Filesystem
  Sandbox Logs" section says Landlock emits `CONFIG:` events **at startup and
  when a requested path is skipped**, and nothing per access
  (`logging.mdx:235-253`). Nothing in `crates/openshell-ocsf/src/builders/`
  builds a file-activity event. **This changes the design** (DESIGN.md §5,
  PLAN.md phase 1): "denied *and logged by OpenShell*" cannot pass as written.

**Does the filesystem policy apply to mounted paths: yes, by construction
(reading, not run).**

- Landlock rules are built by opening each policy path by name inside the
  container (`crates/openshell-sandbox/src/sandbox/linux/landlock.rs:224` ff.).
  A policy path that is a mount target is opened like any other path. There is
  no image-versus-mount distinction in the code.
- Paths not listed are inaccessible (`docs/security/best-practices.mdx:157-161`).
  So every lane target **must** be listed, `read_only` or `read_write`, or the
  agent cannot use it at all.
- The filesystem policy is locked at creation. Changing it needs the sandbox
  to be recreated (`best-practices.mdx:32`).
- Caveat: the docs warn that bind mounts "can bypass workspace isolation and
  filesystem policy" (`runtimes.mdx:120`; `architecture/compute-runtimes.md:348-355`).
  The text does not say how. The reading here is that the risk is exposing
  host state that the policy never considered, but that is not confirmed.
  **To settle it:** mount a lane without listing it in the policy, and confirm
  the agent cannot read it.

**New issue found while answering (not in the original list):**
`resource_admission.enabled = false` applies to the whole driver. On a gateway
configured this way, **any caller allowed to `CreateSandbox`** can bind-mount
any host path the Docker daemon can see, read-write if the caller asks for it.

- Sandbox principals cannot call `CreateSandbox`: sandbox JWTs are accepted
  only on the supervisor RPC allowlist (`architecture/gateway.md:379-385`). So
  the agents themselves cannot do this, assuming no user credential is placed
  inside a sandbox.
- Every workspace user can (`docs/how-it-works/workspaces.mdx`, operations
  table).
- A local gateway without OIDC treats every authenticated user as Platform
  Admin (`workspaces.mdx`, Note).
- Mitigation: a gateway interceptor bound to `CreateSandbox` `validate`
  (see Q2) can reject any bind source outside the AMAP instance tree, or any
  bind that doesn't match that sandbox's own lanes.
- Nothing in OpenShell stops one sandbox from being given another's outbox.
  Exclusivity (P1) is our tooling's job and `verify`'s check.

## Q2. Lifecycle hooks (P5)

- Does the gateway expose hooks, interceptors or plugins that run when a sandbox
  is created or deleted?
- Failing that, what's the smallest reliable way for host tooling to learn about
  sandbox creation and deletion? Polling the CLI or API is acceptable.

### Answer: yes, gateway interceptors (reading)

- An external gRPC service registered in `gateway.toml` can bind to
  `CreateSandbox` and `DeleteSandbox`. Both are on the interceptable allowlist
  (`crates/openshell-gateway-interceptors/src/routes.rs:18,21`).
- Phases are `modify_operation` (allow, deny or JSON-patch the request),
  `validate` (allow or deny), and `post_commit` (observe only, always fail-open)
  (`docs/extensibility/gateway-interceptors.mdx:28-42,135`).
- Registration is static: the gateway must restart when an interceptor is added
  (`gateway-interceptors.mdx:102`).
- `post_commit` is fail-open, so a missed observation is possible. Auto-provision
  from `post_commit` alone could silently skip a sandbox, and polling is
  still needed as the backstop.
- Polling: `ListSandboxes` and the streaming `WatchSandbox`
  (`proto/openshell.proto:83,631`), or `openshell sandbox list --output json`.
- A `validate` binding on `CreateSandbox` is also the natural place for the
  bind-source restriction described under Q1.

## Q3. Sandbox identity (blocking: P2)

- Does each sandbox have a stable name or ID that survives restart?
- Can the host read it?
- Can the agent **set or change** it? If so, it isn't usable as identity.

### Answer: yes. Use the name as the address, and pin the ID alongside it (reading)

**Stable name and ID:** yes. Each sandbox has a gateway-generated ID and a
human-readable name.

- The ID is "Stable object ID generated by the gateway" (`proto/datamodel.proto:39-43`).
- The name is a DNS-1123 label of at most **19** characters
  (`crates/openshell-server/src/grpc/validation.rs:241-251`,
  `crates/openshell-server/src/grpc/mod.rs:140`).
- The name is unique **within a workspace**
  (`crates/openshell-server/src/persistence/tests.rs:1027`).
- Public RPCs address a sandbox by name plus workspace and do not accept the ID
  (`docs/how-it-works/workspaces.mdx:178-190`).
- Stop/start and the restart policy keep the sandbox record. A replacement
  "retains the sandbox identity and configuration"
  (`docs/how-it-works/sandboxes/overview.mdx:36-48`, `runtimes.mdx:13-16`).

**The host can read it:** yes. Use `openshell sandbox list|get --output json`
or the `ListSandboxes` and `GetSandbox` RPCs. The Docker driver also labels
each container with the sandbox ID, name and workspace
(`crates/openshell-driver-docker/src/lib.rs`, `LABEL_SANDBOX_ID` and
`LABEL_SANDBOX_NAME` in `build_container_create_body_for_image`).

**The agent can change it:** no, as far as reading shows.

- The API has no rename or update RPC for sandboxes. `proto/openshell.proto`
  lists Create, Get, List, Delete, Stop and Start only.
- The sandbox's own credential cannot call those RPCs anyway
  (`architecture/gateway.md:379-385`). The gateway resolves a sandbox
  principal by "the immutable ID in its authenticated identity"
  (`gateway.md:149-157`).
- The agent can change environment variables inside its own processes, so
  anything the agent reads from its environment is *not* identity. The
  deployment must derive the address on the host, which is what DESIGN P2
  already says.

**Remaining limits:**

- Names are reused, by design. After a delete, the same name can be created
  again with a new ID. OpenShell's docs say: "Names can be reused, so use them
  only for display and logging"
  (`docs/extensibility/supervisor-middleware/operations.mdx:27`). The unique
  index covers live objects only
  (`crates/openshell-server/migrations/sqlite/006_add_workspace_column.sql`).
  The OpenShell agent confirmed this.
- Names are also only unique per workspace.
- The design therefore pins `(workspace, name, id)` in `membership.json`.
  `verify` fails on an ID mismatch, and the fleet is restricted to one
  workspace. See DESIGN.md P2.
- Address syntax was checked. `<name>@agents.example.org`, for a range of
  valid sandbox names up to 19 characters, matches the address `pattern` in
  amap-spec's `schemas/roster.schema.json`. The check ran the pattern through
  Python's `re` against sample names. It was not a full JSON Schema
  validation, because no validator is installed here.

## Q4. Agent launch arguments

- How are the launch arguments and environment variables of Claude Code (or any
  agent) configured per sandbox?
- How is an MCP config file supplied: baked into the image, from a mounted path,
  or as an argument?

### Answer (reading)

- The trailing command of `openshell sandbox create ... -- <cmd> <args>` is
  the sandbox's **canonical main process** (`overview.mdx:23`). Claude Code's
  `--mcp-config` and `--append-system-prompt-file` are passed there.
- `--env KEY=VALUE` (repeatable) sets variables "available to all processes in
  the sandbox" (`overview.mdx:366-376`). The connector's `AMAP_*` and
  `INBOX_*` variables go here.
- The MCP config can come from any readable path: the read-only payload bind
  mount (Q1), or the image.
- Claude Code reaches the model through a provider. With the `claude-code`
  provider, the CLI reads `ANTHROPIC_API_KEY`
  (`docs/how-it-works/providers/overview.mdx:354`). A Console API key is
  required. A subscription token is not accepted
  (`providers/overview.mdx:458`).
- Unknown: whether Claude Code's cross-session receiver setting
  (`crossSessionInbound`) can be set from inside the image or a mount without
  being writable by the agent. That is a Claude Code question, not an
  OpenShell one. Settle it in phase 1.

## Q5. A supervised side process (blocking: P4)

- Can a sandbox run a second, long-lived process beside the agent, for example
  the connector's `inbox-delivery`? Does anything restart it if it exits?
- If not, is a wrapper entrypoint supported, where the image starts the daemon
  and then the agent?
- Can a process inside the sandbox reach Claude Code's own messaging socket, or
  does the process confinement (seccomp or Landlock) block a Unix socket between
  two processes in the same sandbox?

### Answer: a wrapper as the main process, supervising the daemon itself (reading)

**A second supervised process: no.** OpenShell supervises exactly one process,
the canonical main process (`overview.mdx:23`).

- `openshell sandbox exec` runs one-shot commands. It is not a supervisor, and
  "if a background process keeps stdout or stderr open for more than 30
  seconds after the command exits, `exec` reports an output delivery failure"
  (`overview.mdx:331`).
- Nothing in OpenShell restarts a secondary process.

**Restarts:** only the whole runtime, and only on `main`.

- `--restart-policy on-failure|always` replaces the supervisor *and* the main
  process when the main process exits (`overview.mdx:36-48`,
  `runtimes.mdx:13-16`).
- That landed in commit `acbac9c` on 2026-09-29, **after** `v0.1.2`. In
  `v0.1.2`, "canonical main-process exit is terminal"
  (`v0.1.2:crates/openshell-driver-docker/README.md:83`). Nothing is
  restarted.

**A wrapper entrypoint: yes, by construction.**

- The main process is whatever command follows `--` (`overview.mdx:23`). A
  wrapper on the read-only payload can start `inbox-delivery` in its own
  restart loop and then run `claude`.
- Operators attach to the main process with `openshell sandbox connect`, which
  gives a retained PTY (`overview.mdx:262-275`, `architecture/sandbox.md:710-719`).
- The agent runs as the same UID as the wrapper
  (`architecture/sandbox.md:14-15`), so it can kill the daemon. That is the
  same as under sandy. The wrapper's loop restarts the daemon. If the wrapper
  itself dies, the main process has exited, which on `main` triggers the
  restart policy.

**A Unix socket between two processes in one sandbox: not blocked, from
reading.**

- In block mode, seccomp denies `AF_INET`, `AF_INET6`, `AF_PACKET`,
  `AF_BLUETOOTH`, `AF_VSOCK` and non-route `AF_NETLINK`. It leaves `AF_UNIX`
  alone (`crates/openshell-sandbox/src/sandbox/linux/seccomp.rs:187-215`). A
  behavioural test asserts "retaining Unix IPC"
  (`seccomp.rs:855-890`).
- OpenShell's Landlock code sets no Unix-socket scoping. Nothing in
  `landlock.rs` refers to scope or abstract sockets.
- `inbox-delivery` binds its reply socket in Claude Code's socket directory,
  `/tmp/cc-socks` (amap-deploy-sandy `payload/handoff-sessions`). `/tmp` is
  read-write in the default policy (`best-practices.mdx:184`).
- Not run. **To settle it:** deliver one delegation end to end in phase 1.

**Needed but not OpenShell's (design change):**

- `inbox-delivery` finds its target session by running
  `AMAP_DELIVERY_SESSION_SOURCE`
  (amap-connector-claude `bin/inbox-delivery`, `find_claude_targets`).
- sandy's lister reads **tmux panes** and sandy's pane options
  (amap-deploy-sandy `payload/handoff-sessions`). Neither exists here.
- This deployment must ship its own lister. It emits the one `claude`
  process's row, using Claude Code's socket and key-file locations. See
  DESIGN.md P4.

## Q6. Network policy

- Can a sandbox's policy allow only the model endpoint and nothing else?
- Is inference routed through OpenShell's provider mechanism, and does that
  count as an egress rule?

### Answer (reading)

- The default policy has no network rules, so all egress is denied
  (`docs/how-it-works/policies/default-policy.mdx:42-47`).
- Attaching a provider adds that profile's endpoints, scoped to its
  `binaries`, to the effective policy. So yes, provider attachment is an egress
  rule (`default-policy.mdx:44-47`; `docs/how-it-works/inference.mdx:11-24`).
- A sandbox with only the `claude-code` provider attached allows the
  profile's Anthropic endpoints and nothing else.
- Network denials **are** logged, as `NET:OPEN ... DENIED` OCSF events
  (`logging.mdx:120-123`). So PLAN phase 1's "mail API call denied and
  logged" check can pass as written.

## Q7. Kubernetes (later)

- Do sandboxes on Kubernetes support shared volumes or sidecar containers?
- Is Landlock still enforced on a volume there?
- Which of OpenShell's plugin types (compute driver, isolation backend) would
  attach a volume?

### Answer: partly (reading)

- Existing PVCs can be mounted into the agent container, `read_only` by
  default (`runtimes.mdx:230-263`).
- The driver config names only an `agent` container
  (`crates/openshell-driver-kubernetes/src/driver.rs:417-418`). Reading found
  no user-defined sidecar.
- Unknown: whether Landlock is enforced on a PVC mount. By the Q1 reasoning it
  should be, since it is path-based, but nothing was run.
- Unknown: which plugin type would attach a volume beyond the built-in driver.
- Deferred to phase 3, as planned.

## Q8. Logs

- Where do the allow and deny decisions go?
- Could the router's decisions be sent to the same collector, as an optional
  extra?

### Answer: partly (reading)

- Decisions go to `openshell logs <name>`, the TUI, and inside the sandbox
  to `/var/log/openshell.YYYY-MM-DD.log`
  (`docs/observability/accessing-logs.mdx:9,61-87`).
- Opt-in OCSF JSONL is written to `openshell-ocsf.YYYY-MM-DD.log`
  (`docs/observability/ocsf-json-export.mdx:14,259-261`).
- Filesystem denials are not among them (Q1).
- Unknown: whether an external process such as the router can push into the
  same collector. `PushSandboxLogs` is a supervisor RPC
  (`proto/openshell.proto:539`), so it is not available to the router as-is.
  Deferred to phase 3.
