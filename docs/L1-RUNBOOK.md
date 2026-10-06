# L1 runbook: the proof of concept on a live host

**Status: unsupported until verified.** Nothing in this repository has run
against a live OpenShell yet. This runbook is the procedure that verifies it. It
settles PLAN.md phase 1 and the L1 unknowns of IMPLEMENTATION-PLAN.md, and its
deliverable is `docs/POC-REPORT.md`, made from the template in step 9. Every
OpenShell claim in DESIGN.md stays `[docs]` or `[unverified]` until step 8
records evidence for it. UNKNOWN is never a pass: a check you could not run is
reported as UNKNOWN.

The scenario is the one in PLAN.md phase 1: two agents, alpha and beta, in two
OpenShell sandboxes, with the router on the host. Alpha may task beta. Beta may
not task alpha. It is the same demo as amap-deploy-sandy's tutorial, so the two
compare directly.

The kit writes files and never runs a program. It does not run `openshell` or
`docker`, and it does not invent a sandbox ID. Every command that touches
OpenShell, Docker or the router is yours to run, in the order below.

## Scripted run

`l1-run` runs steps 0 to 9 below as one command. Unlike the kit, it runs
`openshell`, `docker` and the router, so read the steps once before you use it,
and first set `AMAP_OPENSHELL_HOME`, `CLAUDE_CODE_VERSION` and
`ANTHROPIC_API_KEY` (step 0). It finds amap-router-local, amap-connector-claude
and amap-spec beside this repository, where `siblings --apply` puts them. Their
variables are needed only when a checkout is not beside this one. Without `--apply` it prints each command
and runs nothing:

```sh
python3 amap-openshell.py l1-run
python3 amap-openshell.py l1-run --apply
python3 amap-openshell.py l1-run --from 7 --apply
python3 amap-openshell.py l1-run --only 8 --apply
```

- **Gates.** Step 0 always runs: it checks the variables, reports where it found each sibling checkout, the key (never
  printed) and D6's posture as the gateway's file states it. Step 2, the live
  posture, runs whenever the selection includes a step from 3 to 8, even with
  `--from` or `--only`. If either fails, the runner refuses to go on.
- **Evidence.** Every command it runs is recorded under
  `$AMAP_OPENSHELL_HOME/evidence/step-<N>/`, with its argv, output, exit status
  and time. The key and any messaging token are redacted. A record is written
  once and never rewritten.
- **The report.** Step 9 drafts `$AMAP_OPENSHELL_HOME/evidence/POC-REPORT.md`
  from the evidence. Read every line, fill in the columns and tags it leaves to
  you, and then move it to `docs/POC-REPORT.md`.
- **The provider profile.** Step 3 also reads `node`'s and `claude`'s real paths
  from the built image, renders
  `$AMAP_OPENSHELL_HOME/providers/amap-claude-code.json` from them, lints it,
  and imports it. When the gateway already has a copy that differs, it updates
  that copy instead. It never deletes a profile or a provider. A rerun imports
  nothing.
- **Reruns.** A step that is already done is skipped, with a note. A rerun
  after a complete run changes nothing but the evidence it adds.
- **What it never deletes.** No sandbox except the throwaway probe of Unknown 3
  that it created itself, no lane, and no router container. It never edits or
  restarts the gateway.
- **Exit codes.** 0 means every selected step completed and every check is
  PASS. 1 means a step failed or the runner refused. 3 means every step
  completed but some check is FAIL or UNKNOWN: UNKNOWN is never a pass.
- **Where its evidence differs from the manual commands.** The lister cannot
  be run through `sandbox exec` as step 8 shows: the supervisor starts an exec'd
  process itself, so it is not a descendant of the main process and the lister
  refuses it. The runner loads the lister as a module and asks it for the
  sessions instead. And the router removes each outcome file once it has read
  it, so there is nothing to list in the outbox. The runner reads the router's
  audit log, which the agents cannot write, for the outcomes.

## 0. Before you start

**Prerequisites (gate G3).**

- Linux 6.2 or later (Landlock ABI 3), with Docker. Docker is the only compute
  driver this deployment uses (D3).
- OpenShell `v0.1.2` or later, installed with your approval.
- A Console API key (D10) for this deployment's provider, `amap-claude-code`,
  exported as `ANTHROPIC_API_KEY` in the terminal that creates each sandbox.
- The router image, which step 6 builds from the amap-router-local checkout.
- The Claude Code version you have measured and recorded. There is no default.

**Accepted gateway posture (D6, Decision 1).** Bind mounts need the gateway's
admission checks off (DESIGN.md section 6), so any caller that may create a
sandbox on this gateway can mount any host path the container runtime can see.
That is acceptable only if all of these hold, and step 2 checks each one:

- the gateway is dedicated to this work and reachable on loopback only;
- it has no OIDC, and no other user holds its credentials;
- no other sandboxes run on it.

