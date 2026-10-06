# The CreateSandbox interceptor

**Status: implemented in S10, deployed by S10b, and run live on 2026-10-05
(docs/POC-REPORT.md, "L2 rerun with the mounts interceptor on"). Revised
2026-10-05 for D17-D19 and D24, and approved by the operator. A `[docs]` tag
below records where a claim was read; the live run's evidence is cited where it
settled an item.**

**Tags.** Every claim about OpenShell carries one of two tags.

- **[docs]** means *read in OpenShell's source or docs at v0.1.2, not run*.
  DESIGN.md writes `[code]` for a source-only read; here `[docs]` covers both,
  and always names the file and lines as `[docs: OpenShell v0.1.2:<path>:<lines>]`.
- **[unverified]** means assumed, and to be confirmed at L3.

Nothing here has run. No claim is tagged verified, and none may be upgraded
without cited evidence (a file, a page or command output).

Host paths are written `$AMAP_OPENSHELL_HOME/...`. Choices this document makes
where OpenShell leaves room are marked **[choice]**, each with its reason.

## 1. What it protects, and why it is needed

DESIGN.md section 6 explains the gap. With `resource_admission.enabled = false`
(the setting that lets a create carry `--driver-config-json` bind mounts), any
caller allowed to create sandboxes can bind any host path, including another
member's outbox, read-write. The Docker driver accepts the mount as given
[docs: OpenShell v0.1.2:crates/openshell-driver-docker/src/lib.rs:1049-1058].

The interceptor restores two properties at creation time: exclusivity of the
spool (P1, a member's lanes are its own) and the read-only split (P3, only the
member's own outbox is writable). It is deployment code of this repository, not
OpenShell code, and it runs on the host beside the gateway.

**What it is for, under this repository's scope (D17).** This repository deploys
onto one host with one operator (DESIGN.md "Scope"). Every caller of the
gateway acts for that operator. So the interceptor is not a boundary between
people. It protects the operator against their own mistakes, and against local
tooling that misbehaves: for example, a hand-typed `sandbox create`, or a
script, that mounts the wrong path. A wrong mount silently breaks spool
exclusivity, and `verify` would catch it only afterwards. The interceptor
refuses it at creation time. That is defence in depth. Turning it into a
boundary between operators is on DESIGN.md's "Widening the scope" list.

## 2. Language and dependencies (D5)

**The exception.** The interceptor is Python 3.9 or later, inside `interceptor/`
only. It is this repository's one exception to the standard-library rule (D5,
locked 2026-10-05). Everything outside `interceptor/` stays standard library
only.

**Run-time dependencies.** Exactly `grpcio` and `protobuf`, pinned with `==` and
`--hash=sha256:` in `interceptor/requirements.txt`.

**Generation-time dependency (D18, approved 2026-10-05).** `grpcio-tools`
regenerates the stubs. It is never imported at run time and is pinned in
`interceptor/requirements-gen.txt`. Reason: D5 says the stubs are generated from
OpenShell v0.1.2's protos, generating needs `protoc`, which `grpcio-tools`
carries, and generated stubs keep each OpenShell bump mechanical and testable.

**Vendored protos.** `gateway_interceptor.proto`, `extension.proto`,
`openshell.proto` and their imports, as of v0.1.2, are copied into
`interceptor/proto/`.

- `interceptor/proto/SOURCE` names the tag `v0.1.2` and each file's sha256.
- The generated `*_pb2.py` and `*_pb2_grpc.py` files go in `interceptor/_gen/`.
  Each header records the same sha256 values.

**Module split.** **[choice]** The split keeps the main suite's standard-library
rule and its never-a-smaller-set rule intact.

| Module | Imports | Role |
|---|---|---|
| `interceptor/rule.py` | standard library, and this repository's `render`, `policy`, `membership`, `l1_kit` | The decision |
| `interceptor/wire.py` | standard library only | The manifest, Describe and Evaluate on plain dicts, the gateway.toml fragment |
| `interceptor/server.py` | `grpc`, `google.protobuf`, `_gen` | A thin servicer: converts the Struct with `json_format.MessageToDict`, calls `wire`, builds `InterceptorResult` and `InterceptorManifest` from wire's dicts |

Outside `interceptor/`, nothing imports `interceptor/server.py`, `grpc` or
`google.protobuf`. `verify.py` may import `interceptor.wire` and
`interceptor.rule`, which are standard library only.

**JWT.** With gateway signing configured, a `unix://` interceptor receives a
bearer token [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:193-205].
Verifying EdDSA needs a library D5 did not approve, and the standard library has
no Ed25519. The resolution is consistent with D5's "a JWT is checked only if the
gateway signs one":

- **The gateway does sign interceptor calls by default (D24, 2026-10-05).** The
  first live run on the test host showed it: the packaged gateway logs
  `gateway-minted sandbox JWT enabled`, and its `Describe` carried a token.
  This document had assumed it did not.
