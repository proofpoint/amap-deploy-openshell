#!/usr/bin/env bash
# examples/vms/proxmox/provision-guest.sh: turn the new VM into the G3 host.
#
# Run it INSIDE the VM that create-vm.sh made, as the login user (it uses
# sudo). With --apply it goes from a fresh Ubuntu 24.04 VM to gate G3 met:
#   1. checks the kernel for what OpenShell needs;
#   2. installs Docker Engine from Docker's apt repository (OpenShell needs
#      28.0 or later), plus python3, git, jq and qemu-guest-agent;
#   3. enables linger, and makes sure the systemd user manager, which runs the
#      OpenShell gateway, has the docker group;
#   4. installs OpenShell, pinned to the version this deployment was verified
#      on (--openshell-version to change it);
#   5. writes the gateway configuration: DESIGN.md section 6, with decision
#      D6's posture (Docker driver, bind mounts on, loopback only, no OIDC);
#   6. validates the config, restarts the gateway, and registers it with the
#      CLI when the installer didn't;
#   7. checks the posture and runs the G3 probe;
#   8. with --home DIR: deploys the mounts interceptor (docs/INTERCEPTOR.md):
#      a hash-locked virtualenv, a systemd user unit ordered before the
#      gateway, the fragment merged into gateway.toml, and a gateway restart.
# --no-openshell stops after step 3, for a snapshot before OpenShell.
#
# Step 8 needs a clone of this repository and its siblings, which a fresh VM
# lacks on its first pass. So it runs only when --home is given: clone, run
# `siblings --apply`, then rerun this script from the clone with --home.
# The gateway is a systemd user unit with After=default.target and
# WantedBy=default.target (OpenShell v0.1.2:deploy/deb/openshell-gateway.service:4-23).
# It was read, not run. The interceptor's unit is ordered from our side only,
# with Before= and WantedBy=openshell-gateway.service; OpenShell's own unit is
# never edited. Under fail_closed the gateway does not start without the
# interceptor (OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:218-243).
# UNVERIFIED, nothing here has run live: that `config preflight` accepts the
# interceptors tables, and that the gateway reconnects after an interceptor
# restart.
#
# The docker group is root-equivalent on this VM. That is acceptable only
# because the VM has one operator (IMPLEMENTATION-PLAN.md, decision D6).
#
# DRY RUN BY DEFAULT. Without --apply it only runs the kernel checks and prints
# what it would do. It is safe to rerun: each step checks what is already
# there. It never overwrites a gateway.toml that differs from the one below;
# --replace-gateway-config moves the old one aside first.
#
# Requirements are from OpenShell main@acbac9c:docs/about/support-matrix.mdx,
# and the install and gateway steps from docs/about/installation.mdx,
# docs/how-it-works/gateways/configuration.mdx and authentication.mdx:52.
set -euo pipefail

PROG="$(basename "$0")"
APPLY=0 WITH_OPENSHELL=1 REPLACE_CONFIG=0 AMAP_HOME_DIR=""
OPENSHELL_WANT="0.1.2"
while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --no-openshell) WITH_OPENSHELL=0; shift ;;
        --openshell-version) OPENSHELL_WANT="${2:-}"; OPENSHELL_WANT="${OPENSHELL_WANT#v}"; shift 2 ;;
        --replace-gateway-config) REPLACE_CONFIG=1; shift ;;
        --home)
            [ $# -ge 2 ] && [ -n "$2" ] || { echo "$PROG: --home needs a value" >&2; exit 2; }
            AMAP_HOME_DIR="$2"; shift 2 ;;
        -h|--help)
            echo "usage: $PROG [--apply] [--no-openshell] [--openshell-version X.Y.Z] [--replace-gateway-config] [--home DIR]"
            exit 0 ;;
        *) echo "$PROG: unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$OPENSHELL_WANT" ] || { echo "$PROG: --openshell-version needs a value" >&2; exit 2; }

# Overridable only so the script can be tested off a real VM.
LSM_FILE="${LSM_FILE:-/sys/kernel/security/lsm}"
OS_RELEASE="${OS_RELEASE:-/etc/os-release}"
KERNEL_RELEASE="${KERNEL_RELEASE:-$(uname -r)}"
GATEWAY_TOML="${GATEWAY_TOML:-${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateway.toml}"
WAIT_SECS="${WAIT_SECS:-60}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
INTERCEPTOR_VENV="${INTERCEPTOR_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/amap-openshell/interceptor-venv}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
UNIT_NAME=amap-openshell-interceptor.service