If the host is shared, stop: the interceptor of DESIGN.md section 6 has to come
first. Record the posture you accepted in the report.

**Variables.** Set these in every terminal you use:

```sh
export AMAP_OPENSHELL_HOME="<absolute path of a new directory>"
export CLAUDE_CODE_VERSION="<the Claude Code version you measured>"
```

**The sibling checkouts.** `l1-run` finds amap-router-local,
amap-connector-claude and amap-spec (or `.amap-spec`) beside this repository or
beside one of its parent directories, and step 0 reports where it found each.
These variables are needed only when a checkout is not beside this one. When
one is set and not empty, it is the only place searched:

```sh
export AMAP_ROUTER_REPO="<absolute path of the amap-router-local checkout>"
export AMAP_CONNECTOR_REPO="<absolute path of the amap-connector-claude checkout>"
export AMAP_SPEC_DIR="<absolute path of the amap-spec directory>"
```

The lines you run by hand in steps 6, 8 and 9 spell the checkouts as
`$AMAP_ROUTER_REPO` and `$AMAP_SPEC_DIR`. Set those two before you run those
lines yourself.

`AMAP_OPENSHELL_HOME` must not be inside, or contain, this repository or any of
the sibling checkouts. The kit refuses it if it is. The parent of the directory
must exist.

**Every command runs from this repository's root.** Change to it first. Host
paths are always rooted at one of the variables above, or are relative to this
repository. Paths inside a sandbox are absolute, and placeholders look like
`<file>`.

**Where the flags come from.** Each OpenShell subcommand below is cited at
OpenShell main@acbac9c, and its flags are checked against
crates/openshell-cli/src/main.rs at that revision. The fleet's workspace is
`default`, from `examples/fleet.json`, and every command names it with the
global `--workspace` flag (main.rs:466-475), so an `OPENSHELL_WORKSPACE`
variable cannot move it.

Record the OpenShell version, which the report needs (`--version` is global,
main.rs:497-499):

```sh
openshell --version
```

## 1. Build the image

The recipe is `image/Dockerfile` (D4). Its build arguments are the Claude Code
version and the one uid:gid (D7): the account in the image is the same uid:gid
as the router container and every sandbox's `run_as_user`. Use your own.

```sh
docker build --build-arg CLAUDE_CODE_VERSION="$CLAUDE_CODE_VERSION" --build-arg SANDBOX_UID="$(id -u)" --build-arg SANDBOX_GID="$(id -g)" -t amap-openshell-agent image/
```

The build refuses an empty, non-numeric or zero uid or gid. The tag
`amap-openshell-agent` is what step 3 passes as `--image`.

The claude flags the sandboxes use (`--settings` and
`--dangerously-skip-permissions`) were read from Claude Code 2.1.284's
`claude --help`. If you pin another version, check that its `claude --help`
lists both.

## 2. Configure the gateway

If you built the host with `examples/vms/proxmox/provision-guest.sh`, it has
already written this file, restarted the gateway and registered it. Run the
checks below anyway.

Merge the fragment into the gateway's configuration file. The fragment is DESIGN.md
section 6. Step 3 writes it to `$AMAP_OPENSHELL_HOME/gateway-fragment.toml` for
reference, and its text is in `l1_kit.py`. The gateway reads
`$XDG_CONFIG_HOME/openshell/gateway.toml` when neither `--config` nor
`OPENSHELL_GATEWAY_CONFIG` is set. For the Debian/Ubuntu package's systemd user
service that is usually .config/openshell/gateway.toml in your home directory
(OpenShell main@acbac9c:docs/how-it-works/gateways/configuration.mdx:11 and
32). The commands below fall back to .config in your home directory when
`XDG_CONFIG_HOME` is unset, as it usually is on Ubuntu:

```sh
GATEWAY_TOML="${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateway.toml"
mkdir -p "$(dirname "$GATEWAY_TOML")"
```

The file needs the schema version and the Docker driver selected, and then the
fragment's tables:

```toml
[openshell]
version = 2

[openshell.gateway]
compute_driver = "docker"

[openshell.drivers.docker]
allow_driver_config = true
enable_bind_mounts = true

[openshell.drivers.docker.resource_admission]
enabled = false
```

The schema version is configuration.mdx:47-48 and 69-70, and the driver
selector is configuration.mdx:66. The three driver settings are OpenShell
main@acbac9c:docs/how-it-works/sandboxes/runtimes.mdx:120-131. If your file
already has other tables, keep them and add only what is missing.

Check the D6 posture before you restart the gateway:

```sh
grep -n "bind_address" "$GATEWAY_TOML"
grep -n "oidc" "$GATEWAY_TOML"
```

Neither command should print a line. With no `bind_address`, the gateway uses
its built-in `127.0.0.1:17670` listener, which is loopback
(configuration.mdx:36-38). If either prints a line, read it: a non-loopback
address or an OIDC table breaks the accepted posture.