- So the registration sets `allow_insecure_transport = true`. That is
  OpenShell's per-registration opt-out from extension authentication, and the
  gateway logs a warning about it at every start
  [docs: OpenShell v0.1.2:crates/openshell-server/src/lib.rs:113-140;
  OpenShell v0.1.2:crates/openshell-core/src/config.rs:420-425].
- If `Describe` arrives with `authorization` metadata, `wire` refuses it. Then
  the gateway does not start [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:218-234].
  So removing the opt-out fails loudly, and never quietly trusts a token that
  is not checked. V1 also FAILs without the opt-out line. This refusal is what
  surfaced the wrong assumption live: the gateway logged "the gateway sent
  authorization metadata: this interceptor does not verify tokens".
- The boundary is the `unix://` socket: mode 0600, in a 0700 directory (section 6).
- `expected_audience` is empty, which skips the audience check
  [docs: OpenShell v0.1.2:proto/gateway_interceptor.proto:107-112].

Approving a token verifier is an operator decision, listed in section 11.

## 3. What it binds: one RPC, one phase

**The selector.** One binding.

| Field | Value |
|---|---|
| `rpc` | `openshell.v1.OpenShell/CreateSandbox` |
| `phases` | `["validate"]` |
| `failure_policy` | `"fail_closed"` |

- `CreateSandbox` is interceptable: it is the first entry of the allowlist
  [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/routes.rs:17-43]
  (`"CreateSandbox"` is at line 18).
- Only unary, allowlisted methods of the OpenShell service can be bound
  [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/routes.rs:94-98].
- The phase enum value is `GATEWAY_INTERCEPTOR_PHASE_VALIDATE = 3`
  [docs: OpenShell v0.1.2:proto/gateway_interceptor.proto:36-41].

**Why `validate` and not `modify_operation`.** Every interceptor's modify phase
runs before any validate binding
[docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/runtime.rs:142-153].
So no other interceptor's patch can add a mount after the rule has run. A
validate binding cannot patch
[docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/runtime.rs:414-419],
and the rule needs no patch.

**Why not `post_commit`.** It must be fail-open and cannot deny
[docs: OpenShell v0.1.2:proto/gateway_interceptor.proto:132-134;
OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:135].

**What the validate binding can see.** It receives the operation as
protobuf-JSON in `proposed_operation`
[docs: OpenShell v0.1.2:proto/gateway_interceptor.proto:71-76], after every
modify patch. Only fields marked secret are removed
[docs: OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:122], and
`SandboxTemplate.driver_config` is not secret
[docs: OpenShell v0.1.2:proto/openshell.proto:1095]. So the rule sees the
driver config it must check. The one blind spot is a templated create, handled
by R1 in section 4.

**Deliberately not bound.** Cited to
[docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/routes.rs:17-43].

| RPC | routes.rs line | Why not bound |
|---|---|---|
| `DeleteSandbox` | :21 | Removes, never mounts. P5's lifecycle is handled by the tooling |
| `UpdateConfig` | :35 | Policy and settings, not mounts (section 8) |
| `AttachSandboxProvider` | :19 | Not a mount |
| `DetachSandboxProvider` | :20 | Not a mount |
| `ExposeService` | :23 | Not a mount |
| every provider RPC | | Not a mount |

Under `exact` (section 6), binding any of these would also have to be declared,
so the bound set is exactly one.

**The manifest.** Field values, per
[docs: OpenShell v0.1.2:proto/gateway_interceptor.proto:97-115,124-144]:

- `name`: `"amap-openshell-mounts"`
- `bindings`: `[{id: "create-sandbox-mounts", selector: {rpc: "openshell.v1.OpenShell/CreateSandbox"}, phases: [3], failure_policy: "fail_closed"}]`
- `failure_policy`: `"fail_closed"`
- `provider_profiles`: `false`
- `expected_audience`: `""`
- `extension`: `{protocol_version: {major: 1, minor: 0}, implementation_name: "amap-openshell/interceptor", implementation_version: <repo version string>, supported_capabilities: ["openshell.gateway-interceptor.contract"], required_capabilities: ["openshell.gateway-interceptor.contract"]}`
  [docs: OpenShell v0.1.2:crates/openshell-core/src/extension_protocol.rs:12-13,40-42,104-116,140-173].

**Describe refuses** with gRPC `FAILED_PRECONDITION`, and the gateway then does
not start [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:218-243],
when any of these holds:

