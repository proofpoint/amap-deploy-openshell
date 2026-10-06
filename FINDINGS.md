# Phase 0 findings

Date: 2026-09-28. Source: NVIDIA/OpenShell `main@acbac9c` and the release
`v0.1.2`, **read only**. Nothing was installed or run. The full answers, with
file and line citations, are in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md), and the
design changes are in [DESIGN.md](DESIGN.md).

## Answers

| Q | Status | One line |
|---|---|---|
| Q1 Host directories (blocking) | **answered: yes, with conditions**; two parts unknown | The Docker and Podman drivers take per-sandbox `bind` mounts with a `read_only` flag, which becomes a `:ro` mount the agent cannot remount. That works only after the operator sets `allow_driver_config`, `enable_bind_mounts` and `resource_admission.enabled = false` on the gateway. Landlock adds a second read-only layer on the same path. OpenShell does **not** log filesystem denials. Unknown until run: whether the host's writes appear live. The errno should be `EROFS` (the OpenShell agent's reading of the kernel), and L1 confirms it |
| Q2 Lifecycle hooks | **answered** | Gateway interceptors can bind to `CreateSandbox` and `DeleteSandbox` (modify, validate, post-commit). Post-commit is fail-open, so keep polling `sandbox list` or `WatchSandbox` as a backstop |
| Q3 Sandbox identity (blocking) | **answered**; one part unknown | A gateway-generated ID and a name (DNS-1123, at most 19 characters, unique per workspace). Both survive restart, the host can read them, and the agent cannot rename. A deleted name can be reused under a new ID, by design (OpenShell docs, confirmed by the OpenShell agent). The design pins `(workspace, name, id)` |
| Q4 Launch arguments | **answered**; one part unknown | The command after `--` is the main process, and `--env` reaches every process. The MCP config comes from the read-only mount. Unknown: how to set Claude Code's cross-session receiver setting so the agent can't change it (a Claude Code question) |
| Q5 Supervised side process (blocking) | **answered**; end-to-end unknown | OpenShell supervises only the main process, so the main process is our wrapper, and the wrapper restarts `inbox-delivery` itself. A whole-runtime `--restart-policy` exists on `main` only, not in `v0.1.2`. Seccomp and Landlock do not block `AF_UNIX`. We must ship our own session lister, because sandy's reads tmux. Unknown until a phase-1 delegation: injection end to end |
| Q6 Network policy | **answered** | Egress is denied by default, and attaching the `claude-code` provider adds only its endpoints. Network denials are logged. It needs a Console API key, not a subscription |
| Q7 Kubernetes | **partly answered; deferred** | PVC mounts are supported, and reading found no user sidecars. Whether Landlock applies on a PVC is unknown |
| Q8 Logs | **partly answered; deferred** | Decisions go to `openshell logs`, the TUI and in-sandbox log files, with optional OCSF JSONL. There is no documented way for the router to push into the same collector |

No finding for the other repos. The connector, the router and amap-spec need
no change. The session lister is host-supplied by the connector's own design
(`AMAP_DELIVERY_SESSION_SOURCE`), and the router's identity mounts of host
paths work as they do today.

## Design changes made

1. **The deny-log check can't pass as written.** DESIGN §5 and PLAN phase 1
   required a write to `inbound/` to be "denied by OpenShell and logged".
   OpenShell has no filesystem-activity events. The check is now: the write
   fails, and host state shows both the `:ro` mount and the Landlock
   `read_only` entry.
2. **Identity is pinned to the ID as well as the name** (P2), because names
   are reusable and unique only per workspace. One fleet uses one OpenShell
   workspace.
3. **P4 is a wrapper as the main process**, plus an OpenShell-specific session
   lister in the payload.
4. **New DESIGN §6, on the gateway configuration.** Enabling bind mounts
   switches off resource admission for the whole driver, so any gateway user
   could mount any host path, including another agent's outbox. Agents can't
   do it themselves. The mitigation is a `CreateSandbox` validate interceptor
   that limits bind sources to the sandbox's own lanes.

## Can phase 1 go ahead?

**Yes.** Q1 is not negative. Host directories can be shared, and the read-only
and read-write split holds, enforced by the mount flag with Landlock
underneath. No network transport is needed or proposed.

Conditions:

- **Use the Docker or Podman driver, on OpenShell `v0.1.2` or later**, with a
  dedicated gateway that one operator controls (DESIGN §6). The VM and
  Kubernetes drivers are out.
- **The phase-1 pass criteria have changed** (PLAN.md). Don't expect an
  OpenShell log line for the `inbound/` write.
- **Phase 1 must settle, by running:** the live visibility of router writes,
  the errno, name reuse, cross-session injection over `AF_UNIX`, and how the
  cross-session receiver setting is fixed. Each is an UNKNOWN until then, not
  a pass.
