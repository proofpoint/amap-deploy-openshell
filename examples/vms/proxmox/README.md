# The G3 host as a Proxmox VM

Two scripts that turn a Proxmox VE host into gate G3 of
[IMPLEMENTATION-PLAN.md](../../../IMPLEMENTATION-PLAN.md): a Linux VM that can
run OpenShell's Docker driver. Both are **dry runs by default**. Run each once
without `--apply` to read the plan, then again with it.

| Script | Where it runs | What it does |
|---|---|---|
| [`create-vm.sh`](create-vm.sh) | the Proxmox host, as root | creates an Ubuntu 24.04 VM from the cloud image, with your SSH key and a non-root user, and starts it |
| [`provision-guest.sh`](provision-guest.sh) | inside the new VM, as that user | checks the kernel for Landlock, installs Docker Engine and the tools, installs OpenShell (pinned to 0.1.2), writes the gateway config for decision D6, registers the gateway with the CLI, checks the posture, and runs the G3 probe. With `--home`, it also deploys the mounts interceptor |

`provision-guest.sh --apply` takes a fresh VM to gate G3 met. It is safe to
rerun: each step checks what is already there. It never overwrites a
`gateway.toml` with different settings unless you pass
`--replace-gateway-config`, which keeps the old file beside it.

## Why a VM, and why this one

- **A full VM, not an LXC container.** OpenShell's sandboxes need Landlock
  ABI 3, seccomp user-notification with `ADDFD`, and same-uid task-memory
  access. OpenShell probes all three at launch and fails closed without them
  (OpenShell `docs/about/support-matrix.mdx`, "Kernel Requirements"). A VM has
  its own kernel. Docker nested in an LXC container may have those operations
  filtered.
- **Ubuntu 24.04** (kernel 6.8). Landlock ABI 3 needs Linux 6.2 or later, and
  Debian and Ubuntu are OpenShell's supported Linux hosts. Debian 12's 6.1
  kernel is too old.
- **Docker Engine 28.0 or later**, OpenShell's stated minimum, from Docker's
  own repository.
- **No nested virtualization.** This deployment uses the Docker driver, not
  MicroVM (decision D3).
- **4 vCPU, 8 GiB RAM, 64 GiB disk** by default. That is an estimate for two
  Claude Code agents, the router and the images, not a measured requirement.

## Steps

```sh
# 1. On the Proxmox host (7.2 or later), as root:
./create-vm.sh --vmid <vmid> --ssh-key ~/.ssh/id_ed25519.pub            # read the plan
./create-vm.sh --vmid <vmid> --ssh-key ~/.ssh/id_ed25519.pub --apply

# 2. In the VM (ssh amap@<address>):
./provision-guest.sh              # checks, and the plan
./provision-guest.sh --apply      # ends with "G3 is met" and the versions

# 3. On the Proxmox host: snapshot the working G3 host.
qm snapshot <vmid> g3-openshell-0-1-2
```

For a snapshot before OpenShell as well, run `provision-guest.sh --apply
--no-openshell` first, take `qm snapshot <vmid> pre-openshell`, then run
`--apply` again. Proxmox snapshot names allow letters, digits, `-` and `_`
only. Then follow `docs/L1-RUNBOOK.md`.

`create-vm.sh --help` lists every option: storage, bridge, a static address,
sizes, CPU type, and the image URL for a non-amd64 host.

## The accepted posture (decision D6)

L1 and L2 run on a gateway with resource admission off, so any caller allowed
to create sandboxes could bind-mount any host path. The operator accepted this
for this VM on these conditions:

- the OpenShell gateway listens on the VM's **loopback only**, and its port is
  never exposed or forwarded to the LAN;
- the VM has one operator, with no OIDC and no other gateway credentials;
- no other sandboxes run on that gateway.

The mounts interceptor (D17) refuses wrong mounts at creation, so a
caller that may create sandboxes should no longer be able to bind-mount any
host path. It ran live on 2026-10-05 (docs/POC-REPORT.md, "L2 rerun with the
mounts interceptor on").

Run everything, including the `openshell` CLI, this repo's tooling and the
router, inside the VM over SSH. In DESIGN.md's terms, the VM is the host. The
`docker` group that `provision-guest.sh` grants is root-equivalent, which is
acceptable only under the same one-operator condition.

## Things L1 needs from this VM

- **Passwordless sudo for the account that runs `provision-guest.sh`.** It
  must run *as* the account that will own OpenShell, because it installs
  OpenShell, the gateway's user service and linger for whoever runs it. And
  it uses `sudo` throughout, for apt, Docker, `usermod`, `loginctl`,
  `systemctl` and OpenShell's own package installer. Without sudo it stops
  at the first `apt-get`. Run by a different account, it sets OpenShell up
  for the wrong uid and breaks D7. Withholding sudo protects nothing here:
  that account is in the `docker` group, which is root-equivalent, and
  OpenShell's Docker driver needs it. `create-vm.sh`'s cloud-init user gets
  passwordless sudo already; another provisioning tool must grant it.
- **The shared uid (decision D7):** the login user's `id -u` and `id -g`.
  They are the image's `SANDBOX_UID` and `SANDBOX_GID`, and the router runs as
  that user too. `provision-guest.sh` prints them at the end.
- **This repo and its siblings.** All are public, so the VM needs **no GitHub
  credential** (D11): nothing on it to revoke, and nothing to re-register when
  the VM is rebuilt. In the VM, clone this repository, and let `siblings` set
  up the four siblings beside it at the pins in `siblings.json`:

  ```sh
  mkdir -p ~/dev && cd ~/dev
  git clone https://github.com/proofpoint/amap-deploy-openshell
  cd amap-deploy-openshell
  python3 amap-openshell.py siblings
  python3 amap-openshell.py siblings --apply
  ```

  To keep the VM off GitHub, or to run your own unpublished changes, push your
  checkout to a bare repository on the VM and clone that instead:

  ```sh
  ssh amap@<vm> 'git init -q --bare ~/amap-deploy-openshell.git'
  git push amap@<vm>:amap-deploy-openshell.git main
  ```

  Then, in the VM, clone that in place of the clone above:

  ```sh
  mkdir -p ~/dev && cd ~/dev
  git clone -b main ~/amap-deploy-openshell.git amap-deploy-openshell
  ```

  Then, from the clone, deploy the mounts interceptor: a hash-locked
  virtualenv, a systemd user unit ordered before the gateway, the fragment
  merged into `gateway.toml`, and a gateway restart. It is a dry run first.

  ```sh
  examples/vms/proxmox/provision-guest.sh --home "$AMAP_OPENSHELL_HOME"
  examples/vms/proxmox/provision-guest.sh --home "$AMAP_OPENSHELL_HOME" --apply
  ```

  `siblings` refuses a checkout with local changes. amap-spec isn't pinned:
  `siblings` fast-forwards it and prints its commit. The suite finds each
  checkout beside the others; `$AMAP_SPEC_DIR` and the other `AMAP_*`
  variables override that. Each later update is a push from your machine and a
  `git pull` in the VM, then `siblings --apply`.
- **An Anthropic Console API key** for the `claude-code` provider. A
  subscription token is not accepted.
- **A clean start.** The operator chose to rebuild rather than roll back
  (2026-10-01): destroying the VM and running these two scripts again is
  what proves the G3 automation, and it leaves no trace of an earlier run.
  Run the deployment's `deprovision` and `teardown` verbs first if the
  outgoing VM holds a fleet, so those verbs get exercised on state that is
  about to go. A snapshot is still worth taking once a rebuilt VM reaches
  G3, as a fast way back to a known-good host.