Validate the file and restart the gateway. The systemd user service is the
package default (configuration.mdx:1219-1223). If yours is started another way,
restart it the way you started it:

```sh
openshell-gateway config preflight --path "$GATEWAY_TOML"
systemctl --user restart openshell-gateway
```

If `openshell status` then reports no active gateway, the CLI never registered
the package's gateway. That happens when the gateway wasn't running while the
installer ran. Register it: with `--local` and this name, the CLI collects its
client certificate from the package's TLS directory (OpenShell
main@acbac9c:docs/how-it-works/gateways/authentication.mdx:52):

```sh
openshell gateway add https://127.0.0.1:17670 --local --name openshell
openshell status
```

Then check that the listener is loopback and that no other sandbox exists.
`sandbox list` is OpenShell main@acbac9c:crates/openshell-cli/src/main.rs:1610:

```sh
ss -ltn
openshell --workspace default sandbox list
```

The listener on port 17670 must be `127.0.0.1`, and the list must be empty. If
either is not true, stop and record why.

## 3. Prepare the host and import the provider profile

`l1-kit prepare` writes everything the host needs before any sandbox exists,
under `$AMAP_OPENSHELL_HOME`: the payload (copied byte for byte from the
connector checkout and this repository), every member's lanes, each member's
policy and create command, the fleet policy and the gateway fragment. It
creates `$AMAP_OPENSHELL_HOME/roster` empty. OpenShell refuses a bind mount
whose source does not exist (OpenShell
main@acbac9c:crates/openshell-driver-docker/src/lib.rs:3753-3761), so this
comes first.

It is a dry run without `--apply`. Read the dry run first:

```sh
python3 amap-openshell.py l1-kit prepare --home "$AMAP_OPENSHELL_HOME" --fleet examples/fleet.json --run-as "$(id -u):$(id -g)" --image amap-openshell-agent
```

Every path line starts with `would create`. Then write:

```sh
python3 amap-openshell.py l1-kit prepare --home "$AMAP_OPENSHELL_HOME" --fleet examples/fleet.json --run-as "$(id -u):$(id -g)" --image amap-openshell-agent --apply
```

`--run-as` is the same uid:gid as the image's build arguments in step 1. The
kit renders it into both policies. It refuses root.

**`--restart-policy`.** OpenShell `v0.1.2` has no `--restart-policy` flag on
`sandbox create`, and `main` has one (OpenShell
main@acbac9c:crates/openshell-cli/src/main.rs:1538-1544). The default is the
command that works on both. If `openshell sandbox create --help` lists
`--restart-policy` on your host and you want it, add `--restart-policy` to the
command above. The kit then puts `--restart-policy on-failure` in each create
command. Running the command again is safe: it reports `present` for what is
already right and `would update` for what differs.

### Import this deployment's provider profile

Step 4's `--provider amap-claude-code` names a profile this deployment ships in
`providers/amap-claude-code.json`. OpenShell does not load its own example
profile (OpenShell main@acbac9c:providers/claude-code.yaml:4-6), and it would not
load this one by itself either: a profile must be imported. And a
rule's `binaries` match the real path of the executable that opens the
connection, which for an npm-installed Claude Code is not `/usr/bin/claude`
(OpenShell main@acbac9c:docs/how-it-works/policies/network-rules.mdx:57-77). So
the shipped file has no binaries, and the kit renders a copy with the image's
real paths. Read those from the built image, and pass each as `--binary`:

```sh
docker run --rm --network none --entrypoint sh amap-openshell-agent -c 'printf "node=%s\nclaude=%s\n" "$(readlink -f "$(command -v node)")" "$(readlink -f "$(command -v claude)")"'
python3 amap-openshell.py l1-kit profile --home "$AMAP_OPENSHELL_HOME" --binary "<node path>" --binary "<claude path>" --apply
openshell --workspace default provider profile lint -f "$AMAP_OPENSHELL_HOME/providers/amap-claude-code.json"
openshell --workspace default provider list-profiles -o json
openshell --workspace default provider profile import -f "$AMAP_OPENSHELL_HOME/providers/amap-claude-code.json"
```

Leave out `--apply` on the kit line for a dry run. Import the profile only when
`list-profiles` does not show `amap-claude-code`. The import is create-only
(OpenShell main@acbac9c:docs/how-it-works/providers/profiles.mdx:308, 317). When
the listing shows a copy that differs, add that copy's `resource_version` to a
copy of the rendered file and update it. The gateway refuses an update without a
non-zero `resource_version`
(OpenShell main@acbac9c:crates/openshell-server/src/grpc/provider.rs:2894-2908):

```sh
openshell --workspace default provider profile update amap-claude-code -f <file with resource_version>
```

