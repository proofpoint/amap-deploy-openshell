# AMAP on OpenShell: the runbook

**Status: verified live on one host, under the scope in the README.** These
have run on a fresh VM and passed ([docs/POC-REPORT.md](POC-REPORT.md)):
- the proof of concept;
- the full bring-up with the tooling below;
- the rerun with the mounts interceptor on.

This is the hands-on half. It sets up two Claude Code agents, `alpha` and
`beta`, each in its own OpenShell sandbox, with the router on the host. Alpha
may task beta. Beta may not task alpha. It is the same demo as
amap-deploy-sandy's runbook, so the two compare directly. Then it breaks each
guarantee on purpose so you can see where it is enforced.

This page is the operator's procedure. It uses the verbs of
`amap-openshell.py`. [docs/L1-RUNBOOK.md](L1-RUNBOOK.md) is the proof-of-concept
procedure this replaces for an operator, and every `openshell` and `docker`
command here is one that runbook already shows and cites. What the live run
already settled is in [docs/POC-REPORT.md](POC-REPORT.md), and each experiment
in step 11 cites it.

1. [What you need](#1-what-you-need)
2. [Get the pieces](#2-get-the-pieces)
3. [Build the image](#3-build-the-image)
4. [Configure the gateway](#4-configure-the-gateway)
5. [Install](#5-install)
6. [Import the provider profile](#6-import-the-provider-profile)
7. [Provision the two agents](#7-provision-the-two-agents)
8. [Start the router](#8-start-the-router)
9. [Verify](#9-verify)
10. [Send your first delegation](#10-send-your-first-delegation)
11. [Break it on purpose](#11-break-it-on-purpose)

## 1. What you need

- Linux 6.2 or later (Landlock ABI 3), with Docker. Docker is the only compute
  driver (D3).
- OpenShell `v0.1.2` or later.
- Python 3.9 or later, standard library only.
- The sibling checkouts amap-router-local, amap-connector-claude,
  amap-deploy-sandy and amap-spec. Step 2 sets them up. amap-deploy-sandy is
  not optional: this repository imports the pure core of sandy's
  `fleet_policy.py`, so every verb but `siblings` needs it.
- A Console API key (D10) for this deployment's provider profile
  `amap-claude-code`, exported as `ANTHROPIC_API_KEY` in the terminal that
  provisions. Never print it.
- The Claude Code version you have measured. There is no default.

**Accepted gateway posture (D6).** Bind mounts need the gateway's admission
checks off (DESIGN.md section 6), so any caller that may create a sandbox on
this gateway can mount any host path the container runtime can see. That is
acceptable only if the gateway is dedicated to this work and reachable on
loopback only, it has no OIDC, one operator holds its credentials, and no other
sandboxes run on it. If the host is shared, stop: the interceptor of DESIGN.md
section 6 has to come first.

Set these in every terminal you use:

```sh
export AMAP_OPENSHELL_HOME="<absolute path of a new directory>"
export CLAUDE_CODE_VERSION="<the Claude Code version you measured>"
```

The verbs, `bring-up` and `l1-run` among them, find the sibling checkouts beside
this repository, where step 2 puts them. The variables are needed only when a
checkout is not beside this one. When one is set and not empty, it is the only
place searched:

```sh
export AMAP_ROUTER_REPO="<absolute path of the amap-router-local checkout>"
export AMAP_CONNECTOR_REPO="<absolute path of the amap-connector-claude checkout>"
export AMAP_SANDY_REPO="<absolute path of the amap-deploy-sandy checkout>"
export AMAP_SPEC_DIR="<absolute path of the amap-spec directory>"
```

Step 8's lines spell the router checkout as `$AMAP_ROUTER_REPO`. Set it before
you run them by hand. `bring-up` does not need it.

You need the export only until `install --apply` (or `provision-guest.sh
--home`) has run. Both record the home, and every later command finds it
without the variable (D25).

`AMAP_OPENSHELL_HOME` must not be inside, or contain, this repository or any
sibling checkout. **Every command runs from this repository's root.**

## 2. Get the pieces

This repository comes first:

```sh
git clone https://github.com/proofpoint/amap-deploy-openshell
cd amap-deploy-openshell
```

On a host that should not reach GitHub, push it from another machine to a bare
repository on the host and clone that instead
([examples/vms/proxmox/README.md](../examples/vms/proxmox/README.md), D11).
Then, from this repository's root:

```sh
python3 amap-openshell.py siblings
python3 amap-openshell.py siblings --apply
```

`siblings` reads `siblings.json`. It clones each sibling beside this
repository, or fetches it if it is already there, and checks out its pin.
amap-spec is not pinned: it is cloned or fast-forwarded, and its commit is
printed. It refuses a checkout with local changes, naming it. Without
`--apply` it changes nothing. It needs no sibling to run.

| Sibling | Pin |
|---|---|
| amap-router-local | `e43dbba` |
| amap-connector-claude | `37875a5` |
| amap-deploy-sandy | `94a6372` |
| amap-spec | not pinned |

This repository finds the siblings by looking beside itself; the variables
of step 1 override that. You do not build the connector: `install` copies its
binaries onto the read-only payload.

## 3. Build the image

**Steps 3 to 9 in one command.** `bring-up` does steps 3 and 5 to 9 in order, and step 4's `gateway-config` check. Merge the gateway fragment first (step 4): `bring-up` stops at that check when `gateway.toml` lacks a setting, and it never edits `gateway.toml` or restarts the gateway, so step 4's merge, restart and listener checks stay by hand. Its preflight also refuses when the mounts interceptor is not accepting on its socket (step 4). Steps 10 and 11, and every break-it experiment, stay by hand too. It is a dry run without `--apply`. With `--apply` it skips whatever is already done, so it can be rerun. Its last line is `AMAP is up` only when `verify` exits 0; otherwise it names the stage that stopped it. The key comes from `$ANTHROPIC_API_KEY`, or from one line of stdin with `--api-key-stdin`, and is needed only when a member must be created.

```sh
python3 amap-openshell.py bring-up --claude-code-version "$CLAUDE_CODE_VERSION"
python3 amap-openshell.py bring-up --claude-code-version "$CLAUDE_CODE_VERSION" --apply
```

To see each piece, follow steps 3 to 9 one at a time instead.

The recipe is `image/Dockerfile` (D4). Its build arguments are the Claude Code
version and the one uid:gid (D7): the account in the image is the same uid:gid
as the router container and every sandbox's `run_as_user`.

```sh
docker build --build-arg CLAUDE_CODE_VERSION="$CLAUDE_CODE_VERSION" --build-arg SANDBOX_UID="$(id -u)" --build-arg SANDBOX_GID="$(id -g)" -t amap-openshell-agent image/
```

The tag `amap-openshell-agent` is what `--image` defaults to.

## 4. Configure the gateway

`gateway-config` is read-only. It prints the fragment this deployment needs and
reports each of its three settings as PRESENT, ABSENT or UNKNOWN. UNKNOWN (an
unreadable file, for one) is never a pass.

```sh
python3 amap-openshell.py gateway-config
```

Merge the fragment into the gateway's file, check the file, and restart the
gateway:

```sh
GATEWAY_TOML="${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateway.toml"
mkdir -p "$(dirname "$GATEWAY_TOML")"
openshell-gateway config preflight --path "$GATEWAY_TOML"
systemctl --user restart openshell-gateway
```

Then check that the listener is loopback and that no other sandbox exists:

```sh
openshell status
ss -ltn
openshell --workspace default sandbox list
```

The listener on port 17670 must be `127.0.0.1`, and the list must be empty.

### The mounts interceptor

The second block of `gateway-config --home` registers the mounts interceptor
`fail_closed`, so the gateway does not start unless `interceptor/server.py`
already listens (docs/INTERCEPTOR.md). On the example VM,
`examples/vms/proxmox/provision-guest.sh --home "$AMAP_OPENSHELL_HOME" --apply`
builds the virtualenv from `interceptor/requirements.txt` with
`pip install --require-hashes --only-binary=:all:`, installs a systemd user unit
ordered before the gateway, merges the block and restarts the gateway. On
another host, run its dry run and do what it prints. It ran live on 2026-10-05
(POC-REPORT, "L2 rerun with the mounts interceptor on").

```sh
examples/vms/proxmox/provision-guest.sh --home "$AMAP_OPENSHELL_HOME"
examples/vms/proxmox/provision-guest.sh --home "$AMAP_OPENSHELL_HOME" --apply
```

## 5. Install

`install` is a dry run until you add `--apply`:

```sh
python3 amap-openshell.py install
python3 amap-openshell.py install --apply
```

For a first install with a base you control, give the base in place of the
plain `install --apply` (the base applies only to a new `fleet.json`, so a
second run does not change it):

```sh
python3 amap-openshell.py install --apply --fleet-domain-base agents.example.org
```

It seeds `fleet.json` from `examples/fleet.json`, writes the payload byte for
byte from the connector and this repository, and creates `roster` and
`instances` empty. `install` gives the new `fleet.json` the domain
`openshell.<host>.internal`, and `--fleet-domain-base` sets the base. Every
address is `<name>@` that domain, so `install` never changes it in an existing
`fleet.json`. Edit `task_graph` and `workspace` in
`$AMAP_OPENSHELL_HOME/fleet.json`, then run `install --apply` again. The shipped
example is already the alpha and beta fleet.

`router.json`, `selected.json` and `router-state` appear only once every member
is recorded, so the router never sees a partial fleet.

## 6. Import the provider profile

Read the real binary paths from the image:

```sh
docker run --rm --network none --entrypoint sh amap-openshell-agent -c 'printf "node=%s\nclaude=%s\n" "$(readlink -f "$(command -v node)")" "$(readlink -f "$(command -v claude)")"'
```

Render the profile with those two paths, then lint and import it:

```sh
python3 amap-openshell.py l1-kit profile --home "$AMAP_OPENSHELL_HOME" --binary "<node path>" --binary "<claude path>" --apply
openshell --workspace default provider profile lint -f "$AMAP_OPENSHELL_HOME/providers/amap-claude-code.json"
openshell --workspace default provider list-profiles -o json
openshell --workspace default provider profile import -f "$AMAP_OPENSHELL_HOME/providers/amap-claude-code.json"
```

If the list already holds a differing copy, update it instead:

```sh
openshell --workspace default provider profile update amap-claude-code -f <file with resource_version>
```

This comes before step 7: `sandbox create --auto-providers` builds the
provider from this profile.

## 7. Provision the two agents

```sh
test -n "$ANTHROPIC_API_KEY" || echo "set ANTHROPIC_API_KEY first"
python3 amap-openshell.py provision alpha
```

The dry run prints the create command and says it would record the ID. Then:

```sh
python3 amap-openshell.py provision alpha --apply
python3 amap-openshell.py provision beta --apply
```

`--apply` does this, in order: it renders the policy and the create command,
creates the lanes, runs `openshell sandbox create`, reads the ID back with
`sandbox get --output json`, and records `(workspace, name, id)` in
`membership.json`. `provision` refuses a name whose sandbox exists under a
different ID, and tells you to `deprovision` first.

```sh
python3 amap-openshell.py list
```

Each line must read `member`.

## 8. Start the router

```sh
"$AMAP_ROUTER_REPO/docker/build.sh"
"$AMAP_ROUTER_REPO/docker/run.sh" --config "$AMAP_OPENSHELL_HOME/router.json" --detach
docker ps --filter name=amap-router-local
docker logs amap-router-local
```

**Starting the router is the approval.** Its first poll of each instance is
first sight, and it never delivers anything staged before then. Keep
`router-state` on persistent storage.

## 9. Verify

```sh
python3 amap-openshell.py verify
```

It has six sections: install, router-config, gateway, members, the router's
container, and the router's health. The member section asks for each member:

- that the sandbox exists and matches its `(workspace, name, id)` record;
- that the effective policy has the lanes and no mail egress;
- that the container's mount table shows `ro` and `rw` as rendered, and no
  source is shared across the fleet;
- that a write to `inbox` from inside the sandbox fails with `EROFS`;
- that `inbox-delivery` is running.

The write probe uses a scratch name and removes it whatever the outcome. Every
check is PASS, FAIL or UNKNOWN, and UNKNOWN is never a pass: `verify` exits 1
for either. Continue only once it exits 0.

## 10. Send your first delegation

Attach to both agents in two terminals:

```sh
openshell --workspace default sandbox connect alpha
openshell --workspace default sandbox connect beta
```

Ask alpha:

> Read `/opt/amap/roster/roster.json` and find beta's address. Then ask beta,
> through `inbox-submit`, to list the files in its workspace and report back.

Alpha reads the roster, which the router publishes and every sandbox sees
read-only. Alpha calls `submit`, which writes an inert request into its outbox.
The router checks its graph, binds the sender to alpha's outbox, and places a
notice in beta's read-only peer lane. Beta's delivery daemon injects the request
into beta's session as a turn from the runtime-asserted sender. Beta replies,
the router binds the reply to alpha, and it arrives in alpha's session the same
way. Alpha can check the router's verdict with `submit_result`.

A delegation that is delivered and answered is the only proof that the whole
chain works. It must arrive in beta's session as a turn, not be found by
polling.

## 11. Break it on purpose

Each experiment breaks one guarantee, predicts what you will see, cites the
live run that settled it, and puts things back. None damages the fleet.

### The agent cannot write its own lanes

Try to create a file in alpha's read-only inbox lane:

```sh
openshell --workspace default sandbox exec --name alpha -- touch /opt/amap/lanes/inbox/probe
openshell --workspace default sandbox exec --name alpha -- python3 -c 'import os; os.open("/opt/amap/lanes/inbox/probe", os.O_WRONLY | os.O_CREAT)'
```

Predicted: `Read-only file system`. The Python call raises `Errno 30`, which is
`EROFS`, not `EACCES`. Cited: `docs/POC-REPORT.md`, Unknown 2 and Pass
criterion 3. Nothing to put back: no file was created.

### Both layers, not one

The boundary is the read-only mount and the Landlock entry:

```sh
openshell --workspace default sandbox get alpha --policy-only
docker ps --format '{{.Names}} {{.Image}}'
docker inspect --format '{{range .Mounts}}{{.Source}} {{.Destination}} {{.RW}}{{println}}{{end}}' "<alpha container>"
```

Predicted: `inbox` and `peer` under `read_only` in the policy and `RW` false in
the mounts; only `outbox` under `read_write` and `RW` true. A write to either lane fails with the same `Read-only file system` as above.
No OpenShell log line is expected for a filesystem denial. Cited: `docs/POC-REPORT.md`, Pass
criterion 3, and FINDINGS.md. Read-only, nothing to put back.

### A delegation edge is directed

Ask beta to start a **new** task for alpha, then to call `submit_result`. Then:

```sh
docker logs amap-router-local
find "$AMAP_OPENSHELL_HOME/instances/alpha/peer/notices" -type f
```

Predicted: the router's log shows `queued_for_human` with reason
`recipient_not_allowlisted`, and there is no notice in alpha's peer lane. A
reply still reaches alpha, because a reply is bound to the sender. Cited:
`docs/POC-REPORT.md`, Pass criterion 5. Nothing to put back.

### The sandbox has no way out but the provider

```sh
openshell --workspace default sandbox exec --name alpha -- python3 -c 'import urllib.request; urllib.request.urlopen("https://mail.example.org", timeout=15)'
openshell --workspace default logs alpha --source sandbox --level info -n 100
```

Predicted: the call fails, and the log shows `DENIED mail.example.org`. Cited:
`docs/POC-REPORT.md`, Pass criterion 4. Nothing to put back.

### A generated file is not yours to edit

`router.json` is generated. Add a space to it, and look:

```sh
printf ' ' >> "$AMAP_OPENSHELL_HOME/router.json"
python3 amap-openshell.py router-config
python3 amap-openshell.py verify
```

Predicted: `router-config` prints the drift and exits 1, and `verify` reports
FAIL on "router.json is what this fleet renders". Put it back:

```sh
python3 amap-openshell.py router-config --apply
```

Cited: `verbs.run_router_config` and `verify.verify_router_config`.

### A stopped router is not a healthy one

Stop the router, verify at once, and verify again once three poll intervals
have passed:

```sh
docker stop amap-router-local
python3 amap-openshell.py verify
echo "exit $?"
sleep 20
python3 amap-openshell.py verify
echo "exit $?"
```

Predicted:

- **At once:** "the router container is running" fails, and `verify` exits
  1. The health checks still pass, because they read the router's
  `status.json`, and its last poll is within 3 × the announced interval
  (15 s at the default 5 s).
- **After 20 s:** "the last poll is recent relative to the interval the
  router announced" also fails, with the age against the 15 s bound.
- No check reports UNKNOWN: nothing is missing, only stale.

Cited: `docs/POC-REPORT.md`, L2, step 11, and sandy's `router_health`
discipline, reused through `router_sections`. Put it back with
`docker start amap-router-local`. Do not use step 8's `run.sh`: it starts a
new container under the same name, which the stopped one still holds. Then
`verify` exits 0 again.