- the gateway metadata is missing
  [docs: OpenShell v0.1.2:proto/gateway_interceptor.proto:30-32] ("Interceptors
  must reject unmet requirements");
- the gateway's major protocol version is not 1;
- the gateway does not support `openshell.gateway-interceptor.contract`;
- `authorization` metadata is present (section 2).

## 4. The validate rule

The rule's input is `proposed_operation` as a dict. Its output is
`Decision(allowed: bool, reasons: Tuple[str, ...])`. It collects every reason
and does not stop at the first.

**Field names.** The gateway serialises with prost-reflect's defaults: JSON
names, so lowerCamelCase, with default values omitted
[docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/proto_json.rs:83-96,116-124,168].
The exact spelling on the wire is still [unverified]. The live run
(2026-10-05) does not settle it: the rule's reasons name `driverConfig`
whichever spelling arrived. It is harmless, because the rule reads both
spellings and denies a request that carries both.

- **[choice]** The rule reads both spellings of every field it inspects
  (`workloadTemplate`/`workload_template`, `workspaceScope`/`workspace_scope`,
  `driverConfig`/`driver_config`, `allWorkspaces`/`all_workspaces`), and denies
  when both spellings of one field are present. Reason: the spelling is
  unverified, and a rule that reads one spelling would silently allow the other.
- Keys inside the `driver_config` Struct are verbatim (`read_only`, `type`,
  `source`, `target`), because a Struct keeps its keys
  [docs: OpenShell v0.1.2:proto/openshell.proto:1092-1095].

**Clauses**, in evaluation order. Each clause that fails adds one reason.

**R1. Templated creates are denied, always.** A non-empty `workloadTemplate`
denies. The stored template's driver config is merged after interception
[docs: OpenShell v0.1.2:crates/openshell-server/src/grpc/sandbox.rs:434-457,788-812],
and `CreateSandboxTemplate` is not interceptable
[docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/routes.rs:17-43].

**R2. Unknown fields are denied, on the path to the mounts.** Keys are
allowlisted, in both spellings, at three levels.

- `CreateSandboxRequest` [docs: OpenShell v0.1.2:proto/openshell.proto:1242-1265]:
  `workspaceScope, spec, name, labels, annotations, awaitMainProcessAttachment, workloadTemplate, requestId, serviceExposures`.
- `SandboxSpec` [docs: OpenShell v0.1.2:proto/openshell.proto:1017-1048]:
  `logLevel, environment, template, policy, providers, resourceRequirements, command, tty, providerAttachmentEpoch`.
- `SandboxTemplate` [docs: OpenShell v0.1.2:proto/openshell.proto:1068-1096]:
  `image, runtimeClassName, agentSocket, labels, annotations, environment, resources, userNamespaces, driverConfig`.

**[choice]** Fail closed: a field added by a newer gateway may carry a mount, so
the rule refuses it until reviewed.

**R3. The driver block.**

- `driverConfig`, when present, may have only the key `docker`, and its value
  must be an object.
- The gateway forwards only the active driver's block
  [docs: OpenShell v0.1.2:crates/openshell-server/src/compute/mod.rs:5026-5042].
  **[choice]** Any other driver key is denied, because D3 is Docker only, and an
  unchecked `podman` block would be live on a Podman gateway.
- Inside `docker`, the only allowed key is `mounts`, which must be a list.
  `cdi_devices` is denied; this deployment never renders it. The Docker schema is
  [docs: OpenShell v0.1.2:crates/openshell-driver-docker/src/lib.rs:701-710].

**R4. "Has mounts" gates identity.** A create with no `driverConfig`, or with an
empty `mounts` list, cannot touch the spool, so R5 to R7 do not apply.
**[choice]** Conditional, not unconditional: this is the minimal rule the step
asks for, and a create without mounts is outside this interceptor's job.

**R5. Identity (only with mounts).**

- `name` is non-empty and passes `membership.name_problem`. An empty name is
  generated by the server
  [docs: OpenShell v0.1.2:proto/openshell.proto:1246-1247].
- `name` is in `policy.named_instances(fleet)`. **[choice]** Only fleet members
  get lanes; this reinforces P5 at no extra cost.
- `workspaceScope.workspace` equals `policy.workspace_of(fleet)`. `allWorkspaces`
  or an absent scope denies. Names are unique only per workspace (DESIGN P2)
  [docs: OpenShell v0.1.2:proto/datamodel.proto:17-26].

**R6. Each mount.**

- It is an object whose `type` is `"bind"`. `volume`, `image` and `tmpfs` are
  denied [docs: OpenShell v0.1.2:crates/openshell-driver-docker/src/lib.rs:731-767],
  because this deployment renders only binds, and a volume or image source is a
  second path to host data.
- Its keys are a subset of `{type, source, target, read_only}`. `selinux_label`
  is denied, because `:z` and `:Z` relabel the host directory
  [docs: OpenShell v0.1.2:crates/openshell-driver-docker/src/lib.rs:731-741].
- `read_only`, when present, is a JSON bool. Absent means `true`
  [docs: OpenShell v0.1.2:crates/openshell-driver-docker/src/lib.rs:737-738,769-771].

**R7. Each bind source.** The rule reuses `render`.

- **(a)** `render.host_path_problem("bind source", source)` is `None`. The source
  must be absolute and normalised (no `..`, `.`, `//` or trailing `/`), with no
  `:`, no whitespace and no control characters. The gateway checks only absolute,
  non-empty, NUL and existence
  [docs: OpenShell v0.1.2:crates/openshell-core/src/driver_mounts.rs:34-55;
  OpenShell v0.1.2:crates/openshell-driver-docker/src/lib.rs:3745-3761].
- **(b)** **[choice]** The source must be exactly one of the rendered rows'
  sources for this name: `{row.source for row in render.member_rows(home, name)}`.
  That is the payload, the roster, and `instances/<name>/<lane>` for each lane in
  `router.config.LANES`. This is stricter than "lies under". A subtree below a
  lane is agent-writable (the outbox), so allowing it would invite a symlink
  swapped between validate and Docker's mount (a time-of-check-to-time-of-use
  race). Using render's own rows is D5's stated reason: the rule cannot drift from
  what `provision` renders.