The profile goes into the fleet's workspace, not the global scope, which needs
Platform Admin (profiles.mdx:247). Every subcommand and flag above is at
OpenShell main@acbac9c and at `v0.1.2`, on the same lines of
crates/openshell-cli/src/main.rs: `provider` 583-585, `provider list-profiles
-o/--output` 917-927 (dispatch 3948-3957; `v0.1.2` 3938-3947), `provider
profile` 929-931, `provider profile import -f` 1108-1126, `provider profile
update <id> -f` 1128-1141, `provider profile lint -f` 1143-1161, and the `json`
output format 766-771. The implementations are
crates/openshell-cli/src/commands/provider.rs:1590-1615 and 1701-1830, also
identical at `v0.1.2`.

L1 must confirm two things, and neither is a new check. First, that after an
import the listing's fields compare equal to the file, so a rerun does not
update every time. Second, which of the two listed binaries opens the
connection.

## 4. Create each sandbox

Each member has a one-line create command in
`$AMAP_OPENSHELL_HOME/commands/create-<name>.sh`. Read it once. It is the
`openshell sandbox create` argument vector from the renderer, unchanged.

A script can run it with no terminal, because it carries three flags. `--detach`
returns once the sandbox is ready instead of attaching to it. `--tty` gives the
main process a terminal whatever the caller has. `--auto-providers` creates the
`amap-claude-code` provider without asking. They are OpenShell
main@acbac9c:crates/openshell-cli/src/main.rs:1523-1555 (also v0.1.2,
main.rs:1523-1547). `--detach` is run.rs:649-653 and 1165-1172 and
docs/how-it-works/sandboxes/overview.mdx:29-33, `--tty` is run.rs:641-642 and
675, and `--auto-providers` is commands/provider.rs:481-497. The provider is
created from `ANTHROPIC_API_KEY` without a question
(OpenShell main@acbac9c:docs/how-it-works/providers/overview.mdx:344-356).
That provider is created from the profile imported in step 3 and from
`ANTHROPIC_API_KEY` in the creating terminal (commands/provider.rs:413-447 and
508-521; crates/openshell-providers/src/discovery.rs:33-63). The settings file's
`env` switches off Claude Code's non-essential traffic (Claude Code CHANGELOG
2.0.17, 2.1.105 and 2.1.120), and the profile allows only the Anthropic API
either way.

Each command returns when its sandbox is ready, so both run from one terminal.
The main process is the payload's wrapper, which runs `claude` with
`--settings /opt/amap/payload/claude-settings.json` and bypass permissions (D8).
Before it starts anything, the wrapper seeds Claude Code's first-run answers
(D9) into `/sandbox/.claude.json`.

```sh
test -n "$ANTHROPIC_API_KEY" || echo "set ANTHROPIC_API_KEY first"
sh "$AMAP_OPENSHELL_HOME/commands/create-alpha.sh"
sh "$AMAP_OPENSHELL_HOME/commands/create-beta.sh"
```

Attach with `sandbox connect` to look at a session:

```sh
openshell --workspace default sandbox connect alpha
```

If Claude Code is showing any question, D9's seed missed it. Write down exactly
what it asked, answer it, and put it in the report as a finding for D9.

Ctrl-D and Ctrl-P Ctrl-Q both disconnect without ending the main process
(OpenShell main@acbac9c:docs/how-it-works/sandboxes/overview.mdx:263-272).
`sandbox connect` is main.rs:1730. If a create fails, read its message and stop.
Do not go on with one sandbox missing.

## 5. Record each sandbox's ID

`sandbox get --output json` prints the sandbox's `id`, `name` and `workspace`
(OpenShell main@acbac9c:crates/openshell-cli/src/run.rs:2789-2791 and
docs/how-it-works/sandboxes/overview.mdx:603-607; the command is main.rs:1594-1606).
The snippet below asserts the name and the workspace before it prints the ID, so
an ID from the wrong sandbox is refused. Then `l1-kit record` writes it into
`membership.json`:

```sh
ALPHA_ID=$(openshell --workspace default sandbox get alpha --output json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["name"]=="alpha" and d["workspace"]=="default"; print(d["id"])')
python3 amap-openshell.py l1-kit record --home "$AMAP_OPENSHELL_HOME" alpha "$ALPHA_ID" --apply
```

```sh
BETA_ID=$(openshell --workspace default sandbox get beta --output json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["name"]=="beta" and d["workspace"]=="default"; print(d["id"])')
python3 amap-openshell.py l1-kit record --home "$AMAP_OPENSHELL_HOME" beta "$BETA_ID" --apply
```