- **Phase 1 needs OpenShell installed on a host**, and a Console API key for
  the `claude-code` provider. Per the session instructions, installation waits
  for the operator's go-ahead.

## S9: gateway interceptors in OpenShell v0.1.2 (2026-10-05)

These come from reading OpenShell's source at the `v0.1.2` tag, which the
live host runs. None of it has been run yet. Every citation is
`v0.1.2:<path>:<lines>`. On each point, main at `e9271cb31` is the same or
differs only in logging.

1. **Transport.** The only contract is the gRPC service
   `openshell.gateway_interceptor.v1.GatewayInterceptor`, with `Describe`,
   `Evaluate` and `SnapshotProviderProfiles`
   (`proto/gateway_interceptor.proto:15-27`). There is no exec, HTTP/JSON or
   file route.
   - **Endpoints and startup.** An endpoint is `http://`, `https://` or
     `unix://` (`crates/openshell-core/src/config.rs:410-412`). If
     `Describe` fails at startup (default timeout 500 ms), the gateway does
     not start (`crates/openshell-gateway-interceptors/src/plan.rs:191-243`).
   - **Signing.** When the gateway signs extension JWTs (EdDSA), it needs
     `https://` or `unix://`, unless `allow_insecure_transport` is set
     (`crates/openshell-server/src/lib.rs:113-178`).
   - **The example.** OpenShell's example is Rust, using tonic, prost and
     jsonwebtoken (`examples/governance-interceptor/Cargo.toml`).
2. **Phases.** There are three: `modify_operation` (allow, deny or patch),
   `validate` (allow or deny) and `post_commit` (observe)
   (`proto/gateway_interceptor.proto:36-41`;
   `crates/openshell-gateway-interceptors/src/runtime.rs:142-153,414-420`).
   - `CreateSandbox` is one of 25 interceptable RPCs
     (`crates/openshell-gateway-interceptors/src/routes.rs:17-43`).
   - A `validate` binding sees the request after every patch (proto:72).
3. **Visibility.** `validate` receives the client's request as
   protobuf-JSON (`proposed_operation`, proto:71-76).
   - The only fields stripped are those marked secret, and none on this path
     is (`docs/extensibility/gateway-interceptors.mdx:122`). So
     `spec.template.driver_config`, `spec.environment`, `spec.policy` and
     `name` are all visible (`proto/openshell.proto:1017-1100,1242-1265`).
   - The Docker mount schema is in
     `crates/openshell-driver-docker/src/lib.rs:701-767`.
   - **The gap.** A request that names a `workload_template` gets the
     stored template's `driver_config` *after* interception
     (`crates/openshell-server/src/grpc/sandbox.rs:434-457,788-812`), and
     `CreateSandboxTemplate` is not interceptable. The rule must deny
     templated creates.
4. **After creation.** No public RPC changes a sandbox's driver mounts
   (`proto/openshell.proto:25-810`):
   - there is no `UpdateSandbox`;
   - `UpdateConfig` touches policy and settings only;
   - `StartSandbox` and `StopSandbox` carry only the name.

   With templated creates denied, binding `CreateSandbox` is enough.
5. **Failure handling.** `binding_policy` is `dynamic` (the default),
   `allowlist` or `exact`. Under `exact`, startup fails if the configured
   and declared bindings differ
   (`crates/openshell-gateway-interceptors/src/plan.rs:249-289,540-545`).
   - Under `allowlist` and `exact`, an unset `failure_policy` becomes
     `fail_closed`.
   - At runtime, `fail_closed` turns any failure into PERMISSION_DENIED
     (`runtime.rs:332-404`).
6. **Silent disablement.** Any of these leaves `CreateSandbox` unguarded
   with no error:
   - no interceptor entry;
   - a missing default config file
     (`crates/openshell-server/src/cli.rs:343-351`);
   - `allowlist` without the binding (only a warning, `plan.rs:546-551`);
   - a `dynamic` override with `disabled = true` (`plan.rs:823`);
   - `fail_open`.

   `openshell gateway info -o json` lists the negotiated interceptors but
   not their bindings (`crates/openshell-cli/src/main.rs:1376-1382`). Only a
   probe that is refused proves the binding is in force.
7. **Identity.** The sandbox being created is named by `name`, which is
   optional: an empty name is generated by the server
   (`proto/openshell.proto:1247-1248`). The rule must deny an empty name.
   The caller is the `principal` map
   (`crates/openshell-server/src/multiplex.rs:641-690`).
8. **Paths.** The gateway checks a bind source only for being absolute,
   non-empty, free of NUL and existing. It does not reject `..`, and it
   does not resolve symlinks
   (`crates/openshell-core/src/driver_mounts.rs:35-55`). The interceptor
   must resolve paths itself.
   - Binds become a `-v source:target:opts` string
     (`crates/openshell-driver-docker/src/lib.rs:3720-3779`), so a `:` in
     a source is rejected.
   - `volume` and `image` mounts carry a `source` too.