- **(c)** The `read_only` flag equals that row's `read_only`. Only the own outbox
  may be read-write. **[choice]** Equality, not "at least as strict", for one
  simple invariant.
- **(d)** `os.path.realpath(source) == source` on the host. No component may be a
  symlink. This is defence in depth beside (b), and mirrors verify's `_real`.

**R8. Targets are not checked by the rule.** The gateway checks reserved roots
[docs: OpenShell v0.1.2:crates/openshell-core/src/container_paths.rs:32-41], and
`verify` checks the mount table. A target cannot break exclusivity, because
exclusivity is a property of sources.

**R9. Malformed input is a denial, never an exception.** A non-dict operation, a
non-list `mounts`, or a missing or invalid `fleet.json` each deny with a reason.
`fleet.json` is read on every evaluation: `policy.load_fleet(l1_kit.fleet_json(home))`.
**[choice]** No restart is needed after a fleet edit, and a broken file denies.

**The result.**

- Denied: `allowed=False`, `status_code="PERMISSION_DENIED"`,
  `reason = "amap-openshell interceptor: " + "; ".join(reasons)`, no patches.
- `log_annotations`: `{"amap.rule": "create-mounts", "amap.decision": "deny"|"allow"}`.
  Nothing secret goes into a reason or an annotation
  [docs: OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:164].
- A deny becomes the RPC's status before the handler runs
  [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/runtime.rs:258-267,428-430].

**Signatures for S10 to implement.**

```python
# render.py: S10 refactor. mount_table's rows, computed from home and name
# alone. mount_table checks the host, then returns member_rows(host.home, name).
# Every existing test passes unchanged.
def member_rows(home: str, name: str) -> Tuple[Mount, ...]: ...

# interceptor/rule.py (standard library only)
class Decision(NamedTuple):
    allowed: bool
    reasons: Tuple[str, ...]
REASON_PREFIX = "amap-openshell interceptor: "
def evaluate_create(operation: object, home: str) -> Decision: ...   # loads fleet.json itself (R9)
def check_create(operation: object, home: str, fleet: Mapping[str, Any]) -> Decision: ...  # pure

# interceptor/wire.py (standard library only)
INTERCEPTOR_NAME = "amap-openshell-mounts"
BINDING_ID = "create-sandbox-mounts"
CREATE_SANDBOX_RPC = "openshell.v1.OpenShell/CreateSandbox"
PHASE_VALIDATE = 3
CONTRACT = "openshell.gateway-interceptor.contract"
class Refused(Exception): ...                       # Describe refusal
def manifest(version: str) -> Dict[str, Any]: ...
def describe(gateway: Optional[Mapping[str, Any]], metadata: Mapping[str, str]) -> Dict[str, Any]: ...
def evaluate(evaluation: Mapping[str, Any], home: str) -> Dict[str, Any]: ...  # InterceptorResult as a dict
def socket_path(home: str) -> str: ...              # $AMAP_OPENSHELL_HOME/run/interceptor.sock
def gateway_fragment(home: str) -> str: ...         # the TOML of section 6
```

`evaluate` refuses (as a denial result) any RPC other than CreateSandbox and any
phase other than validate, and turns an exception from the rule into a denial.

## 5. A refused request: another member's outbox

The members are `alpha` and `beta` from `examples/fleet.json`, whose workspace is
`default`. `alpha` creates its sandbox but also asks for `beta`'s outbox,
read-write.

**1. The `proposed_operation` JSON.**

```json
{"workspaceScope": {"workspace": "default"}, "name": "alpha",
 "spec": {"template": {"image": "...", "driverConfig": {"docker": {"mounts": [
   {"type": "bind", "source": "$AMAP_OPENSHELL_HOME/payload", "target": "/opt/amap/payload", "read_only": true},
   {"type": "bind", "source": "$AMAP_OPENSHELL_HOME/instances/beta/outbox", "target": "/opt/amap/lanes/outbox", "read_only": false}]}}}}}
```

**2. The trace.** R1 passes (no template). R2 passes (every key is allowlisted).
R3 passes (one `docker` block, only `mounts`). R4: there are mounts, so identity
applies. R5 passes (`alpha` is a member, the workspace is `default`). R6 passes
(both are binds with only the four keys). R7 on the first mount passes. R7 on the
second mount: (a) passes, but **(b) fails**: `$AMAP_OPENSHELL_HOME/instances/beta/outbox`
is not one of alpha's rendered sources. (Because (b) fails, (c) and (d) add no
further reason for that mount.)

**3. The result, and what the caller sees.**

```json
{"allowed": false, "status_code": "PERMISSION_DENIED",
 "reason": "amap-openshell interceptor: bind source $AMAP_OPENSHELL_HOME/instances/beta/outbox is not one alpha may mount (...)"}
```

