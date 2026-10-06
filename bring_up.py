"""The `bring-up` verb: README "Bring-up" lines 1-8 as one command (D15).

Host side only. Without `--apply` it reads the host, prints each stage's plan
and what it would skip, and changes nothing. With `--apply` it runs seven
stages in order, stops at the first that fails, and skips what is already done,
so it can be rerun. Each stage reuses the code that already does the work:

1. image: `l1_run.step_1` builds the image with the Claude Code version and the
   run-as uid:gid as build arguments and as labels, and skips an image whose
   labels already match (`l1_run.image_is_current`);
2. gateway-config: `verbs.run_gateway_config` compares `gateway.toml` with the
   fragment, and bring-up goes no further when a setting is missing;
3. install: `verbs.run_install`;
4. profile: `l1_run.import_profile` reads the node and claude paths from inside
   the built image, renders the provider profile, imports it, or updates the
   gateway's copy when it differs;
5. provision: `verbs.run_provision` for each member that is not both present
   and recorded; a member that is both is left alone;
6. router: `verbs.run_router_config`, then `l1_run.step_6` builds and starts the
   router unless a container of that name is running, and refuses a container
   that exists but is not running. `l1_run.step_6` also stops at once, with the
   container's state, exit code and last log line, if the container is
   restarting, exited or dead;
7. verify: `verify.main`. Its exit code is bring-up's.

The last line is `AMAP is up` if and only if verify exited 0. Every other end
names the stage that stopped it (`preflight` is the checks made before any
change), and the exit code is not 0.

The key (D10). Only `sandbox create --auto-providers` reads the provider key
(see `openshell_cli` and `provider_profile` for the citations). It comes from
`--api-key-stdin` (one line) or from `$ANTHROPIC_API_KEY`, and it is needed only
when a member must be created. For the whole run it is removed from this
process's environment, so no child inherits it. It is handed to
`verbs.run_provision` as an explicit environment for a member that must be
created, and `openshell_cli.Client` passes it to that `create` call and to no
other. A missing key with a member to create is refused before anything
changes. Everything printed goes through the redactor of `l1_run`, which knows
the key, and no command takes it as an argument. The key is written nowhere.

With `--apply` the commands bring-up runs itself (stages 1, 4 and 6) are
recorded, redacted, under the home's evidence directory, as `l1-run` records
them. A refusal in the preflight writes nothing.

Preflight also refuses when the mounts interceptor is not accepting
connections on its socket (docs/INTERCEPTOR.md): under fail_closed the gateway
does not create sandboxes without it. The message names
`examples/vms/proxmox/provision-guest.sh --home`, which starts it. Bring-up
still never edits `gateway.toml` or restarts the gateway.

It never runs `l1-run`, TUTORIAL step 10 or an experiment. It never removes a
sandbox, a container, an image or a profile, never edits `gateway.toml` and
never restarts the gateway.

Exit codes: 0 only when verify exited 0; 1 a refusal or a stopped stage, and a
dry run that foresees one; verify's own code when verify fails; 2 usage.
Standard library only, and Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import time
from typing import (Any, Callable, Iterator, List, Mapping, NamedTuple,
                    Optional, Sequence, Tuple)

import l1_kit
import l1_run
import membership
import openshell_cli
import policy
import provider_profile
import render
import router_config
import router_link
import verbs
import verify
from interceptor import deploy

PROG = l1_kit.PROG
EXIT_OK, EXIT_STOPPED, EXIT_USAGE = 0, 1, 2
VERSION_VARIABLE = "CLAUDE_CODE_VERSION"
KEY_VARIABLE = openshell_cli.KEY_VARIABLE
UP_LINE = "AMAP is up"
DRY_LINE = "dry run: nothing was changed; add --apply to bring AMAP up"
PREFLIGHT = "preflight"
STAGES = (("image", "build the sandbox image"),
          ("gateway-config", "check the gateway's configuration"),
          ("install", "install the payload and the layout"),
          ("profile", "render and import the provider profile"),
          ("provision", "provision every member"),
          ("router", "render the router's config and start the router"),
          ("verify", "verify"))
DONE, SKIPPED = "done", "skipped"
WOULD_RUN, WOULD_SKIP, WOULD_STOP = "would run", "would skip", "would stop"
GATEWAY_STOP = ("gateway.toml does not have every setting of the fragment "
                "above: merge it and restart the gateway yourself; bring-up "
                "never edits gateway.toml or restarts the gateway")
SETTINGS = l1_run.Settings()      # tests replace this
REFUSALS = (verbs.VerbError, l1_kit.KitError, render.RenderError,
            router_config.RouterConfigError, policy.PolicyError,
            membership.MembershipError, provider_profile.ProfileError,
            openshell_cli.OpenShellError, router_link.RouterNotFound,
            policy.SandyNotFound, l1_run.RunnerError, OSError)


class Stop(Exception):
    """A stage cannot go on. `code` is bring-up's exit code."""

    def __init__(self, reason: str, code: int = EXIT_STOPPED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class Outcome(NamedTuple):
    outcome: str                 # DONE or SKIPPED, or one of WOULD_*
    notes: Tuple[str, ...] = ()
    reason: str = ""             # WOULD_STOP, and the parenthesised SKIPPED


class MemberState(NamedTuple):
    name: str
    state: str                   # present | absent | unknown
    sandbox_id: Optional[str]
    recorded_id: Optional[str]
    why: str

    @property
    def settled(self) -> bool:
        return (self.state == "present" and self.recorded_id is not None
                and self.recorded_id == self.sandbox_id)

    @property
    def needs_create(self) -> bool:
        return self.state == "absent"


def one_line(text: Any) -> str:
    return " ".join(str(text).split())


def version_from(args: argparse.Namespace,
                 env: Mapping[str, str]) -> Optional[str]:
    """The flag, else `$CLAUDE_CODE_VERSION`. Empty counts as missing."""
    return getattr(args, "claude_code_version", None) \
        or env.get(VERSION_VARIABLE) or None


def read_key(args: argparse.Namespace, env_key: Optional[str],
             stdin: Any) -> Tuple[Optional[str], str]:
    """`(key, source)`; `(None, "")` when there is none."""
    if getattr(args, "api_key_stdin", False):
        key = stdin.readline().strip()
        if not key:
            raise Stop("--api-key-stdin was given but stdin held no key")
        return key, "stdin"
    if env_key:
        return env_key, f"${KEY_VARIABLE}"
    return None, ""


@contextlib.contextmanager
def key_out_of_environment() -> Iterator[Optional[str]]:
    """Remove the provider key from `os.environ` for the block, yield it, and
    put it back after."""
    key = os.environ.pop(KEY_VARIABLE, None)
    try:
        yield key
    finally:
        if key is not None:
            os.environ[KEY_VARIABLE] = key


def fleet_for(h: verbs.HostArgs) -> dict:
    """The home's `fleet.json` once installed, else the template."""
    if os.path.lexists(l1_kit.fleet_json(h.home)):
        return verbs.loaded_fleet(h)
    return policy.load_fleet(h.fleet)


def member_states(h: verbs.HostArgs, fleet: dict) -> List[MemberState]:
    client = verbs.client_for(h, fleet)
    known = verbs.recorded(h) or []
    workspace = policy.workspace_of(fleet)
    out: List[MemberState] = []
    for name in l1_kit.fleet_members(fleet):
        state, doc, why = openshell_cli.find_sandbox(client, name)
        on_record = membership.find(known, name)
        sandbox_id: Optional[str] = None
        if state == "present" and doc is not None \
                and openshell_cli.identity_problem(doc, name, workspace) is None:
            sandbox_id = str(doc["id"])
        out.append(MemberState(name, state, sandbox_id,
                               on_record.id if on_record else None, why))
    return out


def _openshell_argv(argv: Sequence[str], binary: str) -> List[str]:
    """`argv` with `binary` in place of a first word `openshell`."""
    argv = list(argv)
    if argv and argv[0] == "openshell":
        argv[0] = binary
    return argv


def with_openshell(go: l1_run.Go, binary: str) -> l1_run.Go:
    def swapped(cmd: l1_run.Cmd, timeout: Optional[float] = None,
                **kw: Any) -> Any:
        return go((cmd[0], _openshell_argv(cmd[1], binary)), timeout=timeout,
                  **kw)
    return swapped


def reader(binary: str, env: Mapping[str, str]) -> l1_run.Go:
    """The dry run's runner: it runs a command and records nothing."""
    client = openshell_cli.Client(binary, "", env)

    def read(cmd: l1_run.Cmd, timeout: Optional[float] = None,
             **kw: Any) -> Any:
        return client.run(cmd[1])
    return with_openshell(read, binary)


def captured(fn: Callable[..., int], *a: Any, **kw: Any
             ) -> Tuple[int, List[str]]:
    """Run an in-process verb and return `(exit code, its lines)`."""
    out, err = io.StringIO(), io.StringIO()
    code = EXIT_STOPPED
    problem: Optional[str] = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = fn(*a, **kw)
        except REFUSALS as e:
            problem = str(e)
    lines = [ln.rstrip() for ln in out.getvalue().splitlines()
             + err.getvalue().splitlines()]
    lines = [ln for ln in lines if ln.strip() and ln != verbs.DRY_RUN_LINE]
    if problem is not None:
        return EXIT_STOPPED, lines + [problem]
    return code, lines


def interceptor_problem(home: str) -> str:
    """'' when the mounts interceptor accepts on its socket; otherwise why,
    naming provision-guest.sh (deploy.listener_problem)."""
    problem = deploy.listener_problem(home)
    if not problem:
        return ""
    return (f"the mounts interceptor is not running ({problem}). Start it "
            f"before the gateway: examples/vms/proxmox/provision-guest.sh "
            f"--home {home} --apply builds its virtualenv, installs its "
            f"systemd unit, merges its fragment into gateway.toml and "
            f"restarts the gateway. bring-up never edits gateway.toml or "
            f"restarts the gateway")


def verb_args(args: argparse.Namespace, **over: Any) -> argparse.Namespace:
    return argparse.Namespace(**{**vars(args), **over})


class BringUp:
    def __init__(self, args: argparse.Namespace, stdin: Any = None,
                 out: Any = None, settings: Optional[l1_run.Settings] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Optional[Callable[[], Any]] = None) -> None:
        self.args = args
        self.apply = bool(getattr(args, "apply", False))
        self.stdin = sys.stdin if stdin is None else stdin
        self.out = sys.stdout if out is None else out
        self.settings = SETTINGS if settings is None else settings
        self.clock, self.sleep = clock, sleep
        self.now = l1_run._utc_now if now is None else now
        self.key: Optional[str] = None
        self.key_source = ""
        self.states: List[MemberState] = []
        self.interceptor_down = ""

    # --- the run ---------------------------------------------------------

    def run(self) -> int:
        with key_out_of_environment() as env_key:
            self.ctx = l1_run.Ctx(
                dict(os.environ), self.apply, self.settings, self.out,
                self.clock, self.sleep, self.now,
                lambda: (os.getuid(), os.getgid()))
            version = version_from(self.args, os.environ)
            if version is None:
                return self._stopped(
                    PREFLIGHT, "no Claude Code version: pass "
                    "--claude-code-version or set $CLAUDE_CODE_VERSION; there "
                    "is no default", EXIT_USAGE)
            if version.split() != [version]:
                return self._stopped(
                    PREFLIGHT, "the Claude Code version must not hold "
                    "whitespace", EXIT_USAGE)
            try:
                self.key, self.key_source = read_key(self.args, env_key,
                                                     self.stdin)
            except Stop as e:
                return self._stopped(PREFLIGHT, e.reason, e.code)
            secrets = dict(self.ctx.env)
            if self.key:
                secrets[KEY_VARIABLE] = self.key
            self.ctx.redactor = l1_run.Redactor(l1_run.secret_values(secrets))
            try:
                self.preflight(version)
            except Stop as e:
                return self._stopped(PREFLIGHT, e.reason, e.code)
            except REFUSALS as e:
                return self._stopped(PREFLIGHT, str(e))
            missing = self.missing_key()
            if self.apply:
                if self.interceptor_down:
                    return self._stopped(PREFLIGHT, self.interceptor_down)
                if missing:
                    return self._stopped(PREFLIGHT, missing)
                self.ctx.attach_evidence()
                return self._apply()
            return self._dry(missing)

    def preflight(self, version: str) -> None:
        h = verbs.host_args(self.args)
        run_as = render.parse_run_as(h.run_as)
        l1_kit.home_checks(h.home)
        l1_kit.require_home_directory(h.home, must_exist=False)
        fleet = fleet_for(h)
        members = l1_kit.fleet_members(fleet)
        router_repo = router_link.find_router(h.env)
        states = member_states(h, fleet)
        for s in states:
            if s.state == "unknown":
                raise Stop(f"UNKNOWN: {s.why}")
        self.ctx.adopt({
            "home": h.home, "image": h.image, "claude_code_version": version,
            "run_as": run_as, "run_as_text": f"{run_as.uid}:{run_as.gid}",
            "fleet": fleet, "workspace": policy.workspace_of(fleet),
            "members": tuple(members), "router_repo": router_repo,
            "gateway_toml": getattr(self.args, "gateway_toml", None) or ""})
        self.h, self.states = h, states
        self.read = reader(h.openshell, self.ctx.env)
        self.interceptor_down = interceptor_problem(h.home)

    def missing_key(self) -> str:
        """Why `--apply` must refuse for want of a key, or empty."""
        need = [s.name for s in self.states if s.needs_create]
        if need and not self.key:
            return (f"{', '.join(need)} must be created and there is no key: "
                    f"pass --api-key-stdin or set ${KEY_VARIABLE}. Nothing "
                    f"was changed")
        return ""

    def say(self, line: str = "") -> None:
        self.ctx.say(line)

    def _stopped(self, stage: str, reason: str, code: int = EXIT_STOPPED) -> int:
        self.say(f"bring-up stopped at {stage}: {one_line(reason)}")
        return code

    def _show(self, lines: Sequence[str]) -> None:
        for line in lines:
            self.say("  " + line)

    # --- apply -----------------------------------------------------------

    def _apply(self) -> int:
        total = len(STAGES)
        for n, (name, title) in enumerate(STAGES, 1):
            self.say(f"stage {n}/{total} {name}: {title}")
            code = EXIT_STOPPED
            try:
                result = getattr(self, "apply_" + name.replace("-", "_"))()
            except Stop as e:
                reason, code = e.reason, e.code
            except REFUSALS as e:
                reason = str(e)
            except Exception as e:  # reported, as l1_run.execute does
                reason = f"unexpected {type(e).__name__}: {e}"
            else:
                self._show(result.notes)
                self.say(f"stage {n}/{total} {name}: {result.outcome}"
                         + (f" ({result.reason})" if result.reason else ""))
                continue
            self.say(f"stage {n}/{total} {name}: stopped")
            return self._stopped(name, reason, code)
        self.say(UP_LINE)
        return EXIT_OK

    def _last(self, lines: Sequence[str], fallback: str) -> str:
        return lines[-1] if lines else fallback

    def apply_image(self) -> Outcome:
        r = l1_run.step_1(self.ctx)
        if r.outcome == l1_run.FAIL:
            raise Stop(r.notes[0] if r.notes else "the image build failed")
        if r.outcome == l1_run.SKIPPED:
            return Outcome(SKIPPED, (), r.notes[0] if r.notes else "")
        return Outcome(DONE, r.notes)

    def apply_gateway_config(self) -> Outcome:
        code, lines = captured(verbs.run_gateway_config,
                               verb_args(self.args))
        self._show(lines)
        if code != 0:
            raise Stop(GATEWAY_STOP)
        return Outcome(DONE)

    def apply_install(self) -> Outcome:
        code, lines = captured(verbs.run_install,
                               verb_args(self.args, apply=True))
        self._show(lines)
        if code != 0:
            raise Stop(self._last(lines, "install failed"))
        return Outcome(DONE)

    def _stage_result(self, outcome: str, notes: Sequence[str]) -> Outcome:
        if outcome == l1_run.FAIL:
            raise Stop(notes[0] if notes else "failed")
        if outcome == l1_run.SKIPPED:
            return Outcome(SKIPPED, (), notes[0] if notes else "")
        return Outcome(DONE, tuple(notes))

    def apply_profile(self) -> Outcome:
        go = with_openshell(l1_run.stepgo(self.ctx, 3), self.h.openshell)
        outcome, notes = l1_run.import_profile(self.ctx, go)
        return self._stage_result(outcome, notes)

    def apply_provision(self) -> Outcome:
        states = member_states(self.h, fleet_for(self.h))
        provisioned = False
        for s in states:
            if s.state == "unknown":
                raise Stop(f"UNKNOWN: {s.why}")
            if s.settled:
                self._show([f"{s.name}: present and recorded "
                            f"({s.sandbox_id}); left alone"])
                continue
            env: Optional[Mapping[str, str]] = None
            if s.needs_create:
                if not self.key:
                    raise Stop(f"{s.name} must be created and there is no "
                               f"key")
                env = {**self.h.env, KEY_VARIABLE: self.key}
            code, lines = captured(
                verbs.run_provision,
                verb_args(self.args, name=s.name, apply=True), env=env)
            self._show(lines)
            if code != 0:
                raise Stop(self._last(lines, f"provision {s.name} failed"))
            provisioned = True
        if provisioned:
            return Outcome(DONE)
        return Outcome(SKIPPED, (), "every member is present and recorded")

    def apply_router(self) -> Outcome:
        code, lines = captured(verbs.run_router_config,
                               verb_args(self.args, apply=True))
        self._show(lines)
        if code != 0:
            raise Stop(self._last(lines, "router-config failed"))
        r = l1_run.step_6(self.ctx)
        if r.outcome == l1_run.FAIL:
            raise Stop(r.notes[0] if r.notes else "the router did not start")
        self._show(r.notes if r.outcome != l1_run.SKIPPED else ())
        if r.outcome == l1_run.SKIPPED:
            return Outcome(SKIPPED, (), r.notes[0] if r.notes else "")
        return Outcome(DONE)

    def apply_verify(self) -> Outcome:
        code, lines = captured(verify.main, self.args)
        self._show(lines)
        if code != 0:
            raise Stop(f"verify exited {code}; see its report above",
                       code=code)
        return Outcome(DONE)

    # --- dry run ---------------------------------------------------------

    def _dry(self, missing: str) -> int:
        foreseen: Optional[Tuple[str, str]] = None
        if self.interceptor_down:
            self.say("preflight: " + self.interceptor_down)
            foreseen = (PREFLIGHT, one_line(self.interceptor_down))
        if missing:
            self.say("preflight: " + missing)
            if foreseen is None:
                foreseen = (PREFLIGHT, one_line(missing))
        if self.key:
            self.say("key: given on stdin" if self.key_source == "stdin"
                     else f"key: ${KEY_VARIABLE} is set")
        else:
            self.say("key: none")
        total = len(STAGES)
        for n, (name, title) in enumerate(STAGES, 1):
            self.say(f"stage {n}/{total} {name}: {title}")
            try:
                result = getattr(self, "dry_" + name.replace("-", "_"))()
            except Stop as e:
                result = Outcome(WOULD_STOP, (), e.reason)
            except REFUSALS as e:
                result = Outcome(WOULD_STOP, (), str(e))
            self._show(result.notes)
            reason = one_line(result.reason)
            self.say(f"stage {n}/{total} {name}: {result.outcome}"
                     + (f" ({reason})" if reason else ""))
            if result.outcome == WOULD_STOP and foreseen is None:
                foreseen = (name, reason)
        if foreseen is not None:
            self.say(f"dry run: --apply would stop at {foreseen[0]}: "
                     f"{foreseen[1]}")
            return EXIT_STOPPED
        self.say(DRY_LINE)
        return EXIT_OK

    def _plus(self, cmd: l1_run.Cmd) -> str:
        return "+ " + l1_run.display_argv(
            _openshell_argv(cmd[1], self.h.openshell))

    def dry_image(self) -> Outcome:
        ctx = self.ctx
        r = self.read(l1_run.cmd_image_inspect(ctx.image))
        if l1_run.image_is_current(ctx, r):
            return Outcome(WOULD_SKIP, (), (
                f"{ctx.image} already carries the labels for Claude Code "
                f"{ctx.claude_code_version} and run-as {ctx.run_as_text}"))
        return Outcome(WOULD_RUN,
                       ("+ " + l1_run.display_argv(l1_run.cmd_build(ctx)[1]),))

    def dry_gateway_config(self) -> Outcome:
        code, lines = captured(verbs.run_gateway_config, verb_args(self.args))
        if code != 0:
            return Outcome(WOULD_STOP, tuple(lines), GATEWAY_STOP)
        return Outcome(WOULD_RUN, tuple(lines))

    def dry_install(self) -> Outcome:
        code, lines = captured(verbs.run_install,
                               verb_args(self.args, apply=False))
        if code != 0:
            return Outcome(WOULD_STOP, tuple(lines[:-1]),
                           self._last(lines, "install refuses"))
        if any(ln.startswith("would ") for ln in lines):
            return Outcome(WOULD_RUN, tuple(lines))
        return Outcome(WOULD_SKIP, tuple(lines),
                       "everything install writes is present")

    def dry_profile(self) -> Outcome:
        ctx = self.ctx
        notes = [self._plus(c) for c in (
            l1_run.cmd_image_paths(ctx.image),
            l1_run.cmd_profile_kit(
                ctx, (l1_run.ph("node path"), l1_run.ph("claude path")), True),
            l1_run.cmd_profile_list(ctx))]
        lst = self.read(l1_run.cmd_profile_list(ctx))
        if lst.code != 0:
            return Outcome(WOULD_STOP, tuple(notes), (
                f"listing the provider profiles failed: "
                f"{openshell_cli.first_line(lst.stderr)}"))
        try:
            copies = provider_profile.gateway_copies(
                l1_run.parse_json(lst.stdout))
        except provider_profile.ProfileError as e:
            return Outcome(WOULD_STOP, tuple(notes), str(e))
        pid = provider_profile.PROFILE_ID
        if not copies:
            notes.append(f"the gateway has no profile {pid}: would lint "
                         f"and import it")
            notes.append(self._plus(l1_run.cmd_profile_lint(ctx)))
        elif len(copies) == 1:
            notes.append("would update the gateway's copy only if it differs "
                         "from the rendering; it is never deleted")
        else:
            return Outcome(WOULD_STOP, tuple(notes), (
                f"the gateway lists {len(copies)} profiles with the ID "
                f"{pid}: ambiguous"))
        return Outcome(WOULD_RUN, tuple(notes))

    def dry_provision(self) -> Outcome:
        notes: List[str] = []
        for s in self.states:
            if s.settled:
                notes.append(f"{s.name}: present and recorded "
                             f"({s.sandbox_id}); left alone")
            elif s.state == "present":
                notes.append(f"{s.name}: present, not recorded: would record "
                             f"it")
            else:
                source = self.key_source or "none: --apply refuses"
                notes.append(f"{s.name}: would create it (sandbox create "
                             f"--auto-providers); key: {source}")
        if all(s.settled for s in self.states):
            return Outcome(WOULD_SKIP, tuple(notes),
                           "every member is present and recorded")
        return Outcome(WOULD_RUN, tuple(notes))

    def dry_router(self) -> Outcome:
        ctx, h = self.ctx, self.h
        notes: List[str] = []
        in_sync = False
        known = verbs.recorded(h) or []
        if not l1_kit.missing_members(fleet_for(h), known):
            _, lines = captured(verbs.run_router_config,
                                verb_args(self.args, apply=False))
            notes.extend(lines)
            in_sync = "in sync" in lines
        else:
            notes.append("router.json and selected.json are rendered when the "
                         "last member is recorded")
        state, ps = l1_run.router_state(ctx, self.read)
        assert ps is not None
        if ps.code != 0:
            return Outcome(WOULD_STOP, tuple(notes), (
                f"docker ps failed: {openshell_cli.first_line(ps.stderr)}"))
        if state == "running":
            notes.append("the router container is running: not started again")
        elif state is not None:
            return Outcome(WOULD_STOP, tuple(notes), (
                f"the router container exists and is {state}; bring-up never "
                f"removes a container: remove it yourself and rerun"))
        else:
            for cmd in (l1_run.cmd_router_build(ctx),
                        l1_run.cmd_router_run(ctx)):
                notes.append("+ " + l1_run.display_argv(cmd[1]))
        if state == "running" and in_sync:
            return Outcome(WOULD_SKIP, tuple(notes))
        return Outcome(WOULD_RUN, tuple(notes))

    def dry_verify(self) -> Outcome:
        return Outcome(WOULD_RUN, (
            "would run verify: its exit code is bring-up's, and success is "
            "reported only when it exits 0",))


def main(args: argparse.Namespace) -> int:
    return BringUp(args).run()