Leave off `--apply` to see what would be written. After alpha, only
`membership.json` exists. After beta, every member is recorded, and the kit also
writes `selected.json` (the router's discovery verdict) and `router.json`, and
creates `$AMAP_OPENSHELL_HOME/router-state`. Neither file exists before then, so
the router never sees a partial fleet.

The record is pinned to the ID: OpenShell reuses a deleted sandbox's name under a
new ID, and `record` refuses the new ID with an `ID mismatch` message. If you
delete and re-create a sandbox, that is a different sandbox. The kit never
deletes a file, so remove `$AMAP_OPENSHELL_HOME/membership.json` yourself and
record every member again.

## 6. Start the router

The router starts last, so its first sight of each instance follows the
sandboxes. Its `$AMAP_ROUTER_REPO/docker/run.sh` derives the container's mounts
from `router.json` and refuses a `state_dir` that does not exist, which is why
`record` created `$AMAP_OPENSHELL_HOME/router-state`. The container runs as your
uid:gid (amap-router-local docker/run.sh:81), the same one as every sandbox (D7).

```sh
"$AMAP_ROUTER_REPO/docker/build.sh"
"$AMAP_ROUTER_REPO/docker/run.sh" --config "$AMAP_OPENSHELL_HOME/router.json" --detach
```

Check that it runs, and read its log for any refusal:

```sh
docker ps --filter name=amap-router-local
docker logs amap-router-local
```

## 7. Delegate from alpha to beta

Attach to both agents in two terminals (`sandbox connect alpha` and
`sandbox connect beta`, as in step 4). Ask alpha:

> Read `/opt/amap/roster/roster.json` and find beta's address. Then ask beta,
> through `inbox-submit`, to list the files in its workspace and report back.

Alpha reads the roster, which the router publishes to
`$AMAP_OPENSHELL_HOME/roster/<roster-file>` and every sandbox sees read-only. Alpha
calls `submit`, which writes an inert request into its outbox. The router checks
its graph, binds the sender to alpha's outbox, and places a notice in beta's
read-only peer lane. Beta's delivery daemon injects the request into beta's
session, where it arrives as a turn from the runtime-asserted sender. Beta
replies with `submit`, the router binds the reply to alpha, and it arrives in
alpha's session the same way. Alpha can check the router's verdict with
`submit_result`.

A delegation that is delivered and answered is the only proof that the whole
chain works. Confirm that it was injected into beta's session, and not only
found in its inbox: beta's terminal shows it as a turn, without beta polling.

## 8. Check the pass criteria and settle the unknowns

Record, for each item, the command you ran, its output, and one outcome: PASS,
FAIL or UNKNOWN. Put each into the report of step 9.

### Pass criteria (PLAN.md phase 1)

#### Pass criterion 1

> `amap-spec/fixtures/validate.py` gates every document the connector reads and
> writes;

Run `check_document` from the amap-spec validator on one live document of each
class the run produced. This shell function loads `validate.py` from
`$AMAP_SPEC_DIR` and prints PASS or FAIL with every error:

```sh
check() {  # usage: check <fixture-name> <file>; the name's prefix picks the schema
  PYTHONDONTWRITEBYTECODE=1 python3 - "$AMAP_SPEC_DIR" "$1" "$2" <<'EOF'
import importlib.util, json, os, sys
spec_dir, name, path = sys.argv[1:4]
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("validate", os.path.join(spec_dir, "fixtures", "validate.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with open(path, encoding="utf-8") as fh:
    errors = mod.check_document(name, json.load(fh))
print(("FAIL " if errors else "PASS ") + name + "".join("\n  " + e for e in errors))
sys.exit(1 if errors else 0)
EOF
}
```

Then, one document of each class from the delegation of step 7. The request is
in its `processed` directory once the router has handled it, and in the outbox root while it
waits:

```sh
check roster-live.json "$AMAP_OPENSHELL_HOME/roster/<roster-file>"
check request-live.json "$AMAP_OPENSHELL_HOME/instances/alpha/outbox/processed/req-<id>.json"
check result-live.json "$AMAP_OPENSHELL_HOME/instances/alpha/outbox/results/<file>"
check peer-live.json "$AMAP_OPENSHELL_HOME/instances/beta/peer/notices/notice-<id>.json"
check message-live.json "$AMAP_OPENSHELL_HOME/instances/beta/peer/messages/notice-<id>.json"
```

The schema for each class is picked by the fixture name's prefix. A class the
run did not produce (the mail-lane `notice-`, `directory-` and `identity-`
documents) is UNKNOWN in the report, not PASS. Say which check ran on which
schema: this is the validator's document-level check, and it says nothing about
the runtime-enforced items in step 9.

#### Pass criterion 2

> the connector's operational checks pass;

These are the operational checks that `$AMAP_SPEC_DIR/fixtures/validate.py` prescribes
for what no document validator can see. The run above exercises them: the
connector ran against inbound trees it cannot write. Confirm each on the live
run:

- the delivery daemon started and relayed the pending notice with read-only
  lanes: beta's outcome files say `delivered` (the outcomes are under beta's
  outbox);
- the body was resolved: beta's session read the message, and the reply exists;
- outbound worked with no inbound write: alpha's `submit_result` says the
  request was accepted;