MIN_KERNEL="6.2"
MIN_DOCKER="28.0"
GATEWAY_URL="https://127.0.0.1:17670"
GATEWAY_NAME="openshell"
PROBE_NAME="g3-probe"
INSTALLER_URL="https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh"

# The gateway configuration. Its settings must stay identical to the TOML
# block in docs/L1-RUNBOOK.md step 2; a test compares the two.
GATEWAY_CONFIG='# Written by examples/vms/proxmox/provision-guest.sh.
# DESIGN.md section 6 and decision D6: the Docker driver with host bind mounts,
# resource admission off, bind_address unset (the built-in 127.0.0.1:17670
# listener, loopback only) and no OIDC.
[openshell]
version = 2

[openshell.gateway]
compute_driver = "docker"

[openshell.drivers.docker]
allow_driver_config = true
enable_bind_mounts = true

[openshell.drivers.docker.resource_admission]
enabled = false
'

problems=0
ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*"; problems=$((problems + 1)); }
note() { echo "  note  $*"; }
die()  { echo "$PROG: $*" >&2; exit 1; }

# version_ge A B: true when dotted version A >= B (compares major.minor).
version_ge() {
    local a1 a2 b1 b2
    IFS=. read -r a1 a2 _ <<<"$1"; IFS=. read -r b1 b2 _ <<<"$2"
    a2="${a2%%[!0-9]*}"; a1="${a1%%[!0-9]*}"
    [ "${a1:-0}" -gt "$b1" ] || { [ "${a1:-0}" -eq "$b1" ] && [ "${a2:-0}" -ge "$b2" ]; }
}

run() {
    printf '+'; printf ' %q' "$@"; printf '\n'
    if [ "$APPLY" -eq 1 ]; then "$@"; fi
}

# Settings only: comments and blank lines don't count.
settings_of() { grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$@" || true; }

# gateway.toml in two parts: the base, before the first interceptor table, and
# the tail from that table on, which step 8 merges.
IC_TABLE='^[[:space:]]*\[\[openshell\.gateway\.interceptors\]\]'
base_settings_of() { sed "/$IC_TABLE/,\$d" "$1" | settings_of; }
tail_settings_of() { sed -n "/$IC_TABLE/,\$p" "$1" | settings_of; }

wait_for_gateway() {
    for _ in $(seq 1 "$WAIT_SECS"); do
        ss -ltnH 2>/dev/null | awk '{print $4}' | grep -q ':17670$' && break
        sleep 1
    done
    ss -ltnH 2>/dev/null | awk '{print $4}' | grep -q ':17670$' \
        || die "the gateway isn't listening after ${WAIT_SECS}s: journalctl --user -u openshell-gateway -n 50"
}

# ---------------------------------------------------------------- 1. checks
echo "$PROG: checks"

if [ -r "$OS_RELEASE" ]; then
    # shellcheck disable=SC1090
    . "$OS_RELEASE"
    if [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "24.04" ]; then
        ok "OS is Ubuntu 24.04"
    else
        note "OS is ${PRETTY_NAME:-unknown}; this script is written for Ubuntu 24.04"
    fi
else
    note "cannot read $OS_RELEASE"
fi

if version_ge "$KERNEL_RELEASE" "$MIN_KERNEL"; then
    ok "kernel $KERNEL_RELEASE (Landlock ABI 3 needs $MIN_KERNEL or later)"
else
    bad "kernel $KERNEL_RELEASE is older than $MIN_KERNEL: no Landlock ABI 3"
fi

if [ -r "$LSM_FILE" ]; then
    if tr ',' '\n' <"$LSM_FILE" | grep -qx landlock; then
        ok "Landlock is an active LSM ($(cat "$LSM_FILE"))"
    else
        bad "Landlock is not active (LSMs: $(cat "$LSM_FILE")); add it to the kernel's lsm= boot parameter"
    fi
else
    bad "cannot read $LSM_FILE: is securityfs mounted?"
fi

case "$(uname -m)" in
    x86_64|aarch64) ok "architecture $(uname -m)" ;;
    *) bad "architecture $(uname -m) is not one OpenShell publishes" ;;
esac

