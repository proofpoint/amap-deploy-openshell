"""examples/vms/proxmox: the G3 host as a Proxmox VM.

Both scripts are dry runs by default. These tests run them against fake `qm`,
`curl`, `sudo` and `apt-get` on PATH, and assert that a dry run only reads
Proxmox state (`qm status`) and never downloads or installs anything. They
also assert that the checks refuse what they exist to refuse. No real `qm`,
Docker or OpenShell runs.
"""

import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

import pytest

import _amap_main
import amap_openshell
import gateway
from interceptor import deploy, wire

HERE = _amap_main.REPO / "examples" / "vms" / "proxmox"
CREATE = HERE / "create-vm.sh"
GUEST = HERE / "provision-guest.sh"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExample operator@example.org\n"
IMAGE = "https://example.org/images/noble-server-cloudimg-amd64.img"


def fake(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def lab(tmp_path: Path) -> Dict[str, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    fake(bindir, "id", '[ "$1" = "-u" ] && { echo "${FAKE_UID:-0}"; exit 0; }\nexec /usr/bin/id "$@"\n')
    for name in ("qm", "curl", "sudo", "apt-get", "pvesh"):
        fake(bindir, name, f'echo "{name} $*" >> "{log}"\n'
                           f'[ "{name} $1" = "qm status" ] && exit "${{QM_STATUS_RC:-2}}"\n'
                           "exit 0\n")
    key = tmp_path / "id.pub"
    key.write_text(KEY)
    return {"bin": bindir, "log": log, "key": key, "tmp": tmp_path}


def run(lab: Dict[str, Path], argv: List[str], **env: str) -> subprocess.CompletedProcess:
    e = {"PATH": f"{lab['bin']}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(lab["tmp"])}
    e.update(env)
    return subprocess.run(["bash", *argv], capture_output=True, text=True, env=e, timeout=60)


def calls(lab: Dict[str, Path]) -> List[str]:
    return lab["log"].read_text().splitlines() if lab["log"].exists() else []


@pytest.mark.parametrize("script", [CREATE, GUEST], ids=lambda p: p.name)
def test_the_script_parses_and_is_executable(script: Path) -> None:
    assert script.stat().st_mode & stat.S_IXUSR, f"{script.name} is not executable"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_a_create_dry_run_only_reads_proxmox(lab) -> None:
    r = run(lab, [str(CREATE), "--vmid", "500", "--ssh-key", str(lab["key"]), "--image-url", IMAGE])
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout
    assert "+ qm create 500" in r.stdout
    assert "import-from=" in r.stdout and "--sshkeys" in r.stdout
    assert calls(lab) == ["qm status 500"], "a dry run ran more than the VMID check"


def test_create_refuses_a_vmid_that_exists(lab) -> None:
    r = run(lab, [str(CREATE), "--vmid", "500", "--ssh-key", str(lab["key"]), "--image-url", IMAGE],
            QM_STATUS_RC="0")
    assert r.returncode == 2
    assert "already exists" in r.stderr
    assert "qm create" not in r.stdout


def test_create_refuses_a_private_key_or_no_key(lab) -> None:
    private = lab["tmp"] / "id"
    # Shaped like a private key file, but split so that no secret scanner
    # matches it: create-vm.sh refuses a file with no public key line.
    armour = "OPENSSH " + "PRIVATE KEY-----"
    private.write_text(f"-----BEGIN {armour}\nnot-a-key\n-----END {armour}\n")
    r = run(lab, [str(CREATE), "--vmid", "500", "--ssh-key", str(private), "--image-url", IMAGE])
    assert r.returncode == 2 and "no public key" in r.stderr
    r = run(lab, [str(CREATE), "--vmid", "500", "--image-url", IMAGE])
    assert r.returncode == 2 and "--ssh-key is required" in r.stderr
    assert calls(lab) == []


def test_create_needs_a_gateway_with_a_static_address(lab) -> None:
    r = run(lab, [str(CREATE), "--vmid", "500", "--ssh-key", str(lab["key"]), "--image-url", IMAGE,
                  "--ip", "192.0.2.10/24"])
    assert r.returncode == 2 and "--gw is required" in r.stderr


def guest_env(lab, kernel: str, lsm: str) -> Dict[str, str]:
    lsm_file = lab["tmp"] / "lsm"
    lsm_file.write_text(lsm)
    os_release = lab["tmp"] / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\nPRETTY_NAME="Ubuntu 24.04"\n')
    return {"KERNEL_RELEASE": kernel, "LSM_FILE": str(lsm_file), "OS_RELEASE": str(os_release)}


def test_a_guest_dry_run_checks_and_installs_nothing(lab) -> None:
    r = run(lab, [str(GUEST)], **guest_env(lab, "6.8.0-45-generic", "lockdown,capability,landlock,yama,apparmor"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY RUN" in r.stdout
    assert "+ sudo apt-get install -y docker-ce" in r.stdout
    assert calls(lab) == [], "a dry run ran sudo, apt-get or curl"


@pytest.mark.parametrize("kernel,lsm,why", [
    ("6.1.0-26-amd64", "lockdown,capability,landlock,yama", "older than 6.2"),
    ("6.8.0-45-generic", "lockdown,capability,yama,apparmor", "Landlock is not active"),
])
def test_the_guest_refuses_a_kernel_openshell_cannot_use(lab, kernel, lsm, why) -> None:
    r = run(lab, [str(GUEST), "--apply"], **guest_env(lab, kernel, lsm))
    assert r.returncode == 1
    assert why in r.stdout
    assert "Nothing installed" in r.stderr
    assert calls(lab) == [], "a failed check still installed something"


@pytest.mark.parametrize("script", [CREATE, GUEST, HERE / "README.md"], ids=lambda p: p.name)
def test_the_example_names_no_host(script: Path) -> None:
    text = script.read_text()
    for prefix in ("/ho" + "me/", "/Us" + "ers/"):
        assert prefix not in text


def test_create_names_the_real_problem_when_qm_is_missing(lab) -> None:
    (lab["bin"] / "qm").unlink()
    r = run(lab, [str(CREATE), "--vmid", "500", "--ssh-key", str(lab["key"]), "--image-url", IMAGE])
    assert r.returncode == 2
    assert "not a Proxmox VE host" in r.stderr and "create-vm.sh:" in r.stderr


def test_create_refuses_to_run_as_non_root(lab) -> None:
    r = run(lab, [str(CREATE), "--vmid", "500", "--ssh-key", str(lab["key"]), "--image-url", IMAGE],
            FAKE_UID="1000")
    assert r.returncode == 2 and "run as root" in r.stderr
    assert "qm create" not in r.stdout


# --- provision-guest.sh --apply, end to end against fakes ---------------------

def _toml_settings(text: str) -> List[str]:
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def test_the_gateway_config_matches_the_runbook() -> None:
    script = GUEST.read_text()
    body = script.split("GATEWAY_CONFIG='", 1)[1].split("\n'\n", 1)[0]
    runbook = (_amap_main.REPO / "docs" / "L1-RUNBOOK.md").read_text()
    step2 = runbook.split("## 2. Configure the gateway", 1)[1]
    block = step2.split("```toml\n", 1)[1].split("```", 1)[0]
    assert _toml_settings(body) == _toml_settings(block)


@pytest.fixture
def vm(tmp_path: Path) -> Dict[str, Path]:
    """A fake Ubuntu VM: every program the apply path runs is a fake that logs
    its call. State files model what is installed, registered and so on."""
    bindir, state = tmp_path / "bin", tmp_path / "state"
    bindir.mkdir()
    state.mkdir()
    log = tmp_path / "calls.log"
    rec = f'echo "$(basename "$0") $*" >> "{log}"\n'
    fake(bindir, "sudo", rec +
         'case "$*" in\n'
         '  "docker version --format"*) echo 29.8.1 ;;\n'
         '  "systemctl restart user@"*) touch "$STATE/manager_ok" ;;\n'
         '  tee*) cat >/dev/null ;;\n'
         'esac\nexit 0\n')
    fake(bindir, "dpkg", "echo amd64\n")
    fake(bindir, "systemd-run", rec +
         '[ -e "$STATE/manager_ok" ] && echo "amap docker" || echo amap\n')
    fake(bindir, "curl", rec + 'echo \'echo "$OPENSHELL_VERSION" > "$STATE/installed"\'\n')
    fake(bindir, "openshell", rec +
         'case "$1" in\n'
         '  --version) [ -e "$STATE/installed" ] && echo "openshell $(sed s/^v// "$STATE/installed")"; exit 0 ;;\n'
         '  status) [ -e "$STATE/registered" ] && exit 0; echo "Error: No active gateway." >&2; exit 1 ;;\n'
         '  gateway) [ "$2" = add ] && touch "$STATE/registered"; exit 0 ;;\n'
         'esac\nexit 0\n')
    for name in ("openshell-gateway", "systemctl", "sleep"):
        fake(bindir, name, rec + "exit 0\n")
    fake(bindir, "ss", 'echo "LISTEN 0 4096 ${FAKE_LISTEN:-127.0.0.1:17670} 0.0.0.0:*"\n')
    fake(bindir, "id", '[ "$1" = "-u" ] && { echo 1000; exit 0; }\nexec /usr/bin/id "$@"\n')
    return {"bin": bindir, "state": state, "log": log, "tmp": tmp_path,
            "toml": tmp_path / "cfg" / "openshell" / "gateway.toml"}


def prepare_guest(vm) -> Dict[str, str]:
    lsm = vm["tmp"] / "lsm"
    lsm.write_text("lockdown,capability,landlock,yama,apparmor")
    osr = vm["tmp"] / "os-release"
    osr.write_text('ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n')
    return {"PATH": f"{vm['bin']}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(vm["tmp"]),
            "STATE": str(vm["state"]), "KERNEL_RELEASE": "6.8.0-142-generic",
            "LSM_FILE": str(lsm), "OS_RELEASE": str(osr),
            "GATEWAY_TOML": str(vm["toml"]), "WAIT_SECS": "2"}


def run_guest(vm, args, apply: bool, env) -> subprocess.CompletedProcess:
    e = prepare_guest(vm)
    e.update(env)
    argv = ["bash", str(GUEST), *(["--apply"] if apply else []), *args]
    return subprocess.run(argv, capture_output=True, text=True, env=e, timeout=60)


def apply_guest(vm, *args: str, **env: str) -> subprocess.CompletedProcess:
    return run_guest(vm, args, True, env)


def vm_calls(vm) -> List[str]:
    return vm["log"].read_text().splitlines() if vm["log"].exists() else []


def test_a_fresh_vm_goes_all_the_way_to_the_probe(vm) -> None:
    r = apply_guest(vm)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = vm_calls(vm)
    assert "sudo systemctl restart user@1000.service" in calls, "the stale user manager wasn't restarted"
    assert (vm["state"] / "installed").read_text().strip() == "v0.1.2", "OpenShell wasn't pinned to 0.1.2"
    assert _toml_settings(vm["toml"].read_text()) == _toml_settings(
        GUEST.read_text().split("GATEWAY_CONFIG='", 1)[1].split("\n'\n", 1)[0])
    assert "openshell gateway add https://127.0.0.1:17670 --local --name openshell" in calls
    assert "openshell sandbox create --name g3-probe --no-keep -- true" in calls
    linger = [i for i, c in enumerate(calls) if c.startswith("sudo loginctl enable-linger ")]
    assert linger and linger[0] < calls.index("openshell sandbox create --name g3-probe --no-keep -- true")
    assert "G3 is met" in r.stdout and "g3-openshell-0-1-2" in r.stdout


def test_a_configured_vm_is_left_as_it_is(vm) -> None:
    assert apply_guest(vm).returncode == 0
    vm["log"].unlink()
    before = vm["toml"].read_text()
    r = apply_guest(vm)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = vm_calls(vm)
    assert not any(c.startswith("curl") for c in calls), "OpenShell was reinstalled"
    assert not any("gateway add" in c for c in calls), "the gateway was registered twice"
    assert not any("restart user@" in c for c in calls)
    assert vm["toml"].read_text() == before
    assert "already has exactly this deployment's settings" in r.stdout


def test_a_different_gateway_config_is_never_overwritten(vm) -> None:
    vm["toml"].parent.mkdir(parents=True)
    vm["toml"].write_text('[openshell]\nversion = 2\n[openshell.gateway]\nbind_address = "0.0.0.0:17670"\n')
    before = vm["toml"].read_text()
    r = apply_guest(vm)
    assert r.returncode == 1
    assert "exists with different settings" in r.stdout
    assert vm["toml"].read_text() == before
    assert not any("sandbox create" in c for c in vm_calls(vm))
    r = apply_guest(vm, "--replace-gateway-config")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any("bind_address" in ln for ln in _toml_settings(vm["toml"].read_text()))
    assert [p.name for p in vm["toml"].parent.iterdir() if ".replaced-" in p.name], "the old file wasn't kept"


def test_a_gateway_beyond_loopback_fails_the_posture_check(vm) -> None:
    r = apply_guest(vm, FAKE_LISTEN="0.0.0.0:17670")
    assert r.returncode == 1
    assert "must be 127.0.0.1:17670 only" in r.stdout
    assert not any("sandbox create" in c for c in vm_calls(vm)), "the probe ran despite the posture"


def test_no_openshell_stops_before_openshell(vm) -> None:
    r = apply_guest(vm, "--no-openshell")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pre-openshell" in r.stdout
    assert not (vm["state"] / "installed").exists()
    assert not vm["toml"].exists()


def test_the_vm_gets_its_siblings_from_the_siblings_command() -> None:
    md = (HERE / "README.md").read_text(encoding="utf-8")
    # The README's fences sit inside list items, so they are indented.
    lines, fenced = [], False
    for raw in md.splitlines():
        if raw.strip().startswith("```"):
            fenced = not fenced
        elif fenced and raw.strip():
            lines.append(raw.strip())
    parsed = []
    for line in lines:
        tokens = shlex.split(line)
        for i, t in enumerate(tokens):
            if os.path.basename(t) == "amap-openshell.py":
                parsed.append(
                    amap_openshell.build_parser().parse_args(tokens[i + 1:]))
    assert any(a.command == "siblings" and a.apply is True for a in parsed)
    for line in lines:
        assert not re.search(r"git clone https://github\.com/proofpoint/"
                             r"amap-(router-local|connector-claude|"
                             r"deploy-sandy|spec)", line), line
        assert not re.search(r"git -C amap-\S+ checkout", line), line
    # The public clone is the default; a push to a bare repository on the VM
    # is the alternative for a VM kept off GitHub.
    assert any("git clone https://github.com/proofpoint/amap-deploy-openshell"
               in ln for ln in lines)
    assert any("git clone -b main ~/amap-deploy-openshell.git" in ln
               for ln in lines)


# --- provision-guest.sh --home: the mounts interceptor (step 8) ---------------

UNIT_NAME = "amap-openshell-interceptor.service"
RESTART_GATEWAY = "systemctl --user restart openshell-gateway"
RESTART_INTERCEPTOR = f"systemctl --user restart {UNIT_NAME}"
PIP_LINE = ("pip install --require-hashes --only-binary=:all: -r "
            f"{_amap_main.REPO}/interceptor/requirements.txt")


def gateway_config_body() -> str:
    return GUEST.read_text().split("GATEWAY_CONFIG='", 1)[1].split("\n'\n", 1)[0] + "\n"


@pytest.fixture
def ivm(vm):
    """`vm`, with a systemctl that snapshots gateway.toml when the gateway is
    restarted, and a python3 that fakes `-m venv` and runs everything else."""
    home = Path(tempfile.mkdtemp(prefix="pg-", dir="/tmp")).resolve() / "home"
    log = vm["log"]
    fake(vm["bin"], "systemctl",
         f'echo "systemctl $*" >> "{log}"\n'
         '[ "$*" = "--user restart openshell-gateway" ] && cp "$GATEWAY_TOML" "$STATE/toml-at-gateway-restart"\n'
         'exit 0\n')
    fake(vm["bin"], "python3",
         'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
         f'  echo "python3 -m venv $3" >> "{log}"\n'
         '  mkdir -p "$3/bin"\n'
         f'  printf \'#!/bin/sh\\necho "pip $*" >> "{log}"\\n\' > "$3/bin/pip"\n'
         "  printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/python\"\n"
         '  chmod +x "$3/bin/pip" "$3/bin/python"\n'
         "  exit 0\n"
         "fi\n"
         f'exec {shlex.quote(sys.executable)} "$@"\n')
    env = {"PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("AMAP_ROUTER_REPO", "AMAP_CONNECTOR_REPO", "AMAP_SANDY_REPO", "AMAP_SPEC_DIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    out = dict(vm)
    out.update({"home": home, "env": env,
                "venv": vm["tmp"] / ".local" / "share" / "amap-openshell" / "interceptor-venv",
                "unit": vm["tmp"] / ".config" / "systemd" / "user" / UNIT_NAME})
    try:
        yield out
    finally:
        shutil.rmtree(home.parent, ignore_errors=True)


def _snapshot(*roots: Path):
    return {str(p): (p.is_dir(), None if p.is_dir() else p.read_bytes())
            for root in roots for p in [root, *sorted(root.rglob("*"))] if p.exists()}


def test_apply_orders_the_interceptor_unit_before_the_gateway_and_writes_the_fragment(ivm) -> None:
    home, venv, toml = str(ivm["home"]), str(ivm["venv"]), ivm["toml"]
    r = apply_guest(ivm, "--home", home, **ivm["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    unit = ivm["unit"].read_text()
    assert unit.rstrip() == deploy.unit_for(home, venv).rstrip()
    assert "Before=openshell-gateway.service" in unit
    assert "WantedBy=default.target openshell-gateway.service" in unit
    calls = vm_calls(ivm)

    def at(call):
        return calls.index(call)

    gateway_restarts = [i for i, c in enumerate(calls) if c == RESTART_GATEWAY]
    order = [at(f"python3 -m venv {venv}"), at(PIP_LINE),
             at("systemctl --user daemon-reload"), at(f"systemctl --user enable {UNIT_NAME}"),
             at(RESTART_INTERCEPTOR), gateway_restarts[-1]]
    assert order == sorted(order) and len(set(order)) == len(order), calls
    assert at("openshell sandbox create --name g3-probe --no-keep -- true") < at(RESTART_INTERCEPTOR)
    fragment = wire.gateway_fragment(home)
    text = toml.read_text()
    assert fragment in text
    assert _toml_settings(text) == _toml_settings(gateway_config_body()) + _toml_settings(fragment)
    assert fragment in (ivm["state"] / "toml-at-gateway-restart").read_text()
    rows = gateway.interceptor_report(gateway.read_gateway_toml(str(toml)), home)
    assert rows and all(v == gateway.PRESENT for _, v, _ in rows), rows
    assert Path(home).is_dir()


def test_an_interceptor_dry_run_changes_nothing(ivm) -> None:
    home = ivm["home"]
    prepare_guest(ivm)
    before = (_snapshot(ivm["tmp"]), _snapshot(home.parent))
    r = run_guest(ivm, ["--home", str(home)], False, ivm["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY RUN" in r.stdout
    for needle in ("+ python3 -m venv", "pip install --require-hashes --only-binary=:all: -r",
                   f"+ systemctl --user enable {UNIT_NAME}",
                   f"+ systemctl --user restart {UNIT_NAME}",
                   "+ append the interceptor fragment"):
        assert needle in r.stdout, needle
    assert vm_calls(ivm) == []
    assert (_snapshot(ivm["tmp"]), _snapshot(home.parent)) == before


def test_a_deployed_interceptor_is_left_as_it_is(ivm) -> None:
    home = str(ivm["home"])
    assert apply_guest(ivm, "--home", home, **ivm["env"]).returncode == 0
    toml_1, unit_1 = ivm["toml"].read_bytes(), ivm["unit"].read_bytes()
    ivm["log"].unlink()
    r = apply_guest(ivm, "--home", home, **ivm["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert ivm["toml"].read_bytes() == toml_1 and ivm["unit"].read_bytes() == unit_1
    for needle in ("already registers the mounts interceptor", "unit is current", "skipped the G3 probe"):
        assert needle in r.stdout, needle
    calls = vm_calls(ivm)
    assert not any(c.startswith("python3 -m venv") for c in calls)
    assert not any("sandbox create" in c for c in calls)


def test_a_base_only_gateway_toml_gets_the_fragment_merged(ivm) -> None:
    assert apply_guest(ivm).returncode == 0
    saved = ivm["toml"].read_text()
    r = apply_guest(ivm, "--home", str(ivm["home"]), **ivm["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    text = ivm["toml"].read_text()
    assert text.startswith(saved) and text.endswith(wire.gateway_fragment(str(ivm["home"])))
    assert not [p for p in ivm["toml"].parent.iterdir() if ".replaced-" in p.name]


def test_a_different_interceptor_block_is_never_overwritten(ivm) -> None:
    home = str(ivm["home"])
    fragment = wire.gateway_fragment(home)
    assert "fail_closed" in fragment
    ivm["toml"].parent.mkdir(parents=True)
    ivm["toml"].write_text(gateway_config_body() + "\n" + fragment.replace("fail_closed", "fail_open"))
    before = ivm["toml"].read_bytes()
    r = apply_guest(ivm, "--home", home, **ivm["env"])
    assert r.returncode == 1
    assert "exists with different settings" in r.stdout
    assert ivm["toml"].read_bytes() == before
    assert not any(c.startswith("systemctl") for c in vm_calls(ivm))
    r = apply_guest(ivm, "--home", home, "--replace-gateway-config", **ivm["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert ivm["toml"].read_text().endswith(fragment)
    assert "fail_open" not in ivm["toml"].read_text()
    assert [p for p in ivm["toml"].parent.iterdir() if ".replaced-" in p.name]


def test_home_is_checked_before_anything_is_installed(ivm) -> None:
    real = ivm["home"].parent / "real"
    real.mkdir()
    link = ivm["home"].parent / "link"
    link.symlink_to(real)
    for bad in ("relative/x", str(link)):
        r = apply_guest(ivm, "--home", bad, **ivm["env"])
        assert r.returncode == 1, (bad, r.stdout + r.stderr)
        assert "Nothing installed" in r.stderr
        assert vm_calls(ivm) == []


def test_without_home_the_interceptor_is_not_deployed(vm) -> None:
    r = apply_guest(vm)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (vm["tmp"] / ".config" / "systemd" / "user" / UNIT_NAME).exists()
    assert "rerun this script from the clone with --home" in r.stdout
    assert not any(c.startswith("systemctl --user enable") for c in vm_calls(vm))


def test_step_8_records_the_home(ivm) -> None:
    """The home record (home_record.py), so amap-openshell.py finds the home
    without --home or $AMAP_OPENSHELL_HOME."""
    import home_record
    home = str(ivm["home"])
    r = apply_guest(ivm, "--home", home, **ivm["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    # The script runs with HOME set to the fake VM's directory and no
    # XDG_CONFIG_HOME, so the record is under that HOME's .config.
    assert home_record.read({"HOME": str(ivm["tmp"])}) == home