- exactly one `claude` session is visible to the lister, which refuses to guess
  between two.

```sh
find "$AMAP_OPENSHELL_HOME/instances/beta/outbox" -type f
openshell --workspace default sandbox exec --name beta -- python3 /opt/amap/payload/openshell-sessions
```

`sandbox exec` is OpenShell main@acbac9c:crates/openshell-cli/src/main.rs:1681-1727.
The lister prints one row per `claude` process, and nothing when there is none.

#### Pass criterion 3

> a write to `inbound/` from inside the sandbox is denied, and the host shows
> both layers: the `:ro` mount and the Landlock `read_only` entry. OpenShell
> logs no filesystem denials, so there is no log line to expect (DESIGN.md §5);

Try the write from inside alpha. It must fail with a read-only file system:

```sh
openshell --workspace default sandbox exec --name alpha -- touch /opt/amap/lanes/inbox/probe
```

Then show both layers on the host. First the mount flag. Find alpha's container
with `docker ps`, and list its mounts and whether each is writable:

```sh
docker ps --format '{{.Names}} {{.Image}}'
docker inspect --format '{{range .Mounts}}{{.Source}} {{.Destination}} {{.RW}}{{println}}{{end}}' "<alpha container>"
```

`/opt/amap/lanes/inbox` must show `RW` false, and so must the peer lane, the
payload and the roster. Only `/opt/amap/lanes/outbox` shows true. Then the
Landlock entry, from the policy the gateway holds
(OpenShell main@acbac9c:docs/how-it-works/sandboxes/overview.mdx:612):

```sh
openshell --workspace default sandbox get alpha --policy-only
```

`/opt/amap/lanes/inbox` must be under `read_only`, and `/opt/amap/lanes/outbox`
under `read_write`. OpenShell logs no filesystem denials, so no log line is
expected: the failed write and the two listings are the evidence.

#### Pass criterion 4

> a direct network call to a mail API from inside the sandbox is denied and
> logged;

Make the call from inside alpha, to a host that is not a provider endpoint
(`example.org` is reserved, so it is never a real mail API). Then read the
sandbox's log. `logs` is OpenShell
main@acbac9c:crates/openshell-cli/src/main.rs:531-556, and `--source`, `--level`
and `-n` are its flags:

```sh
openshell --workspace default sandbox exec --name alpha -- python3 -c 'import urllib.request; urllib.request.urlopen("https://mail.example.org", timeout=15)'
openshell --workspace default logs alpha --source sandbox --level info -n 100
```

The call must fail, and the log must show the denial of `mail.example.org`. A
policy with no network rules allows egress only through the provider, so the
denial is the expected result. If the call succeeds, that is a FAIL.

#### Pass criterion 5

> beta tasking alpha (never granted) is held at the router.

Beta was never granted the edge to alpha. In beta's session, ask beta to start a
new task for alpha, with `inbox-submit` (not a reply to alpha's delegation: a
reply is bound to the sender and does reach alpha). The connector writes the
request, because it holds no allowlist, and the router decides. Then, in beta's
session, call `submit_result` for that request. It must report that the request
was held. Read the router's own log too:

```sh
docker logs amap-router-local
find "$AMAP_OPENSHELL_HOME/instances/alpha/peer/notices" -type f
```

The listing must show no new notice for the request. Copy the router's line for
the held request into the report.

### Unknowns (IMPLEMENTATION-PLAN.md, L1)

#### Unknown 1

> Router writes appear inside a running sandbox (Q1).

After step 7 the router has written into beta's peer lane, after beta started.
Show the notice from inside beta and on the host, and compare them:

```sh
openshell --workspace default sandbox exec --name beta -- ls -l /opt/amap/lanes/peer/notices
ls -l "$AMAP_OPENSHELL_HOME/instances/beta/peer/notices"
```

If the sandbox lists the notice the host shows, the answer is yes. If it does
not, the router's writes do not appear, and that is a FAIL for the design.

#### Unknown 2

> The errno of an agent's write to `inbox/` is `EROFS`, as expected from
> the OpenShell agent's reading of the kernel (Q1).

Attempt a write into the inbox lane from inside alpha and read the error name.
Python prints it, so the errno is not a guess:

```sh
openshell --workspace default sandbox exec --name alpha -- python3 -c 'import os; os.open("/opt/amap/lanes/inbox/probe", os.O_WRONLY | os.O_CREAT)'
```

`Errno 30` is `EROFS`, the read-only mount. `Errno 13` is `EACCES`, which would
mean Landlock denied it first. Record which one it is. If it is not `EROFS`,
say so: the expectation came from a reading of the kernel, and this is the check.

#### Unknown 3

> A lane that is mounted but missing from the policy is unreadable (Q1).

