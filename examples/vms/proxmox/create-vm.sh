#!/usr/bin/env bash
# examples/vms/proxmox/create-vm.sh: create the G3 host as a Proxmox VM.
#
# Run it as root on a Proxmox VE host (7.2 or later, for `import-from`). It
# creates one VM from Ubuntu 24.04's cloud image. Cloud-init sets up the login
# user, the SSH key and the network. Nothing else is installed here: Docker
# and the package checks come afterwards, from provision-guest.sh inside the
# VM.
#
# DRY RUN BY DEFAULT. Without --apply it downloads nothing and changes
# nothing. It prints every command it would run, and only reads Proxmox state:
# `qm status`, to see whether the VMID is taken.
#
# Why these choices:
# - A full VM, not an LXC container. OpenShell's sandboxes need Landlock
#   ABI 3, seccomp user-notification with ADDFD, and same-uid task-memory
#   access. OpenShell probes all three at launch and fails closed without them
#   (OpenShell main@acbac9c:docs/about/support-matrix.mdx, "Kernel
#   Requirements"). A VM has its own kernel. Docker nested in an LXC container
#   may have those operations filtered.
# - Ubuntu 24.04 (kernel 6.8). Landlock ABI 3 needs Linux 6.2 or later, and
#   Debian and Ubuntu are OpenShell's supported Linux hosts (same page).
# - No nested virtualization. This deployment uses OpenShell's Docker driver,
#   not MicroVM (IMPLEMENTATION-PLAN.md, decision D3).
set -euo pipefail

usage() {
    cat <<'EOF'
usage: create-vm.sh --vmid N --ssh-key FILE [options] [--apply]

required:
  --vmid N            the new VM's id; must not exist yet
  --ssh-key FILE      public key(s) allowed to log in (authorized_keys format)

options (defaults in brackets):
  --name NAME         VM name [openshell-g3]
  --user NAME         login user created by cloud-init [amap]
  --storage ID        storage for the disk and the cloud-init drive [local-lvm]
  --bridge NAME       network bridge [vmbr0]
  --ip CIDR|dhcp      address for net0 [dhcp]
  --gw ADDR           gateway, required with a static --ip
  --cores N           vCPUs [4]
  --cpu TYPE          CPU type [host]
  --memory MIB        RAM in MiB [8192]
  --disk-gb N         disk size in GiB [64]
  --cache-dir DIR     where the cloud image is kept [/var/lib/vz/template/cache]
  --image-url URL     cloud image [Ubuntu 24.04 amd64, current]
  --apply             actually do it; without this, print the plan only
EOF
}

PROG="$(basename "$0")"
die() { echo "$PROG: $*" >&2; exit 2; }

VMID="" SSH_KEY="" APPLY=0
NAME="openshell-g3" CI_USER="amap" STORAGE="local-lvm" BRIDGE="vmbr0"
IP="dhcp" GW="" CORES=4 CPU="host" MEMORY=8192 DISK_GB=64
CACHE_DIR="/var/lib/vz/template/cache"
IMAGE_BASE="https://cloud-images.ubuntu.com/noble/current"
IMAGE_FILE="noble-server-cloudimg-amd64.img"
IMAGE_URL=""

while [ $# -gt 0 ]; do
    case "$1" in
        --vmid) VMID="${2:-}"; shift 2 ;;
        --ssh-key) SSH_KEY="${2:-}"; shift 2 ;;
        --name) NAME="${2:-}"; shift 2 ;;
        --user) CI_USER="${2:-}"; shift 2 ;;
        --storage) STORAGE="${2:-}"; shift 2 ;;
        --bridge) BRIDGE="${2:-}"; shift 2 ;;
        --ip) IP="${2:-}"; shift 2 ;;
        --gw) GW="${2:-}"; shift 2 ;;
        --cores) CORES="${2:-}"; shift 2 ;;
        --cpu) CPU="${2:-}"; shift 2 ;;
        --memory) MEMORY="${2:-}"; shift 2 ;;
        --disk-gb) DISK_GB="${2:-}"; shift 2 ;;
        --cache-dir) CACHE_DIR="${2:-}"; shift 2 ;;
        --image-url) IMAGE_URL="${2:-}"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
done

# --- checks that need no Proxmox ---------------------------------------------
[ -n "$VMID" ] || { usage >&2; die "--vmid is required"; }
case "$VMID" in *[!0-9]*) die "--vmid must be a number: $VMID" ;; esac
[ -n "$SSH_KEY" ] || { usage >&2; die "--ssh-key is required (the VM allows key login only)"; }
[ -f "$SSH_KEY" ] || die "--ssh-key: no such file: $SSH_KEY"
grep -qE '^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-)' "$SSH_KEY" \
    || die "--ssh-key: $SSH_KEY holds no public key (was it the private key?)"
for n in "$CORES" "$MEMORY" "$DISK_GB"; do
    case "$n" in ''|*[!0-9]*) die "--cores, --memory and --disk-gb take numbers" ;; esac
done
case "$CI_USER" in root|'') die "--user must name a non-root user" ;; esac
if [ "$IP" = "dhcp" ]; then
    IPCONFIG="ip=dhcp"