The gateway returns PERMISSION_DENIED with that reason, and no handler runs
[docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/runtime.rs:142-153,258-267;
OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:30,133]. How the
client renders the error text was settled live on the test host (2026-10-05). It prints
the reason in full, but wraps it, with a `│` before each continuation line
(section 11, item 4).

**Variants**, each refused by the named clause.

| Request | Refused by |
|---|---|
| source `.../instances/alpha/../beta/outbox` | R7a |
| beta's outbox with `read_only: true` | R7b |
| source `.../instances` or `.../instances/alpha` | R7b |
| a symlink `.../instances/alpha/outbox` pointing to beta's outbox | R7d |
| the same mounts under `"workspace": "other"` | R5 |
| the same mounts with `"name": "mallory"` | R5 |
| a `workloadTemplate` naming a template that holds beta's outbox | R1 |
| the same mount under a `podman` key | R3 |
| the same mount as `"type": "volume"` | R6 |

## 6. Fail-closed binding

The fragment S10's `wire.gateway_fragment(home)` renders. The rendered value of
`grpc_endpoint` is absolute; it is shown here with the placeholder.

```toml
[[openshell.gateway.interceptors]]
name           = "amap-openshell-mounts"
grpc_endpoint  = "unix://$AMAP_OPENSHELL_HOME/run/interceptor.sock"
failure_policy = "fail_closed"
binding_policy = "exact"
timeout        = "2s"

[[openshell.gateway.interceptors.bindings]]
rpc    = "openshell.v1.OpenShell/CreateSandbox"
phases = ["validate"]
```

**`binding_policy = "exact"`.**

- Startup fails if the configured and declared bindings differ, in RPC or in
  phases [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:249-289,501-505,540-545,669-684].
- Strict configuration cannot use `disabled = true`
  [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:569-573].
- Under strict policies the manifest's `failure_policy` is ignored, and only the
  configuration's counts; unset means `fail_closed`
  [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:258-261,686-689].
  That is why the fragment states `fail_closed` explicitly, and why verify
  refuses `fail_open`.

**`failure_policy = "fail_closed"`.** A timeout, transport error or invalid
result becomes PERMISSION_DENIED
[docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/runtime.rs:373-404;
OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:126-131].

**`timeout = "2s"`.** **[choice]** The default is 500 ms
[docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:207-210;
FINDINGS.md S9 item 1]. Python plus a `fleet.json` read per call needs headroom.
Under fail-closed a timeout is a refusal, never a pass. **Measured live on the test host
(2026-10-05):** the gateway logged `latency_ms=1` for each evaluation of
verify's probe, so the 2 s bound has ample headroom.

**The socket.** `unix://` endpoints are supported
[docs: OpenShell v0.1.2:crates/openshell-core/src/config.rs:410-412;
OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:98]. The socket goes
in `$AMAP_OPENSHELL_HOME/run/`, mode 0700, with the socket itself at 0600.
**[choice]** It sits outside every mount source (payload, roster, instances), so
no sandbox can see it.

**Startup order.** Start the interceptor before the gateway. If Describe fails,
the gateway does not start
[docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:191-243;
OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:98,160-162].
Registration is static, so the gateway must be restarted after a change
[docs: OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:102].

## 7. Misconfigurations that silently disable it, and how verify detects them

| Misconfiguration | Effect | Caught by |
|---|---|---|
| No `[[openshell.gateway.interceptors]]` entry | CreateSandbox is unguarded [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/runtime.rs:221-223] | V1, V2, V3 |
| The gateway loads another file, or the default file is absent [docs: OpenShell v0.1.2:crates/openshell-server/src/cli.rs:343-351] | V1 may read the wrong file | V2, V3 |
| `binding_policy = "allowlist"` without the CreateSandbox binding | Only a warning [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:546-551] | V1, V3 |
| `binding_policy = "dynamic"`, or unset (the default [docs: OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:66]), with an override `disabled = true` or narrowed phases [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:813-835] | The binding is off or narrowed | V1, V3 |
| `failure_policy = "fail_open"`, on the service or the binding | Skipped whenever the interceptor fails [docs: OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/runtime.rs:392-401; OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:131] | V1 (V3 cannot see it while the interceptor is up) |
| The gateway was not restarted after the change [docs: OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:102] | The old plan stays in force | V2, V3 |
| The interceptor process is down, under fail_closed | Not silent: every create is refused, and the gateway will not start | V3 reports FAIL with a distinct note |

The three checks are added to verify's `gateway` section. S10 implements them;
this document specifies them.

**V1: "gateway.toml registers the mounts interceptor as rendered".**

- It reads the same file as today's checks (`gateway.read_gateway_toml(opts.gateway_toml)`).
- New `gateway.interceptor_report(reading, home) -> List[Tuple[str, str, str]]`
  returns (claim, PRESENT|ABSENT|UNKNOWN, why). It groups each
  `[[openshell.gateway.interceptors]]` block with the
  `[[openshell.gateway.interceptors.bindings]]` blocks that follow it.