Use a throwaway sandbox named `probe`, so alpha and beta are untouched. Copy
`$AMAP_OPENSHELL_HOME/policies/alpha.yaml` to
`$AMAP_OPENSHELL_HOME/policies/<probe>.yaml` and delete the
`/opt/amap/lanes/peer` line from its `read_only` list. Copy
`$AMAP_OPENSHELL_HOME/commands/create-alpha.sh` to
`$AMAP_OPENSHELL_HOME/commands/create-<probe>.sh`, and in the copy: change
`--name alpha` to `--name probe`, point `--policy` at the edited file, and
replace everything after ` -- ` with `sleep 3600`. The copy already has
`--detach` and `--tty`. The mount of
`/opt/amap/lanes/peer` stays in `--driver-config-json`: the lane is mounted and
absent from the policy. Then:

```sh
sh "$AMAP_OPENSHELL_HOME/commands/create-<probe>.sh"
openshell --workspace default sandbox exec --name probe -- ls /opt/amap/lanes/peer
openshell --workspace default sandbox exec --name probe -- ls /opt/amap/lanes/inbox
openshell --workspace default sandbox delete probe
```

The first `ls` must fail (`Permission denied`) and the second must succeed. Only
alpha's lanes are mounted, and the probe is not a fleet member, so the router
never sees it. `sandbox delete` is main.rs:1642.

#### Unknown 4