else
    case "$IP" in */*) ;; *) die "--ip must be dhcp or an address in CIDR form" ;; esac
    [ -n "$GW" ] || die "--gw is required with a static --ip"
    IPCONFIG="ip=$IP,gw=$GW"
fi
# --- checks that read Proxmox -------------------------------------------------
# qm lives in /usr/sbin, which a non-root PATH often lacks.
case ":$PATH:" in *:/usr/sbin:*) ;; *) PATH="$PATH:/usr/sbin" ;; esac
if ! command -v qm >/dev/null 2>&1; then
    if command -v pveversion >/dev/null 2>&1 || [ -d /etc/pve ]; then
        die "qm not found although this looks like a Proxmox host; run as root (sudo -i)"
    fi
    die "qm not found: this is not a Proxmox VE host. Copy this script and your .pub key to the Proxmox host and run it there, as root"
fi
[ "$(id -u)" -eq 0 ] || die "run as root on the Proxmox host (sudo -i, or sudo $0 ...)"
if qm status "$VMID" >/dev/null 2>&1; then
    die "VMID $VMID already exists; choose another (pvesh get /cluster/nextid suggests one)"
fi

if [ -z "$IMAGE_URL" ]; then
    # The default image is amd64. On another architecture, pass --image-url.
    [ "$(uname -m)" = "x86_64" ] || die "this host is $(uname -m); pass --image-url for a matching cloud image"
    IMAGE_URL="$IMAGE_BASE/$IMAGE_FILE"
    SUMS_URL="$IMAGE_BASE/SHA256SUMS"
else
    IMAGE_FILE="$(basename "$IMAGE_URL")"
    SUMS_URL="$(dirname "$IMAGE_URL")/SHA256SUMS"
fi
IMAGE="$CACHE_DIR/$IMAGE_FILE"

# --- the plan -----------------------------------------------------------------
run() {
    printf '+'; printf ' %q' "$@"; printf '\n'
    if [ "$APPLY" -eq 1 ]; then "$@"; fi
}

fetch_image() {
    # Always re-verify the image against Ubuntu's published checksums, so a
    # cached file that went stale or corrupt is replaced rather than used.
    run mkdir -p "$CACHE_DIR"
    run curl -fsSL -o "$CACHE_DIR/SHA256SUMS.$IMAGE_FILE" "$SUMS_URL"
    if [ "$APPLY" -eq 1 ]; then
        local want
        want="$(awk -v f="$IMAGE_FILE" '$2 == f || $2 == "*" f { print $1 }' \
                "$CACHE_DIR/SHA256SUMS.$IMAGE_FILE")"
        [ -n "$want" ] || die "no checksum for $IMAGE_FILE in $SUMS_URL"
        if [ -f "$IMAGE" ] && echo "$want  $IMAGE" | sha256sum -c --status; then
            echo "  cached image matches its published checksum"
        else
            run curl -fSL -o "$IMAGE.part" "$IMAGE_URL"
            echo "$want  $IMAGE.part" | sha256sum -c --status \
                || { rm -f "$IMAGE.part"; die "downloaded image fails its checksum"; }
            run mv "$IMAGE.part" "$IMAGE"
        fi
    else
        echo "  (with --apply: verify $IMAGE_FILE against SHA256SUMS; download only if missing or stale)"
    fi
}

echo "$PROG: VM $VMID ($NAME) on storage $STORAGE, bridge $BRIDGE, $CORES vCPU, ${MEMORY} MiB, ${DISK_GB} GiB"
[ "$APPLY" -eq 1 ] || echo "$PROG: DRY RUN: nothing is downloaded or changed; add --apply to do it"

fetch_image
run qm create "$VMID" \
    --name "$NAME" \
    --description "AMAP on OpenShell: G3 host (see amap-deploy-openshell examples/vms/proxmox)" \
    --ostype l26 --machine q35 --bios seabios \
    --cores "$CORES" --memory "$MEMORY" --balloon 0 \
    --cpu "$CPU" \
    --scsihw virtio-scsi-single \
    --net0 "virtio,bridge=$BRIDGE" \
    --serial0 socket --vga serial0 \
    --agent enabled=1
run qm set "$VMID" --scsi0 "$STORAGE:0,import-from=$IMAGE,discard=on,iothread=1,ssd=1"
run qm resize "$VMID" scsi0 "${DISK_GB}G"
run qm set "$VMID" --ide2 "$STORAGE:cloudinit" --boot order=scsi0
run qm set "$VMID" --ciuser "$CI_USER" --sshkeys "$SSH_KEY" --ipconfig0 "$IPCONFIG"
run qm start "$VMID"

cat <<EOF

Next:
  1. Find the VM's address (with DHCP: your DHCP server's leases, or the serial
     console: qm terminal $VMID), then: ssh $CI_USER@<address>
  2. Copy examples/vms/proxmox/provision-guest.sh into the VM and run it there
     (dry run first, then --apply). It installs Docker Engine and checks the
     kernel for Landlock.
  3. Back on this host, snapshot the clean VM before installing OpenShell:
       qm snapshot $VMID pre-openshell
     Rolling back to it gives L1 a fresh start, including the router's first
     sight.
EOF
