"""amap-openshell: deploy AMAP on OpenShell hosts.

Host-side only: nothing here runs inside a sandbox, and nothing here sends or
receives a message. Every writing verb is a dry run without `--apply`.

The operator verbs (verbs.py) are `install`, `provision`, `deprovision`,
`list`, `router-config`, `gateway-config` and `teardown`. The host-wide options
(`--home`, `--fleet`, `--image`, `--run-as`, `--restart-policy`, `--openshell`,
`--gateway-toml`) come before the verb. `l1-kit` (l1_kit.py) writes the
host-side files for the L1 proof of concept and runs nothing. `l1-run`
(l1_run.py) runs the L1 runbook's steps 0-9 on this host: it runs `openshell`,
`docker` and the router. Each module is imported when it is used, so `--help`
does not search for the sibling checkouts. `verify` (verify.py) reads the host,
runs `openshell` and `docker`, and probes each sandbox from inside; it writes
nothing and has no `--apply`. It exits 0 when every check passed and 1 when any
check failed or could not be answered. `siblings` (siblings.py) sets up the
sibling checkouts from siblings.json and loads no sibling. `bring-up`
(bring_up.py) runs README "Bring-up" lines 1-8 as one rerunnable command and
ends with `verify`; it is a dry run without `--apply`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

REPO = Path(__file__).absolute().parent
PROG = "amap-openshell.py"
EXIT_USAGE = 2
# A sibling checkout the verb needs is missing: the operator's to fix.
EXIT_MISSING_SIBLING = 2
# l1-kit's exit code for a refusal (l1_kit.EXIT_REFUSED).
EXIT_REFUSED = 1

L1_KIT = "l1-kit"
L1_RUN = "l1-run"

# The verbs verbs.py implements.
VERBS = ("install", "provision", "deprovision", "list", "router-config",
         "gateway-config", "teardown")

# The verbs of PLAN.md phase 2, in that order. `verify` is S7's.
PHASE_2_VERBS = ("install", "provision", "deprovision", "verify", "list",
                 "router-config", "teardown")
NOT_IMPLEMENTED = ()
VERIFY = "verify"
SIBLINGS = "siblings"
# Not in VERBS, which names the verbs.py verbs.
BRING_UP = "bring-up"

HOME_VARIABLE = "AMAP_OPENSHELL_HOME"
DEFAULT_IMAGE = "amap-openshell-agent"   # l1_run.IMAGE


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Deploy AMAP on an OpenShell host. Every writing verb is a "
                    "dry run without --apply. Host-wide options come before "
                    "the verb.")
    ap.add_argument("--home", default=os.environ.get(HOME_VARIABLE),
                    help=f"the host directory (default: ${HOME_VARIABLE})")
    ap.add_argument("--fleet", default=str(REPO / "examples" / "fleet.json"),
                    help="the fleet.json template a new install starts from; "
                         "its fleet_domain is replaced (D21) "
                         "(default: examples/fleet.json)")
    ap.add_argument("--image", default=DEFAULT_IMAGE,
                    help="the sandbox image (default: %(default)s)")
    ap.add_argument("--run-as", default=None, metavar="UID:GID",
                    help="the one uid:gid of the router and every sandbox "
                         "(default: the invoking operator's)")
    ap.add_argument("--restart-policy", action="store_true",
                    help="add --restart-policy to the create commands")
    ap.add_argument("--openshell", default="openshell",
                    help="the openshell program to run (default: openshell)")
    ap.add_argument("--gateway-toml", default=None,
                    help="the gateway.toml gateway-config reads (default: "
                         "under $XDG_CONFIG_HOME or $HOME/.config)")
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def verb(name: str, help_text: str, apply: bool = False,
             takes_name: bool = False) -> argparse.ArgumentParser:
        stub = name in NOT_IMPLEMENTED
        text = f"not implemented: {help_text}" if stub else help_text
        p = sub.add_parser(name, help=text, description=text)
        if name == VERIFY:
            p.add_argument("--docker", default=None,
                           help="the docker program to run (default: docker)")
            p.add_argument("--router-container", default=None,
                           help="the router's container name (default: "
                                "$CONTAINER, else amap-router-local)")
            p.add_argument("--router-image", default=None,
                           help="the router's image name (default: $IMAGE, "
                                "else amap-router-local)")
        if takes_name:
            p.add_argument("name", help="the sandbox name")
        if apply:
            p.add_argument("--apply", action="store_true",
                           help="write (default: report only)")
        return p

    inst = verb("install", "write the payload, a new fleet.json from the "
                           "template with fleet_domain openshell.<host>.<base>, "
                           "and the roster directory; router.json once every "
                           "member is recorded", apply=True)
    inst.add_argument("--fleet-domain-base", metavar="BASE", default=None,
                      help="a new fleet.json gets fleet_domain "
                           "openshell.<host>.BASE (default: internal, "
                           "non-routable); a domain you control is what "
                           "routing between hosts will need. An existing "
                           "fleet.json is never rewritten")
    verb("provision", "create the sandbox's lanes, policy and command, create "
                      "the sandbox and record its ID in membership.json",
         apply=True, takes_name=True)
    verb("deprovision", "delete the sandbox, empty its lanes and de-enrol it",
         apply=True, takes_name=True)
    verb("verify", "report PASS, FAIL or UNKNOWN, including the in-sandbox "
                   "write attempt")
    verb("list", "list the fleet's sandboxes against membership.json")
    verb("router-config", "render the router's config", apply=True)
    verb("gateway-config", "print the gateway fragment and compare it with "
                           "gateway.toml (read-only)")
    verb("teardown", "remove what install wrote", apply=True)

    up_help = ("build the image, check the gateway config, install, import the "
               "provider profile, provision every member, start the router, "
               "then verify (D15)")
    up = sub.add_parser(BRING_UP, help=up_help, description=up_help + ". A dry "
                        "run without --apply; rerunnable; the last line is "
                        "`AMAP is up` only when verify passed.")
    up.add_argument("--claude-code-version", default=None, metavar="VERSION",
                    help="the Claude Code version to build the image with "
                         "(default: $CLAUDE_CODE_VERSION; there is no other)")
    up.add_argument("--api-key-stdin", action="store_true",
                    help="read the provider key as one line from stdin instead "
                         "of $ANTHROPIC_API_KEY")
    up.add_argument("--apply", action="store_true",
                    help="bring AMAP up (default: report only)")

    sib_help = ("clone or fetch the sibling checkouts beside this repository "
                "and check out the pins in siblings.json (amap-spec is cloned "
                "or fast-forwarded)")
    sib = sub.add_parser(SIBLINGS, help=sib_help, description=sib_help)
    sib.add_argument("--apply", action="store_true",
                     help="clone, fetch and check out (default: report only)")

    kit = sub.add_parser(
        L1_KIT, help="write the host-side files for the L1 proof of concept",
        description="Write the host-side files for the L1 proof of concept. "
                    "Every action is a dry run without --apply.")
    actions = kit.add_subparsers(dest="action", required=True, metavar="ACTION")
    prepare = actions.add_parser(
        "prepare", help="write the payload, lanes, policies and create commands",
        description="Write, under --home, the payload, every member's lanes, "
                    "each member's policy and create command, the fleet "
                    "policy and the gateway fragment.")
    prepare.add_argument("--home", required=True,
                         help="the host directory ($AMAP_OPENSHELL_HOME)")
    prepare.add_argument("--fleet", required=True, help="the fleet policy file")
    prepare.add_argument("--run-as", required=True, metavar="UID:GID",
                         help="the one uid:gid of the router and every sandbox")
    prepare.add_argument("--image", required=True,
                         help="the sandbox image, as built")
    prepare.add_argument("--restart-policy", action="store_true",
                         help="add --restart-policy to the create commands "
                              "(the installed OpenShell must accept it)")
    prepare.add_argument("--apply", action="store_true",
                         help="write (default: report only)")
    record = actions.add_parser(
        "record", help="record a created sandbox's ID",
        description="Record a created sandbox's OpenShell ID in "
                    "membership.json. When every member is recorded, also "
                    "write selected.json and router.json.")
    record.add_argument("--home", required=True,
                        help="the host directory ($AMAP_OPENSHELL_HOME)")
    record.add_argument("name", help="the sandbox name")
    record.add_argument("id", help="the ID from `sandbox get --output json`")
    record.add_argument("--apply", action="store_true",
                        help="write (default: report only)")

    profile = actions.add_parser(
        "profile", help="render this deployment's provider profile",
        description="Render, under --home, this deployment's provider "
                    "profile with its binaries set to the given real paths "
                    "in the image.")
    profile.add_argument("--home", required=True,
                         help="the host directory ($AMAP_OPENSHELL_HOME)")
    profile.add_argument("--binary", dest="binaries", action="append",
                         required=True, metavar="PATH",
                         help="a real path in the image of an executable that "
                              "may reach the profile's endpoint; repeat")
    profile.add_argument("--apply", action="store_true",
                         help="write (default: report only)")

    run = sub.add_parser(
        L1_RUN,
        help="run the L1 proof of concept on this host "
             "(docs/L1-RUNBOOK.md steps 0-9)",
        description="Run docs/L1-RUNBOOK.md steps 0-9. It runs openshell, "
                    "docker and the router. Without --apply it prints each "
                    "command and runs nothing.")
    which = run.add_mutually_exclusive_group()
    which.add_argument("--from", dest="from_step", type=int, choices=range(10),
                       metavar="N",
                       help="start at step N (steps 0 and 2 still run as "
                            "gates)")
    which.add_argument("--only", dest="only_step", type=int,
                       choices=range(10), metavar="N",
                       help="run only step N (and the gates)")
    run.add_argument("--apply", action="store_true",
                     help="run the commands (default: print them only)")
    return ap


def _missing_sandy(error: ImportError) -> bool:
    """Whether this import failed for the one reason an operator can fix.

    `policy` loads sandy's `fleet_policy` at import (decision D1), so a missing
    checkout surfaces here rather than in a verb. The class is matched by name
    because importing `policy` to name it is what raises. Every other
    `ImportError` is a defect and is re-raised.
    """
    return type(error).__name__ == "SandyNotFound"


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # --home, then $AMAP_OPENSHELL_HOME, then the recorded home (home_record).
    # The record fills both, so every verb, verify, bring-up and l1-run see
    # it the same way. `siblings` runs before anything is installed, and
    # l1-kit's actions take --home explicitly, so neither reads it.
    if (args.command != SIBLINGS and not getattr(args, "home", None)
            and not os.environ.get(HOME_VARIABLE)):
        import home_record
        try:
            recorded = home_record.read()
        except home_record.RecordError as e:
            print(f"{PROG}: {e}", file=sys.stderr)
            return EXIT_USAGE
        if recorded:
            os.environ[HOME_VARIABLE] = recorded
            if hasattr(args, "home"):
                args.home = recorded
    needs_home = ((args.command in VERBS or args.command in (VERIFY, BRING_UP))
                  and args.command != "gateway-config")
    if needs_home and not args.home:
        print(f"{PROG} {args.command}: --home is required (or set "
              f"${HOME_VARIABLE})", file=sys.stderr)
        return EXIT_USAGE
    try:
        # lazy: --help needs no siblings
        if args.command == L1_KIT:
            import l1_kit as verb
        elif args.command == L1_RUN:
            import l1_run as verb
        elif args.command == VERIFY:
            import verify as verb
        elif args.command == BRING_UP:
            import bring_up as verb
        elif args.command == SIBLINGS:
            import siblings as verb
        else:
            import verbs as verb
    except ImportError as e:
        if not _missing_sandy(e):
            raise
        print(f"{PROG} {args.command}: {e}", file=sys.stderr)
        return EXIT_MISSING_SIBLING
    return verb.main(args)
