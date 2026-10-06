"""The operator verbs: `install`, `provision`, `deprovision`, `list`,
`router-config`, `gateway-config` and `teardown`.

Host side only. Nothing here writes inside a sandbox, and nothing here sends or
receives a message. Every writing verb is a dry run without `--apply`. A verb
builds a `l1_kit.Plan` and `l1_kit.carry_out` does the writing, so a refusal
writes nothing. The verbs add no rendering, no lane names and no layout of their
own: they reuse `render`, `router_config`, `membership`, `policy` and `l1_kit`.

The only program run is `openshell`, and only through `openshell_cli`, which
reads a sandbox's identity, lists, creates and deletes. `provision` writes the
lanes, the policy and the create command before it runs `sandbox create`,
because a bind mount whose source does not exist is refused (OpenShell
main@acbac9c:crates/openshell-driver-docker/src/lib.rs:3753-3761).

Host-wide options come before the verb, as amap-deploy-sandy's README has them:
`--home`, `--fleet`, `--image`, `--run-as`, `--restart-policy`, `--openshell`.

What `deprovision` removes, and why (amap-router-local):

- `router-state/<name>/first-seen.json` is the first-sight marker. Without
  deleting it a recreated name adopts the old marker, so it would not be a new
  instance (`router/firstsight.py`, module docstring and line 215;
  `router/reset.py:18-37`).
- The contents of each lane leaf go, and the directories stay, because the lane
  roots are live bind-mount sources. `audit/` is preserved
  (`router/reset.py:96-112`).

`router.reset` is an internal module and running it would run the router, so the
removal is done here with plain file operations that follow those two rules.

A new `fleet.json` is the `--fleet` template with `fleet_domain` set to
`fleet_policy.derived_fleet_domain` for `openshell`, <this host's name> and the
base (D21).
An existing `fleet.json` is never rewritten.

Exit codes: 0 ok; 1 a refusal, drift or any UNKNOWN; 2 usage or a missing
sibling. Standard library only, and Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import os
import json
import posixpath
import socket
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import gateway
import home_record
import l1_kit
import membership
import openshell_cli
import policy
import provider_profile
import render
import router_config
import router_link
from interceptor import wire

PROG = l1_kit.PROG
EXIT_REFUSED = 1      # a refusal, drift, or any UNKNOWN
EXIT_USAGE = 2
FLEET_DOMAIN_RUNTIME = "openshell"   # D21: openshell.<host>.<base>

HOME_VARIABLE = "AMAP_OPENSHELL_HOME"
DEFAULT_FLEET = str(l1_kit.REPO / "examples" / "fleet.json")
DEFAULT_IMAGE = "amap-openshell-agent"
AUDIT_DIRNAME = "audit"          # the router keeps its audit log here
FIRST_SEEN_NAME = "first-seen.json"
HELD_DIRNAME = "held"
DRY_RUN_LINE = "dry run: nothing was written; add --apply to write"


class VerbError(ValueError):
    """A refusal the operator can fix."""


class HostArgs(NamedTuple):
    home: str
    image: str
    run_as: str
    restart_policy: bool
    openshell: str
    fleet: str
    env: Mapping[str, str]


def host_args(args: argparse.Namespace,
              env: Optional[Mapping[str, str]] = None) -> HostArgs:
    env = os.environ if env is None else env
    home = getattr(args, "home", None) or env.get(HOME_VARIABLE)
    if not home:
        raise VerbError(f"--home is required (or set ${HOME_VARIABLE})")
    run_as = getattr(args, "run_as", None) or f"{os.getuid()}:{os.getgid()}"
    return HostArgs(
        home=home,
        image=getattr(args, "image", None) or DEFAULT_IMAGE,
        run_as=run_as,
        restart_policy=bool(getattr(args, "restart_policy", False)),
        openshell=getattr(args, "openshell", None)
        or openshell_cli.DEFAULT_BINARY,
        fleet=getattr(args, "fleet", None) or DEFAULT_FLEET,
        env=dict(env))


def client_for(h: HostArgs, fleet: Dict[str, Any]) -> openshell_cli.Client:
    return openshell_cli.Client(h.openshell, policy.workspace_of(fleet), h.env)


def _host(h: HostArgs) -> render.Host:
    return render.Host(h.home, h.image, render.parse_run_as(h.run_as),
                       h.restart_policy)


def _lstat(path: str) -> Optional[os.stat_result]:
    return l1_kit._lstat(path)


def loaded_fleet(h: HostArgs) -> Dict[str, Any]:
    """`$HOME/fleet.json`, loaded; or a refusal that names `install --apply`."""
    l1_kit.home_checks(h.home)
    path = l1_kit.fleet_json(h.home)
    st = _lstat(path)
    if st is None:
        raise VerbError(f"{path} is absent: run `install --apply` first")
    l1_kit._require_regular_file(path, st)
    return policy.load_fleet(path)


def recorded(h: HostArgs) -> Optional[List[membership.Member]]:
    """The recorded members; None means `membership.json` is absent."""
    return membership.load(l1_kit.membership_json(h.home))


def _emit(lines: List[str], apply: bool) -> None:
    for line in lines:
        print(line)
    if not apply:
        print(DRY_RUN_LINE)


def _require_real_dirs(home: str, *paths: str) -> None:
    for path in paths:
        st = _lstat(path)
        if st is None or not stat.S_ISDIR(st.st_mode):
            raise VerbError(f"{os.path.relpath(path, home)}/ is absent: run "
                            f"`install --apply` first")


# --- planning ----------------------------------------------------------------

def new_fleet_domain(base: Optional[str] = None,
                     hostname: Optional[str] = None) -> str:
    """A new fleet's domain, openshell.<host>.<base> (D21). `base` defaults
    to `internal` and `hostname` to this host's. Raises policy.PolicyError
    for a base the domain rule refuses."""
    return policy.domain_for(
        FLEET_DOMAIN_RUNTIME,
        hostname if hostname is not None else socket.gethostname(),
        base or policy.DEFAULT_DOMAIN_BASE)


def new_fleet_bytes(template: str, domain: str) -> bytes:
    """The template's JSON with fleet_domain set to `domain`; every other
    key, and the key order, as the template has them."""
    doc = json.loads(Path(template).read_text(encoding="utf-8"))
    doc[policy.FLEET_DOMAIN_KEY] = domain
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def plan_install(h: HostArgs, hostname: Optional[str] = None,
                 base: Optional[str] = None) -> l1_kit.Plan:
    home = h.home
    l1_kit.home_checks(home)
    real = os.path.realpath(home)
    if real != home:
        raise VerbError(
            f"home {home!r} is not its own real path: it resolves to "
            f"{real!r}. Use {real!r} as --home (or ${HOME_VARIABLE}): the "
            f"interceptor refuses a bind source with a symlink in its path "
            f"(D19)")
    l1_kit.require_home_directory(home, must_exist=False)
    mine = l1_kit.fleet_json(home)
    st = _lstat(mine)
    if st is None:
        fleet = policy.load_fleet(h.fleet)
        domain = new_fleet_domain(base, hostname)
        replaced = fleet[policy.FLEET_DOMAIN_KEY]
        fleet[policy.FLEET_DOMAIN_KEY] = domain
        fleet_bytes: Optional[bytes] = new_fleet_bytes(h.fleet, domain)
        fleet_note: Optional[str] = (
            f"new {l1_kit.FLEET_NAME}: fleet_domain {domain!r} "
            f"(openshell.<host>.<base>, derived once; every address is "
            f"<name>@{domain})"
            + (f"; --fleet's {replaced!r} is not used"
               if replaced != domain else ""))
    else:
        l1_kit._require_regular_file(mine, st)
        fleet = policy.load_fleet(mine)
        fleet_bytes = None  # the operator's: validated, never rewritten
        fleet_note = None
        if base:
            fleet_note = (
                f"note: --fleet-domain-base applies only to a new "
                f"{l1_kit.FLEET_NAME}; yours exists, so its fleet_domain stays "
                f"{fleet[policy.FLEET_DOMAIN_KEY]!r}. To change it, edit "
                f"{l1_kit.FLEET_NAME}: every address changes")
    l1_kit.fleet_members(fleet)

    dirs: List[Tuple[str, int]] = [
        (home, l1_kit.DIR_MODE),
        (render.payload_dir(home), l1_kit.DIR_MODE),
        (posixpath.join(render.payload_dir(home), l1_kit.PAYLOAD_BIN_DIRNAME),
         l1_kit.DIR_MODE),
        (render.roster_dir(home), l1_kit.DIR_MODE),
        (render.instances_dir(home), l1_kit.DIR_MODE),
        (posixpath.join(home, l1_kit.POLICIES_DIRNAME), l1_kit.DIR_MODE),
        (posixpath.join(home, l1_kit.COMMANDS_DIRNAME), l1_kit.DIR_MODE)]
    files: List[Tuple[str, bytes, int]] = [
        (posixpath.join(render.payload_dir(home), rel), data, mode)
        for rel, data, mode in l1_kit.read_payload(l1_kit.find_connector())]
    notes: List[str] = []
    if fleet_bytes is not None:
        files.append((mine, fleet_bytes, l1_kit.FILE_MODE))
    else:
        notes.append(f"present {l1_kit.FLEET_NAME} (yours: validated, never "
                     f"rewritten)")
    if fleet_note:
        notes.append(fleet_note)

    have = membership.load(l1_kit.membership_json(home))
    if have is None:
        notes.append("membership.json is absent: nothing has been recorded")
    elif not have:
        notes.append("membership.json lists no member")
    left = l1_kit.missing_members(fleet, have or [])
    if have is not None and have and not left:
        more_dirs, more_files, warnings = l1_kit.router_files(home, fleet, have)
        dirs.extend(more_dirs)
        files.extend(more_files)
        notes.extend(f"warning: {w}" for w in warnings)
    else:
        if left:
            notes.append(
                f"still to provision: {', '.join(left)}; router.json, "
                f"selected.json and router-state/ are written once every "
                f"member is recorded")
    l1_kit.check_types(dirs, files)
    return l1_kit.Plan(home, tuple(dirs), (), tuple(files), tuple(notes))


def plan_provision_files(h: HostArgs, fleet: Dict[str, Any], name: str
                         ) -> Tuple[l1_kit.Plan, render.Rendering]:
    home = h.home
    members = l1_kit.fleet_members(fleet)
    if name not in members:
        raise VerbError(f"{name!r} is not a member of the fleet "
                        f"({', '.join(members)})")
    l1_kit.home_checks(home)
    l1_kit.require_home_directory(home, must_exist=True)
    _require_real_dirs(home, render.payload_dir(home), render.roster_dir(home))
    rendered = render.render_member(fleet, name, _host(h),
                                    l1_kit.policy_file(home, name))
    dirs = ((posixpath.join(home, l1_kit.POLICIES_DIRNAME), l1_kit.DIR_MODE),
            (posixpath.join(home, l1_kit.COMMANDS_DIRNAME), l1_kit.DIR_MODE))
    files = ((l1_kit.policy_file(home, name),
              rendered.policy_yaml.encode("utf-8"), l1_kit.FILE_MODE),
             (l1_kit.command_file(home, name),
              l1_kit.command_text(rendered.argv).encode("utf-8"),
              l1_kit.FILE_MODE))
    router_config.create_lanes(home, name, dry_run=True)  # refuses a bad lane
    l1_kit.check_types(dirs, files)
    return l1_kit.Plan(home, dirs, (name,), files, ()), rendered


def plan_record(h: HostArgs, member: membership.Member) -> l1_kit.Plan:
    """`l1-kit record`'s plan: `membership.json`, and the router's files when
    this completes the record."""
    return l1_kit.plan_record(h.home, member.name, member.id)


def _state_entries(home: str, name: str) -> List[l1_kit.Removal]:
    """What of `router-state/<name>/` goes: the first-sight marker first, then
    everything but `audit/`."""
    root = posixpath.join(router_config.default_state_dir(home), name)
    st = _lstat(root)
    if st is None:
        return []
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise VerbError(f"{root} is not a real directory: the router scans "
                        f"with follow_symlinks=False")
    out = [l1_kit.Removal(posixpath.join(root, FIRST_SEEN_NAME), "file")]
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if entry.name in (FIRST_SEEN_NAME, AUDIT_DIRNAME):
            continue
        is_dir = entry.is_dir(follow_symlinks=False)
        out.append(l1_kit.Removal(entry.path, "tree" if is_dir else "file"))
    return out


def plan_deprovision(h: HostArgs, fleet: Dict[str, Any], name: str,
                     member: membership.Member) -> l1_kit.Plan:
    home = h.home
    l1_kit.home_checks(home)
    l1_kit.require_home_directory(home, must_exist=True)
    removals = _state_entries(home, name)
    roots = {render.lane_dir(home, name, lane) for lane in router_config.LANES}
    for path in router_config.lane_paths(home, name):
        if path not in roots:
            removals.append(l1_kit.Removal(path, "contents"))

    remaining = membership.remove(recorded(h) or [], name)
    workspace = policy.workspace_of(fleet)
    files: List[Tuple[str, bytes, int]] = [
        (l1_kit.membership_json(home),
         membership.dump(remaining).encode("utf-8"), l1_kit.FILE_MODE)]
    notes: List[str] = []
    if _lstat(render.selected_json(home)) is not None:
        verdict = router_config.json_text(
            router_config.render_verdict(remaining, workspace))
        files.append((render.selected_json(home), verdict.encode("utf-8"),
                      l1_kit.FILE_MODE))
    if _lstat(router_config.router_json_path(home)) is not None:
        notes.append(
            f"note: {router_config.ROUTER_JSON_NAME} still names {name}. "
            f"Edit {l1_kit.FLEET_NAME} and run `router-config --apply`, or "
            f"provision {name} again")
    l1_kit.check_types((), files)
    return l1_kit.Plan(home, (), (), tuple(files), tuple(notes),
                       tuple(removals))


def _tree_files(path: str) -> int:
    n = 0
    for _, _, names in os.walk(path):
        n += len(names)
    return n


def plan_teardown(h: HostArgs) -> l1_kit.Plan:
    home = h.home
    l1_kit.home_checks(home)
    l1_kit.require_home_directory(home, must_exist=True)
    removals = (
        l1_kit.Removal(render.payload_dir(home), "tree"),
        l1_kit.Removal(render.roster_dir(home), "tree"),
        l1_kit.Removal(router_config.router_json_path(home), "file"),
        l1_kit.Removal(render.selected_json(home), "file"),
        l1_kit.Removal(router_config.default_state_dir(home), "tree"))
    for rm in removals:
        l1_kit._removal_state(rm)  # refuses a symlink before anything goes
    return l1_kit.Plan(home, (), (), (), (), removals)


# --- the verbs ---------------------------------------------------------------

def run_install(args: argparse.Namespace) -> int:
    h = host_args(args)
    plan = plan_install(h, base=getattr(args, "fleet_domain_base", None))
    lines = l1_kit.carry_out(plan, args.apply)
    where = home_record.record_path(h.env)
    if where is not None:
        if args.apply:
            home_record.write(h.home, h.env)
            lines.append(f"recorded home {h.home} in {where}")
        else:
            lines.append(f"would record home {h.home} in {where}")
    _emit(lines, args.apply)
    return 0


def _unknown(why: str) -> int:
    print(f"UNKNOWN: {why}")
    return EXIT_REFUSED


def run_provision(args: argparse.Namespace,
                  env: Optional[Mapping[str, str]] = None) -> int:
    """`env` replaces `os.environ` for this call; `bring-up` passes the
    provider key this way (D10)."""
    h = host_args(args, env)
    name = args.name
    fleet = loaded_fleet(h)
    files_plan, rendered = plan_provision_files(h, fleet, name)
    client = client_for(h, fleet)
    workspace = rendered.workspace
    known = recorded(h) or []
    on_record = membership.find(known, name)

    state, doc, why = openshell_cli.find_sandbox(client, name)
    if state == "unknown":
        return _unknown(why)
    observed: Optional[membership.Member] = None
    lines: List[str] = []
    if state == "present":
        assert doc is not None
        problem = openshell_cli.identity_problem(doc, name, workspace)
        if problem:
            raise VerbError(problem)
        observed = membership.Member(workspace, name, str(doc["id"]))
        if on_record is not None:
            problem = membership.check(known, observed)
            if problem:
                raise VerbError(f"{problem} Run `deprovision {name} --apply` "
                                f"first.")
        lines.append(f"present sandbox {name} ({observed.id})")
    elif on_record is not None:
        raise VerbError(
            f"membership.json records {name!r} as {on_record.id!r}, but "
            f"OpenShell has no sandbox {name!r}. That ID cannot come back: run "
            f"`deprovision {name} --apply`, then provision again")

    creating = state == "absent"
    if creating and args.apply and not h.env.get(openshell_cli.KEY_VARIABLE):
        raise VerbError(f"${openshell_cli.KEY_VARIABLE} is not set: "
                        f"`sandbox create` with --auto-providers needs it in "
                        f"the environment (D10). Nothing was written")

    lines = l1_kit.carry_out(files_plan, args.apply) + lines
    if (observed is not None and not args.apply
            and router_config.create_lanes(h.home, name, dry_run=True)):
        lines.append(f"would record {name} in membership.json with ID "
                     f"{observed.id}")
        _emit(lines, False)
        return 0
    if creating:
        command = l1_kit.command_text(rendered.argv).strip()
        if not args.apply:
            lines.append(f"would create sandbox {name}: {command}")
            lines.append(f"would record {name} in membership.json with the ID "
                         f"`sandbox get` reports")
            if not h.env.get(openshell_cli.KEY_VARIABLE):
                lines.append(f"note: ${openshell_cli.KEY_VARIABLE} is not set; "
                             f"--apply will refuse to create")
            _emit(lines, False)
            return 0
        for line in lines:
            print(line)
        lines = []
        made = client.create(rendered.argv)
        if made.code != 0:
            raise VerbError(
                f"`sandbox create {name}` failed "
                f"({openshell_cli.first_line(made.stderr)}). The usual cause "
                f"is a provider profile that is not imported: run `l1-kit "
                f"profile` and `openshell provider profile import`")
        print(f"created sandbox {name}")
        state, doc, why = openshell_cli.find_sandbox(client, name)
        if state != "present":
            raise VerbError(f"created {name}, but its ID cannot be read back "
                            f"({why or 'the sandbox is absent'}); record it "
                            f"with `l1-kit record`")
        assert doc is not None
        problem = openshell_cli.identity_problem(doc, name, workspace)
        if problem:
            raise VerbError(problem)
        observed = membership.Member(workspace, name, str(doc["id"]))
    assert observed is not None
    lines += l1_kit.carry_out(plan_record(h, observed), args.apply)
    _emit(lines, args.apply)
    return 0


def run_deprovision(args: argparse.Namespace) -> int:
    h = host_args(args)
    name = args.name
    fleet = loaded_fleet(h)
    known = recorded(h)
    member = membership.find(known or [], name)
    if member is None:
        raise VerbError(f"{name!r} is not recorded in membership.json: there "
                        f"is nothing to deprovision")
    client = client_for(h, fleet)
    state, doc, why = openshell_cli.find_sandbox(client, name)
    if state == "unknown":
        return _unknown(why)
    lines: List[str] = []
    delete = False
    if state == "absent":
        lines.append(f"absent sandbox {name}")
    else:
        assert doc is not None
        problem = openshell_cli.identity_problem(doc, name, member.workspace)
        if problem is None and doc.get("id") == member.id:
            delete = True
            lines.append(f"{'deleting' if args.apply else 'would delete'} "
                         f"sandbox {name} ({member.id})")
        else:
            lines.append(
                f"left sandbox {name} alone: it is not the recorded member "
                f"({problem or 'it has ID ' + repr(doc.get('id'))}; "
                f"membership.json records {member.id!r}). Only the record and "
                f"the lanes are removed")
    plan = plan_deprovision(h, fleet, name, member)  # refuses before deleting
    lines += held_warnings(h.home, [rm.path for rm in plan.removals
                                    if rm.kind == "tree"])
    if delete and args.apply:
        gone = client.delete(name)
        if gone.code != 0:
            raise VerbError(f"`sandbox delete {name}` failed "
                            f"({openshell_cli.first_line(gone.stderr)}); "
                            f"nothing else was changed")
    _emit(lines + l1_kit.carry_out(plan, args.apply), args.apply)
    return 0


def run_list(args: argparse.Namespace) -> int:
    h = host_args(args)
    fleet = loaded_fleet(h)
    known = recorded(h)
    client = client_for(h, fleet)
    names, why = client.list_names()
    if names is None:
        return _unknown(why)
    prefix = client.workspace + "/"
    listed = {n[len(prefix):] for n in names if n.startswith(prefix)}
    by_name = {m.name: m for m in known or []}
    clean = known is not None
    if known is None:
        print("membership.json is absent: nothing has been recorded")
    for name in sorted(listed | set(by_name)):
        rec = by_name.get(name)
        if rec is None:
            print(f"{name}: not a member (no record in membership.json)")
            clean = False
        elif name not in listed:
            print(f"{name}: recorded but absent (recorded ID {rec.id})")
            clean = False
        else:
            state, doc, gone = openshell_cli.find_sandbox(client, name)
            if state != "present":
                print(f"{name}: UNKNOWN ({gone or 'absent'})")
                clean = False
            elif doc is not None and doc.get("id") != rec.id:
                print(f"{name}: ID MISMATCH (recorded {rec.id}, sandbox has "
                      f"{doc.get('id')})")
                clean = False
            else:
                print(f"{name}: member ({rec.id})")
    return 0 if clean else EXIT_REFUSED


def run_router_config(args: argparse.Namespace) -> int:
    h = host_args(args)
    fleet = loaded_fleet(h)
    known = recorded(h)
    if known is None:
        raise VerbError("membership.json is absent: nothing has been "
                        "recorded, so there is no router configuration to "
                        "render")
    left = l1_kit.missing_members(fleet, known)
    if left:
        raise VerbError(f"every member must be recorded first; still to "
                        f"provision: {', '.join(left)}")
    rendered = router_config.render_router_json(fleet, known, h.home)
    dirs, files, warnings = l1_kit.router_files(h.home, fleet, known)
    plan = l1_kit.Plan(h.home, tuple(dirs), (), tuple(files),
                       tuple(f"warning: {w}" for w in warnings))
    l1_kit.check_types(dirs, files)
    if args.apply:
        _emit(l1_kit.carry_out(plan, True), True)
        return 0
    drift = router_config.drift_on_disk(
        router_config.router_json_path(h.home), rendered)
    pending = [ln for ln in l1_kit.carry_out(plan, False)
               if ln.startswith("would ")]
    if not drift and not pending:
        print("in sync")
        return 0
    for line in drift:
        print(line)
    for line in pending:
        print(line)
    print("drift: run `router-config --apply` to render it")
    return EXIT_REFUSED


def run_gateway_config(args: argparse.Namespace) -> int:
    path = getattr(args, "gateway_toml", None) or gateway.gateway_toml_path(
        os.environ)
    reading = gateway.read_gateway_toml(path)
    print(gateway.FRAGMENT, end="")
    home = getattr(args, "home", None) or os.environ.get(HOME_VARIABLE)
    if home and render.home_problem(home) is None:
        print("\n" + wire.gateway_fragment(home), end="")
    else:
        print("\n# The interceptor fragment needs --home (or "
              f"${HOME_VARIABLE}): its endpoint is a socket under it.")
    print(f"\ngateway.toml: {reading.path or 'not located'}")
    report = gateway.settings_report(reading)
    for setting, verdict, why in report:
        print(f"{verdict:<8} {setting} ({why})")
    return 0 if all(v == gateway.PRESENT for _, v, _ in report) else \
        EXIT_REFUSED


def held_requests(home: str, roots: Sequence[str]) -> List[str]:
    """Every file under `roots` in a `held` directory, sorted: the router's
    held requests (amap-router-local router/outbound.py:196-197). A root that
    is not a real directory holds none."""
    out: List[str] = []
    for root in roots:
        if os.path.isdir(root) and not os.path.islink(root):
            # Only the part below the home counts: a `held` directory above
            # it, in the operator's own path, says nothing about the files.
            out += [os.path.join(d, n) for d, _, names in os.walk(root)
                    if HELD_DIRNAME in os.path.relpath(d, home).split(os.sep)
                    for n in names]
    return sorted(out)


def held_warnings(home: str, roots: Sequence[str]) -> List[str]:
    return [f"held request {os.path.relpath(p, home)} will be lost"
            for p in held_requests(home, roots)]


def run_teardown(args: argparse.Namespace) -> int:
    h = host_args(args)
    plan = plan_teardown(h)
    home = h.home
    state = router_config.default_state_dir(home)
    if os.path.isdir(state) and not os.path.islink(state):
        for line in held_warnings(home, [state]):
            print(line)
        audit = 0
        for entry in os.scandir(state):
            kept = os.path.join(entry.path, AUDIT_DIRNAME)
            if entry.is_dir(follow_symlinks=False) and os.path.isdir(kept):
                audit += _tree_files(kept)
        if audit:
            print(f"router-state holds audit files ({audit} in all); they "
                  f"will be lost with it")
    for rm in plan.removals:
        if os.path.isdir(rm.path) and not os.path.islink(rm.path):
            print(f"{os.path.relpath(rm.path, home)}/ holds "
                  f"{_tree_files(rm.path)} file(s)")
    known = membership.load(l1_kit.membership_json(home))
    if known:
        print(f"membership.json still records "
              f"{', '.join(m.name for m in known)}: kept, and their "
              f"sandboxes are not touched (use `deprovision`)")
    lines = l1_kit.carry_out(plan, args.apply)
    try:
        recorded = home_record.read(h.env)
    except home_record.RecordError:
        recorded = None
    if recorded == home:
        where = home_record.record_path(h.env)
        if args.apply:
            home_record.forget(home, h.env)
            lines.append(f"removed the home record {where}")
        else:
            lines.append(f"would remove the home record {where}")
    _emit(lines, args.apply)
    return 0


# --- command line ------------------------------------------------------------

RUNNERS = {"install": run_install, "provision": run_provision,
           "deprovision": run_deprovision, "list": run_list,
           "router-config": run_router_config,
           "gateway-config": run_gateway_config, "teardown": run_teardown}


def main(args: argparse.Namespace) -> int:
    command = args.command
    try:
        return RUNNERS[command](args)
    except (VerbError, l1_kit.KitError, render.RenderError,
            router_config.RouterConfigError, policy.PolicyError,
            membership.MembershipError, provider_profile.ProfileError,
            openshell_cli.OpenShellError, router_link.RouterNotFound,
            policy.SandyNotFound, OSError) as e:
        print(f"{PROG} {command}: {e}", file=sys.stderr)
        return EXIT_REFUSED