FRAGMENT="" UNIT_TEXT="" DESIRED_TAIL=""
if [ -n "$AMAP_HOME_DIR" ]; then
    # What step 8 needs, checked before anything changes.
    errf="$(mktemp)"
    trap 'rm -f "$errf"' EXIT
    if [ -f "$REPO/interceptor/server.py" ] && [ -f "$REPO/interceptor/requirements.txt" ]; then
        if FRAGMENT="$(python3 -B "$REPO/interceptor/deploy.py" fragment --home "$AMAP_HOME_DIR" 2>"$errf")"; then
            ok "home $AMAP_HOME_DIR can hold the mounts interceptor's socket"
        else
            bad "--home: $(tail -n 1 "$errf")"
        fi
        if ! UNIT_TEXT="$(python3 -B "$REPO/interceptor/deploy.py" unit --home "$AMAP_HOME_DIR" --venv "$INTERCEPTOR_VENV" 2>"$errf")"; then
            bad "the interceptor's unit: $(tail -n 1 "$errf")"
        fi
    else
        bad "--home needs this script run from a clone of this repository, after siblings --apply"
    fi
fi

if [ "$problems" -gt 0 ]; then
    echo "$PROG: $problems check(s) failed; OpenShell sandboxes would fail to launch here. Nothing installed." >&2
    exit 1
fi

[ "$APPLY" -eq 1 ] || echo "$PROG: DRY RUN: nothing is installed or changed; add --apply to do it"

# ------------------------------------------------------ 2. Docker and packages
echo "$PROG: Docker and packages"
ARCH_DEB="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
CODENAME="${VERSION_CODENAME:-noble}"
run sudo apt-get update
run sudo apt-get install -y ca-certificates curl python3 python3-venv git jq qemu-guest-agent
run sudo install -m 0755 -d /etc/apt/keyrings
run sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
run sudo chmod a+r /etc/apt/keyrings/docker.asc
REPO_LINE="deb [arch=$ARCH_DEB signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $CODENAME stable"
echo "+ echo '$REPO_LINE' | sudo tee /etc/apt/sources.list.d/docker.list"
if [ "$APPLY" -eq 1 ]; then echo "$REPO_LINE" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null; fi
run sudo apt-get update
run sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
run sudo usermod -aG docker "$(id -un)"
run sudo systemctl enable --now docker
run sudo systemctl start qemu-guest-agent
if [ "$APPLY" -eq 1 ]; then
    have="$(sudo docker version --format '{{.Server.Version}}')"
    version_ge "$have" "$MIN_DOCKER" \
        && ok "Docker Engine $have (OpenShell needs $MIN_DOCKER or later)" \
        || die "Docker Engine $have is older than $MIN_DOCKER"
fi

# -------------------------------------------- 3. linger and the user manager
echo "$PROG: the systemd user manager"
# The gateway is a systemd user service. The user manager must outlive
# logouts (linger), and it must have the docker group. A manager started
# before usermod keeps its old groups, and the gateway then fails with
# "failed to query Docker daemon version ... client error (Connect)".
run sudo loginctl enable-linger "$(id -un)"
manager_has_docker() { systemd-run --user --wait -P id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; }
if [ "$APPLY" -eq 1 ]; then
    if manager_has_docker; then
        ok "the user manager has the docker group"
    else
        note "the user manager predates the docker group; restarting it"
        run sudo systemctl restart "user@$(id -u).service"
        sleep 2
        manager_has_docker && ok "the user manager has the docker group" \
            || die "the user manager still lacks the docker group: reboot the VM (sudo reboot), then rerun $PROG --apply"
    fi
else
    echo "  (with --apply: restart user@$(id -u).service if its processes lack the docker group)"
fi

if [ "$WITH_OPENSHELL" -eq 0 ]; then
    cat <<EOF

$PROG: stopped before OpenShell (--no-openshell). To snapshot this point, on
the Proxmox host: qm snapshot <vmid> pre-openshell
Then rerun $PROG --apply to continue.
EOF
    exit 0
fi

# ------------------------------------------------------------- 4. OpenShell
echo "$PROG: OpenShell $OPENSHELL_WANT"
installed=""
if [ "$APPLY" -eq 1 ] && command -v openshell >/dev/null 2>&1; then
    installed="$(openshell --version 2>/dev/null | awk '{print $NF}')"
    installed="${installed#v}"
fi
if [ -n "$installed" ] && [ "$installed" = "$OPENSHELL_WANT" ]; then
    ok "OpenShell $installed is installed"
else
    [ -z "$installed" ] || note "OpenShell $installed is installed; installing $OPENSHELL_WANT"
    echo "+ curl -LsSf $INSTALLER_URL | OPENSHELL_VERSION=v$OPENSHELL_WANT sh"
    if [ "$APPLY" -eq 1 ]; then
        curl -LsSf "$INSTALLER_URL" | OPENSHELL_VERSION="v$OPENSHELL_WANT" sh
        installed="$(openshell --version 2>/dev/null | awk '{print $NF}')"
        [ "${installed#v}" = "$OPENSHELL_WANT" ] \
            || die "after installing, openshell reports '${installed:-nothing}', not $OPENSHELL_WANT"
        ok "OpenShell $OPENSHELL_WANT is installed"
    fi