- It requires the block named `amap-openshell-mounts` to hold every line of
  `wire.gateway_fragment(home)`, comparing values with whitespace normalised.
- It requires that block to contain no `failure_policy = "fail_open"`, no
  `disabled = true`, and no second binding.
- An unreadable file is UNKNOWN. An absent file is FAIL.

**V2: "the running gateway negotiated the interceptor".**

- It runs `openshell gateway info -o json`.
- PASS when `extensions[]` has `kind == "gateway-interceptor"` and
  `configured_name == "amap-openshell-mounts"`
  [docs: OpenShell v0.1.2:crates/openshell-cli/src/commands/gateway.rs:456-464,520-547].
- The output carries no bindings, so V2 alone proves nothing
  [docs: OpenShell v0.1.2:crates/openshell-cli/src/commands/gateway.rs:538-546].
- A command failure or unparseable JSON is UNKNOWN.

**V3: "a create that mounts another member's outbox is refused by the
interceptor".** Only a refused probe proves the binding is in force.

- Probe name: `amap-verify-` plus 6 hex characters (`secrets.token_hex(3)`), 18
  characters, a DNS-1123 label.
- Its `--driver-config-json` binds `render.lane_dir(home, M, LANE_OUTBOX)`
  read-write at `/opt/amap/probe`, where M is the first fleet member. It also
  carries a poison key: `{"docker": {"mounts": [...], "amap_verify_probe": true}}`.
- argv: `openshell sandbox create --workspace W --name P --from <host.image> --driver-config-json J --detach --no-tty --no-auto-providers -- true`.

*Why the probe never creates anything.*

- With the interceptor in force, it is refused at validate (R3, R7b).
- Without it, the Docker driver's `deny_unknown_fields` rejects the poison key
  [docs: OpenShell v0.1.2:crates/openshell-driver-docker/src/lib.rs:701-703,1047-1048].
  That happens inside `validate_sandbox_create`
  [docs: OpenShell v0.1.2:crates/openshell-server/src/grpc/sandbox.rs:613-620],
  before the sandbox is persisted (`:646`).
- The CLI forwards arbitrary JSON unchanged
  [docs: OpenShell v0.1.2:crates/openshell-cli/src/run.rs:325-339].
- `--no-auto-providers` exists at v0.1.2
  [docs: OpenShell v0.1.2:crates/openshell-cli/src/main.rs:1542-1547], as does
  `--no-tty` (:1527-1532). `render.UNATTENDED_NEGATIONS` already relies on both.

*Verdicts.*

- PASS: the command fails, and its output contains `REASON_PREFIX` and M's
  outbox path.
- FAIL: the command failed without the prefix. The notes are "refused by the
  gateway, not the interceptor" or "the interceptor failed closed".
- FAIL: the command succeeded. The remedy names the sandbox to delete. Verify
  never deletes, because it has no `--apply`.
- UNKNOWN: `openshell` cannot run, or the fleet has no member.

*Expectations.* Every expected value is a `Fact` built through `Ctx.fact`,
because `tests/test_no_hardcoded_expectations.py` walks verify.py. The constants
live in `interceptor/wire.py` and are referenced from there.

## 8. RPCs that could change a sandbox's mounts after creation

**Verdict: none.** Binding `CreateSandbox` alone is enough, given R1. The table
cites OpenShell v0.1.2:proto/openshell.proto [docs] (the service block lists every
public RPC at lines 25-810, and none is named `UpdateSandbox` or `RestartSandbox`).

| RPC | Line | Why it cannot change mounts |
|---|---|---|
| (no `UpdateSandbox`, no `RestartSandbox`) | 25-810 | The full service list has neither |
| `StartSandbox` | 190; request 1443-1450 | Carries only a name |
| `StopSandbox` | 181; request 1433-1440 | Carries only a name |
| `UpdateConfig` | 452; request 2656-2697 | Policy and settings only. Sandbox scope may change only `network_policies` |
| `AttachSandboxProvider` | 142; request 1385-1400 | Carries a provider name |
| `DetachSandboxProvider` | 152; request 1402-1417 | Carries a provider name |
| `ExposeService` | 1754-1767 | Port and name |
| `CreateSandboxTemplate` | 92-100; `SandboxWorkloadTemplateSpec.driver_config` 1111-1118 | Not interceptable. Takes effect only through a templated `CreateSandbox`, which R1 denies |
| `BeginRootfsTarStaging` | 56-71 | Its token is placed in a later CreateSandbox's `driver_config`, which R3 limits to `mounts` |
| `ReportSandboxConfiguration` | 506-511; 2837-2852 | Sandbox credential only. Carries hashes and revisions, not mounts |

Internal compute-driver RPCs and direct database access are outside the public
API, and outside this document's scope.

## 9. How it runs, and what S10 builds

**Run command:** `python3 interceptor/server.py --home "$AMAP_OPENSHELL_HOME"`, in
a virtualenv built from `interceptor/requirements.txt`. The operator starts it
before the gateway.

**What S10 builds and runs.** S10 has network access, so it delivers everything,
generated and tested:

- `render.member_rows`;
- `interceptor/rule.py` and `interceptor/wire.py`;
- `gateway.interceptor_report`;
- verify's V1 to V3;
- the fragment printed by `gateway-config`, after the existing fragment;
- `interceptor/server.py`;
- `interceptor/proto/` with `SOURCE`;
- `interceptor/regen.sh`. It is a dry run by default, per the operator's
  preference for scripts. With `--apply` it builds a throwaway virtualenv
  from `requirements-gen.txt`, regenerates `_gen/`, and writes both lock
  files with hashes.

S10 runs `regen.sh --apply` itself, so the hash locks and `_gen/` are real
output, never hand-written. It also runs `interceptor/tests/` in a virtualenv
built from `requirements.txt`. Test 24 passes from S10 on.

**The home must be its own real path (D19).** R7(d) refuses any symlink in a
bind source, and every source is under `$AMAP_OPENSHELL_HOME`. So the home
must have no symlinked component. `install` refuses a home whose real path
differs, and names the real path to use. `verify`'s `_real` already assumes
this. S10 adds the `install` check and its test.

## 10. Test plan

S10's acceptance criteria are taken from here. Each test has exactly this
assertion.

**Main suite, `tests/test_interceptor_rule.py`** (standard library only; the
fixture is a tmp `$AMAP_OPENSHELL_HOME` with `examples/fleet.json` and real lane
directories):

1. `test_the_rendered_create_is_allowed`: for each member, an operation built from `render.driver_config(render.member_rows(home, name))`, the member's name and the fleet workspace is allowed with no reasons.
2. `test_another_members_outbox_is_refused`: section 5's request is denied, and the reasons name `instances/beta/outbox`.
3. `test_another_members_lane_read_only_is_refused`: beta's inbox, peer and outbox, each read-only, are each denied for alpha.
4. `test_paths_outside_the_rendered_rows_are_refused`: `/`, the home directory, `instances/`, `instances/alpha`, a path under `instances/alpha/outbox/`, and `/var/run/docker.sock` are each denied.
5. `test_unnormalized_sources_are_refused`: `..`, `.`, `//`, a trailing `/`, a relative path, `:`, a space and a control character are each denied.
6. `test_a_symlinked_source_is_refused`: alpha's outbox replaced by a symlink to beta's outbox is denied.
7. `test_read_only_must_match_the_rendering`: payload `false` is denied; inbox `false` is denied; outbox `true` is denied; absent on payload is allowed; absent on outbox is denied; `"true"` as a string is denied.
8. `test_a_templated_create_is_refused`: `workloadTemplate` and `workload_template` are each denied, with and without mounts.
9. `test_identity_is_required_with_mounts`: an empty name, a name outside the fleet, another workspace, `allWorkspaces` and an absent scope are each denied.
10. `test_a_create_without_mounts_is_allowed`: no `driverConfig`, `{"docker": {"mounts": []}}`, and an empty name with no mounts are each allowed.
11. `test_only_the_docker_block_is_accepted`: `podman`, `vm`, an unknown key and a non-object `docker` are each denied.
12. `test_unknown_keys_are_refused`: an extra key at the request, spec, template, docker and mount levels; `cdi_devices`; `selinux_label`; and types `volume`, `image`, `tmpfs`. Each is denied.
13. `test_both_spellings_are_read_alike`: snake_case and camelCase forms give the same decision. Both spellings of one field together are denied.
14. `test_every_reason_is_reported`: three violations give three reasons.
15. `test_malformed_input_is_a_denial`: a non-dict operation, a non-list `mounts`, a missing `fleet.json` and an invalid `fleet.json` each return `Decision(False, ...)` and never raise.
16. `test_the_rule_uses_renders_rows`: the allowed set equals `{r.source for r in render.member_rows(home, n)}`, and the read-only map equals the rows' map, for every member.

**Main suite, `tests/test_interceptor_wire.py`:**

17. `test_the_manifest_binds_create_sandbox_validate_only`: the manifest has every field value given in section 3.
18. `test_describe_refuses_an_incompatible_gateway`: missing metadata, major 2, a missing contract capability and present `authorization` metadata each raise `Refused`.
19. `test_evaluate_refuses_any_other_rpc_or_phase`: `DeleteSandbox`, `modify_operation`, `post_commit` and a wrong service each give a denial.
20. `test_a_denial_result`: `allowed` is False, `status_code` is `PERMISSION_DENIED`, the reason starts with `REASON_PREFIX`, there are no patches, and the annotation keys are exactly `amap.rule` and `amap.decision`.
21. `test_an_internal_error_is_a_denial`: a rule that raises gives a denial dict.
22. `test_the_fragment_matches_the_manifest`: the fragment's `rpc` and `phases` equal the manifest binding's; `binding_policy` is `"exact"`; `failure_policy` is `"fail_closed"`; the endpoint is `"unix://" + socket_path(home)`; the socket path is outside the payload, roster and instances directories.

**Main suite, the boundary:**