> Injection works over `AF_UNIX` (Q5). **Also test a negative case**: set
> the receiver to refuse, send one notice, and confirm the outcome is
> `refused` or `held`, not `delivered`. A socket directory the receiver
> doesn't recognise produces no receipt, and the daemon counts silence as
> `delivered` (amap-connector-claude's maintainers).

The positive case is step 7: beta's terminal shows the delegation as a turn
from the runtime-asserted sender, and beta's outcome file says `delivered`. Read
it, and name its path in the report:

```sh
find "$AMAP_OPENSHELL_HOME/instances/beta/outbox" -type f
```

The lister's socket is the one Claude Code reported to the payload's
SessionStart hook. Its directory must be under `/tmp` or `/sandbox`, because the
daemon binds its reply socket beside it. Its key file is the session's own peer
key, `/sandbox/.claude/sessions/<pid>.<id>.key`, which Claude Code writes. The
hook records only the socket, the pid and the start time, never the messaging
token.

The negative case proves the outcome is not just silence. Set beta's receiver to
refuse, so a socket that answers nothing cannot be counted as delivered. The
setting is `crossSessionInbound`, and Claude Code reads it at launch. Write it
into beta, restart beta, and send one delegation from alpha again:

```sh
openshell --workspace default sandbox exec --name beta -- python3 -c 'import json; open("/sandbox/.claude/settings.local.json", "w").write(json.dumps({"crossSessionInbound": "refuse"}))'
openshell --workspace default sandbox stop beta
openshell --workspace default sandbox start beta
openshell --workspace default sandbox connect beta
```

`sandbox stop` and `sandbox start` are main.rs:1654 and 1662. Whether a stopped
sandbox keeps `/sandbox` and starts the main process again is itself unverified:
record what you saw. Then repeat step 7 once and read beta's outcome file. It
must say `refused` or `held`, and never `delivered`. Alpha's `submit_result`
and `docker logs amap-router-local` show what the sender was told. If the
outcome is `delivered` with the setting on refuse, the daemon counted silence as
receipt, and that is a FAIL. When done, put beta back:

```sh
openshell --workspace default sandbox exec --name beta -- rm /sandbox/.claude/settings.local.json
openshell --workspace default sandbox stop beta
openshell --workspace default sandbox start beta
```

#### Unknown 5

> How Claude Code's cross-session receiver setting is fixed to `accept` in a
> place the agent cannot write (Q4). Read sandy's
> `verify_cross_session_inbound` for the two files involved.

`--settings /opt/amap/payload/claude-settings.json` sets `crossSessionInbound`
to `accept` from the read-only payload. Per Claude Code's docs, the project and
local settings files can only tighten it. So the agent can hold or refuse its
own delegations through the writable `/sandbox/.claude/settings.local.json`, but
cannot undo the operator's value.

**What L1 must still confirm.**

1. Step 7 is delivered, which needs `accept` from the flag file.
2. Unknown 4's negative case, which tightens through `settings.local.json`,
   gives `refused` or `held`.
3. Whether `/sandbox/.claude/settings.json`, which the agent can also write,
   changes the value. The docs name only the project and local files.

List the files from inside beta, and check whether the agent's user can write
them:

```sh
openshell --workspace default sandbox exec --name beta -- ls -la /sandbox/.claude
openshell --workspace default sandbox exec --name beta -- touch /sandbox/.claude/probe
openshell --workspace default sandbox exec --name beta -- rm /sandbox/.claude/probe
```

#### Unknown 6

> The accepted gateway posture (Decision 1) is written into the report.

Fill in the posture line of the report from what step 2 showed: the gateway is
dedicated, loopback only, has no OIDC and no other user, and runs no other
sandbox. The report also states the risk that was accepted: any caller that may
create a sandbox on this gateway can bind-mount any host path the container
runtime can see, including another agent's outbox. Attach the output of the
checks of step 2.

#### Unknown 7

> The shared uid holds end to end. The router (`--user`), the lane
> directories and every sandbox's `run_as_user` are the same uid, and the
> agent can read a delivered 0600 notice.

Show each place the uid appears, and compare them. All of them must be one
uid:gid, and it must be yours:

```sh
id -u
id -g
openshell --workspace default sandbox exec --name alpha -- id
openshell --workspace default sandbox exec --name beta -- id
stat -c '%u:%g %a' "$AMAP_OPENSHELL_HOME/instances/alpha/outbox" "$AMAP_OPENSHELL_HOME/instances/beta/peer/notices"
docker inspect --format '{{.Config.User}}' amap-router-local
grep run_as "$AMAP_OPENSHELL_HOME/policies/alpha.yaml" "$AMAP_OPENSHELL_HOME/policies/beta.yaml"
```

Then the read: the router writes notices with mode 0600, and the agent must be
able to read one. From inside beta:

```sh
openshell --workspace default sandbox exec --name beta -- stat -c '%u:%g %a' /opt/amap/lanes/peer/notices/notice-<id>.json
openshell --workspace default sandbox exec --name beta -- cat /opt/amap/lanes/peer/messages/notice-<id>.json
```

The mode must be 600, owned by the same uid:gid, and `cat` must print the
document. If the agent cannot read a notice the router wrote, the shared uid
does not hold end to end: FAIL.

## 9. Write the report

Copy the template below to `docs/POC-REPORT.md` and fill it in. Nothing is
filled in for you: every outcome is one you observed. The NOT-CHECKED table has
one row for each item in the "NOT CHECKED HERE" list of
`$AMAP_SPEC_DIR/fixtures/validate.py`.
List the items from your spec checkout, so the table follows the version you
tested against, and do not copy them from anywhere else:

```sh
python3 -c 'import sys; t = open(sys.argv[1], encoding="utf-8").read(); print(t[t.index("NOT CHECKED HERE"):].split("\n\n")[0])' "$AMAP_SPEC_DIR/fixtures/validate.py"
```

For each item, mark exactly one of three columns: enforced by OpenShell, still
operational, or not applicable, and give the evidence. An item you did not
check is UNKNOWN, and it goes in the evidence column, not in a column of the
three.

```markdown
# POC-REPORT: amap-deploy-openshell, L1

Date: <date>
Host: <kernel version, Docker version>
OpenShell: <version, from the version command>
Claude Code: <version>
Fleet: <the fleet policy used, with its task graph>

Accepted gateway posture (D6, Decision 1): <the accepted posture: a dedicated
gateway, loopback only, no OIDC, one operator, no other sandboxes; and the risk
accepted: any caller that may create a sandbox on it can bind-mount any host
path the container runtime can see>

Every outcome below is PASS, FAIL or UNKNOWN. UNKNOWN is not a pass.

## What worked

<A short write-up: what ran, what was delivered and answered, what did not.>

## Pass criteria

### Pass criterion 1: validate.py gates every document
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <the check lines, the classes checked, and the classes not produced>

### Pass criterion 2: the connector's operational checks
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <outcome files, the lister's row, submit_result>

### Pass criterion 3: a write to the inbox lane is denied at both layers
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <the failed write, the mount listing, the policy listing>

### Pass criterion 4: a network call to a mail API is denied and logged
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <the failed call and the log line>

### Pass criterion 5: beta tasking alpha is held at the router
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <submit_result, the router's log line, the empty peer lane>

## Unknowns

### Unknown 1: router writes appear inside a running sandbox
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <both listings>

### Unknown 2: the errno of a write to the inbox lane
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <the errno seen: EROFS or another>

### Unknown 3: a lane mounted but missing from the policy
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <both listings from the probe sandbox>

### Unknown 4: injection over AF_UNIX, and the refusing receiver
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <the delivered outcome, and the refused or held outcome>

### Unknown 5: where the cross-session receiver setting is fixed
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <the files, and whether the agent can write them>

### Unknown 6: the accepted gateway posture
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <the outputs of the posture checks>

### Unknown 7: the shared uid holds end to end
Outcome: <PASS | FAIL | UNKNOWN>
Evidence: <every uid seen, and the read of a notice>

## NOT-CHECKED mapping

One row for each item in the NOT CHECKED HERE list of the amap-spec validator,
filled in during L1. Mark one column with X.

| NOT-CHECKED item | enforced by OpenShell | still operational | not applicable | evidence |
|---|---|---|---|---|
| <item> | | | | <evidence, or UNKNOWN> |

## DESIGN.md tags

<Each tag moved to verified, with the evidence that moved it. A claim the run did
not show stays as it was.>
```