fi

# ------------------------------------------------- 5. the gateway configuration
echo "$PROG: gateway configuration at $GATEWAY_TOML"
[ -z "$AMAP_HOME_DIR" ] || DESIRED_TAIL="$(settings_of <<<"$FRAGMENT")"
BASE_WRITTEN=0
acceptable=0
if [ -e "$GATEWAY_TOML" ] \
   && [ "$(base_settings_of "$GATEWAY_TOML")" = "$(settings_of <<<"$GATEWAY_CONFIG")" ]; then
    have_tail="$(tail_settings_of "$GATEWAY_TOML")"
    if [ -z "$AMAP_HOME_DIR" ]; then
        acceptable=1
        [ -z "$have_tail" ] || note "gateway.toml registers an interceptor; this run without --home leaves it as it is"
    elif [ -z "$have_tail" ] || [ "$have_tail" = "$DESIRED_TAIL" ]; then
        acceptable=1
    fi
fi
if [ "$acceptable" -eq 1 ]; then
    ok "gateway.toml already has exactly this deployment's settings"
elif [ -e "$GATEWAY_TOML" ] && [ "$REPLACE_CONFIG" -eq 0 ]; then
    echo "  FAIL  $GATEWAY_TOML exists with different settings. Compare it with the"
    echo "        TOML in docs/L1-RUNBOOK.md step 2, or rerun with --replace-gateway-config"
    echo "        to move it aside and write this deployment's."
    exit 1
else
    if [ -e "$GATEWAY_TOML" ]; then
        run mv "$GATEWAY_TOML" "$GATEWAY_TOML.replaced-$(date +%Y%m%d%H%M%S)"
    fi
    run mkdir -p "$(dirname "$GATEWAY_TOML")"
    echo "+ write $GATEWAY_TOML"
    if [ "$APPLY" -eq 1 ]; then printf '%s' "$GATEWAY_CONFIG" >"$GATEWAY_TOML"; fi
    BASE_WRITTEN=1
fi

# ---------------------------------------- 6. validate, restart and register
echo "$PROG: the gateway"
run openshell-gateway config preflight --path "$GATEWAY_TOML"
run systemctl --user restart openshell-gateway
if [ "$APPLY" -eq 1 ]; then
    wait_for_gateway
    # Capture first: under pipefail, `openshell status | grep` fails whenever
    # status does, even when grep matches.
    status_out="$(openshell status 2>&1)" && status_rc=0 || status_rc=$?
    if [ "$status_rc" -ne 0 ] && grep -qi "no active gateway" <<<"$status_out"; then
        run openshell gateway add "$GATEWAY_URL" --local --name "$GATEWAY_NAME"
    fi
    openshell status >/dev/null 2>&1 && ok "the CLI reaches the gateway" \
        || die "openshell status still fails: $(openshell status 2>&1 | head -3)"
else
    echo "  (with --apply: wait for the listener, and run 'openshell gateway add $GATEWAY_URL --local --name $GATEWAY_NAME' if the CLI has no active gateway)"
fi

# ------------------------------------------------- 7. the posture and the probe
echo "$PROG: decision D6's posture, and the G3 probe"
if [ "$APPLY" -eq 1 ]; then
    listeners="$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep ':17670$' || true)"
    if [ -n "$listeners" ] && ! grep -qv '^127\.0\.0\.1:17670$' <<<"$listeners"; then
        ok "the gateway listens on 127.0.0.1:17670 only"
    else
        bad "the gateway listens on: $(echo $listeners): it must be 127.0.0.1:17670 only"
    fi
    if grep -qE '^[[:space:]]*(bind_address|\[openshell\.gateway\.oidc\])' "$GATEWAY_TOML"; then
        bad "$GATEWAY_TOML sets bind_address or OIDC, which breaks the accepted posture"
    else
        ok "gateway.toml sets no bind_address and no OIDC"
    fi
    [ "$problems" -eq 0 ] || die "the gateway breaks decision D6's posture; fix it before L1"
fi
if [ -e "$GATEWAY_TOML" ] && grep -q "$IC_TABLE" "$GATEWAY_TOML"; then
    note "skipped the G3 probe: gateway.toml registers an interceptor, and the mounts interceptor refuses every create until install has written fleet.json; the probe runs on the first configuration"