23. `test_only_server_imports_grpc` (`tests/test_interceptor_boundary.py`): an AST walk over every repository `.py` file except `interceptor/server.py`, `interceptor/_gen/` and `interceptor/tests/` finds no import of `grpc`, `google` or `_gen`. `rule.py` and `wire.py` import only the standard library and this repository's modules.
24. `test_requirements_are_hash_locked`: every requirement line in `interceptor/requirements.txt` and `requirements-gen.txt` is `name==version` with at least one `--hash=sha256:`. The top-level runtime names are exactly `{grpcio, protobuf}`, and the generation-time names include `grpcio-tools`. Transitive pins are allowed, hashed like the rest.

**Main suite, `tests/test_verify.py` additions** (a fake `openshell` through `fake_bin`):

25. `test_interceptor_checks_pass_when_registered_negotiated_and_refusing`: V1, V2 and V3 are each PASS.
26. `test_v1_fails_on_each_disabling_line`: a missing entry, `allowlist`, `dynamic` with `disabled = true`, `fail_open` on the service, `fail_open` on the binding, a phase other than `validate`, and an extra binding are each FAIL. An unreadable file is UNKNOWN.
27. `test_v2_fails_when_not_negotiated`: no extension is FAIL. A failed command or bad JSON is UNKNOWN.
28. `test_v3_verdicts`: an interceptor refusal is PASS; a gateway "unknown field" refusal is FAIL; "failed closed" text is FAIL; exit 0 is FAIL naming the sandbox, with no delete call recorded; an `openshell` that cannot run is UNKNOWN.
29. `test_the_probe_argv`: the name matches `^amap-verify-[0-9a-f]{6}$`; the JSON has the poison key and another member's outbox; `--from` is the host image; no `delete` is ever invoked.

**Separate run, `python3 -m pytest interceptor/tests`, in the interceptor
virtualenv.** Its conftest raises `pytest.UsageError` when `grpc` or
`google.protobuf` is missing. Never a skip.

30. `test_stubs_match_vendored_protos`: the sha256 values in the `_gen` headers equal those in `proto/SOURCE`.
31. `test_describe_over_unix_socket`: the server on a tmp socket returns `wire.manifest`.
32. `test_evaluate_refuses_another_members_outbox_over_unix_socket`: section 5's request is refused over the socket.
33. `test_socket_and_directory_modes`: the socket is 0600 and its directory 0700.

**Live, at L3, by the operator.**

- The interceptor is enabled and the gateway restarted.
- V1, V2 and V3 PASS.
- `provision` of each member still succeeds through the interceptor.
- A hand-made create of section 5's request is refused with the prefix.
- Latency stays under the 2 s timeout.
- This document's `[unverified]` tags are upgraded only with cited evidence.

## 11. Residual risks and open items

Under the scope (D17), the first two items are not risks. They are on
DESIGN.md's "Widening the scope" list, to be fixed before the scope widens.

1. A caller can create a correctly shaped sandbox for a member name that does
   not currently exist, such as a deprovisioned member. The interceptor does
   not use `principal`
   [docs: OpenShell v0.1.2:crates/openshell-server/src/multiplex.rs:641-698],
   and on this gateway every caller is unauthenticated, so `principal` is
   `unknown`, a fixed local-dev user, or an mTLS subject
   [docs: OpenShell v0.1.2:crates/openshell-server/src/multiplex.rs:876-985].
   Within scope the caller is the operator. On widening, the first hardening
   step is a membership gate: deny a create for a name that `membership.json`
   records, as `provision` already does. Then add a principal check once the
   gateway authenticates callers. P2's `(workspace, name, id)` pin in verify
   catches an ID change meanwhile.
2. A global-scope `UpdateConfig` replaces the policy for all sandboxes
   [docs: OpenShell v0.1.2:proto/openshell.proto:2660-2667]. It could touch
   Landlock, the second layer, but not the `:ro` mount. Within scope only the
   operator sends it. On widening, add a `validate` binding on `UpdateConfig`.
   Whether static fields change is [unverified].
3. JWT verification needs a dependency D5 did not approve. Approving a verifier
   is an operator decision (section 2), and it is needed only on widening.
4. [unverified] items:
   - the camelCase field naming on the wire;
   - how the CLI renders a PERMISSION_DENIED reason. **Settled live on the test host,
     2026-10-05:** the reason is printed in full, but wrapped to the width of
     the terminal, or narrower off one, breaking at spaces and after hyphens,
     with a `│` before each continuation line. V3 now matches with whitespace
     and the box characters removed.
   - Python latency under 2 s. **Settled live on the test host, 2026-10-05:**
     `latency_ms=1` per evaluation.
   - whether the gateway and the interceptor resolve paths in the same mount
     namespace. **Settled for this deployment, live on the test host, 2026-10-05:** the
     interceptor allowed both members' provisions (`evaluate allow`) on the
     sources it computes from the host's home. Docker then mounted exactly
     those sources: verify's "the container mounts the rendered table" PASSed
     for both members.
   - **Also settled live, 2026-10-05:** after `systemctl --user restart
     amap-openshell-interceptor`, the running gateway reached the new process
     without a restart of its own. verify's two V3 probes were each evaluated
     `decision="deny"`, and verify exited 0.