else
    run openshell sandbox create --name "$PROBE_NAME" --no-keep -- true
fi

# ------------------------------------------------ 8. the mounts interceptor
echo "$PROG: the mounts interceptor (docs/INTERCEPTOR.md)"
if [ -z "$AMAP_HOME_DIR" ]; then
    note "not deployed: after siblings --apply, rerun this script from the clone with --home DIR --apply; bring-up stops until it runs"
else
    [ -d "$AMAP_HOME_DIR" ] || run mkdir -p "$AMAP_HOME_DIR"
    # Record the home, so amap-openshell.py finds it without --home or
    # $AMAP_OPENSHELL_HOME (home_record.py; the same path, under the same base).
    HOME_RECORD="${XDG_CONFIG_HOME:-$HOME/.config}/amap-openshell/home"
    run mkdir -p "$(dirname "$HOME_RECORD")"
    echo "+ record the home in $HOME_RECORD"
    if [ "$APPLY" -eq 1 ]; then printf '%s\n' "$AMAP_HOME_DIR" >"$HOME_RECORD"; fi
    if [ ! -x "$INTERCEPTOR_VENV/bin/python" ]; then
        run mkdir -p "$(dirname "$INTERCEPTOR_VENV")"
        run python3 -m venv "$INTERCEPTOR_VENV"
    fi
    # Every run, so a new lock upgrades the virtualenv.
    run "$INTERCEPTOR_VENV/bin/pip" install --require-hashes --only-binary=:all: -r "$REPO/interceptor/requirements.txt"
    unit_file="$SYSTEMD_USER_DIR/$UNIT_NAME"
    if [ -f "$unit_file" ] && [ "$(cat "$unit_file")" = "$UNIT_TEXT" ]; then
        ok "the interceptor's unit is current"
    else
        run mkdir -p "$SYSTEMD_USER_DIR"
        echo "+ write $unit_file"
        if [ "$APPLY" -eq 1 ]; then printf '%s\n' "$UNIT_TEXT" >"$unit_file"; fi
    fi
    run systemctl --user daemon-reload
    run systemctl --user enable "$UNIT_NAME"
    echo "+ systemctl --user restart $UNIT_NAME"
    if [ "$APPLY" -eq 1 ]; then
        systemctl --user restart "$UNIT_NAME" \
            || die "the interceptor did not start: journalctl --user -u $UNIT_NAME -n 50"
    fi
    if [ "$BASE_WRITTEN" -eq 1 ] || [ ! -e "$GATEWAY_TOML" ] \
       || [ -z "$(tail_settings_of "$GATEWAY_TOML")" ]; then
        echo "+ append the interceptor fragment (gateway-config --home) to $GATEWAY_TOML"
        if [ "$APPLY" -eq 1 ]; then printf '\n%s\n' "$FRAGMENT" >>"$GATEWAY_TOML"; fi
    else
        ok "gateway.toml already registers the mounts interceptor"
    fi
    run openshell-gateway config preflight --path "$GATEWAY_TOML"
    run systemctl --user restart openshell-gateway
    if [ "$APPLY" -eq 1 ]; then
        wait_for_gateway
        ok "the gateway started with the interceptor registered (fail_closed: it does not start otherwise); verify's V1-V3 confirm it"
    fi
fi

if [ "$APPLY" -eq 1 ]; then
    ver="$(openshell --version | awk '{print $NF}')"
    IC_SUMMARY="" NEXT_3=""
    if [ -n "$AMAP_HOME_DIR" ]; then
        IC_SUMMARY="  interceptor  $UNIT_NAME (before the gateway)
"
    else
        NEXT_3="  3. Clone this repository and run siblings --apply (see README.md), then
     rerun this script from the clone with --home \"\$AMAP_OPENSHELL_HOME\" --apply.
"
    fi
    cat <<EOF

G3 is met. For the L1 report:
  OpenShell  $ver
  Docker     $(sudo docker version --format '{{.Server.Version}}')
  kernel     $(uname -r)
  shared uid:gid (decision D7): $(id -u):$(id -g)
EOF
    printf '%s' "$IC_SUMMARY"
    cat <<EOF

Next:
  1. On the Proxmox host, snapshot this point (snapshot names allow letters,
     digits, - and _ only):
       qm snapshot <vmid> g3-openshell-${ver//./-}
  2. Follow docs/L1-RUNBOOK.md from step 0.
EOF
    printf '%s' "$NEXT_3"
fi
