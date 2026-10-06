"""The gateway's configuration file, read and never written.

`gateway-config` prints the fragment DESIGN.md section 6 requires and reports,
for each of its three settings, whether the operator's `gateway.toml` has it:
PRESENT, ABSENT, or UNKNOWN. UNKNOWN is never a pass. A file that is absent is a
different answer from one that cannot be read: the first says the settings are
not there, the second says nothing is known.

The fragment is `l1_kit.GATEWAY_FRAGMENT` (OpenShell
main@acbac9c:docs/how-it-works/sandboxes/runtimes.mdx:120-131). The settings
checked are taken from its own lines, so the fragment and the check cannot
drift. A setting counts only under the table the fragment puts it in.

`interceptor_report` does the same for the mounts interceptor's registration:
the block `interceptor.wire.gateway_fragment(home)` renders, grouped with its
bindings tables (docs/INTERCEPTOR.md section 7, check V1).

Standard library only (plus `interceptor.wire`), and Python 3.9 compatible.
"""

from __future__ import annotations

import os
import posixpath
from typing import List, Mapping, NamedTuple, Optional, Sequence, Tuple

import l1_kit
from interceptor import wire

FRAGMENT = l1_kit.GATEWAY_FRAGMENT

PRESENT, ABSENT, UNKNOWN = "PRESENT", "ABSENT", "UNKNOWN"
READ, NOT_THERE, UNREADABLE = "read", "absent", "unreadable"


def setting_lines(text: str) -> List[str]:
    """A TOML file's setting lines: comments and blanks dropped, whitespace
    normalized."""
    out = []
    for raw in text.splitlines():
        line = " ".join(raw.split("#", 1)[0].split())
        if line:
            out.append(line)
    return out


def gateway_toml_path(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Where the gateway's `gateway.toml` is, or None when neither
    `XDG_CONFIG_HOME` nor `HOME` says."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME")
    if not base:
        home = env.get("HOME")
        if not home:
            return None
        base = posixpath.join(home, ".config")
    return posixpath.join(base, "openshell", "gateway.toml")


def _is_table(line: str) -> bool:
    return line.startswith("[") and line.endswith("]")


def _normal(line: str) -> str:
    """`key=value` and `key = value` are one setting."""
    if "=" in line and not _is_table(line):
        key, value = line.split("=", 1)
        return f"{key.strip()} = {value.strip()}"
    return line


def _settings(text_lines: List[str]) -> List[Tuple[str, str]]:
    """`(table, "key = value")` for each setting line, in order."""
    table = ""
    out: List[Tuple[str, str]] = []
    for line in text_lines:
        if _is_table(line):
            table = line
        elif "=" in line:
            out.append((table, _normal(line)))
    return out


def _required() -> Tuple[Tuple[str, str], ...]:
    return tuple(_settings(setting_lines(FRAGMENT)))


def required_settings() -> Tuple[str, ...]:
    """The fragment's settings, as its own `key = value` lines."""
    return tuple(line for _, line in _required())


class Reading(NamedTuple):
    path: Optional[str]
    state: str                 # READ, NOT_THERE or UNREADABLE
    detail: str                # the OSError's text, or why the path is unknown
    lines: Tuple[str, ...]     # setting_lines of the file, when it was read


def read_gateway_toml(path: Optional[str]) -> Reading:
    if not path:
        return Reading(None, UNREADABLE,
                       "the gateway configuration file cannot be located: "
                       "neither XDG_CONFIG_HOME nor HOME is set", ())
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return Reading(path, NOT_THERE, "", ())
    except (OSError, UnicodeDecodeError) as e:
        return Reading(path, UNREADABLE, str(e), ())
    return Reading(path, READ, "", tuple(setting_lines(text)))


def settings_report(reading: Reading) -> List[Tuple[str, str, str]]:
    """`(setting, PRESENT|ABSENT|UNKNOWN, why)` for each required setting. A
    setting is never guessed: an unreadable file leaves every one UNKNOWN."""
    required = _required()
    if reading.state == NOT_THERE:
        return [(line, ABSENT, "the gateway configuration file is absent")
                for _, line in required]
    if reading.state == UNREADABLE:
        why = f"the gateway configuration file cannot be read: {reading.detail}"
        if reading.path is None:
            why = reading.detail
        return [(line, UNKNOWN, why) for _, line in required]
    have = set(_settings(list(reading.lines)))
    out = []
    for table, line in required:
        if (table, line) in have:
            out.append((line, PRESENT, f"found under {table}"))
        else:
            out.append((line, ABSENT, f"not set under {table}"))
    return out


# --- the mounts interceptor's registration -----------------------------------

INTERCEPTORS_TABLE = "[[openshell.gateway.interceptors]]"
BINDINGS_TABLE = "[[openshell.gateway.interceptors.bindings]]"
ONE_BLOCK = "exactly one block registers the mounts interceptor"
ONE_BINDING = "that block has exactly one bindings table"
NOT_DISABLED = "no `disabled = true` in that block or its binding"
NOT_FAIL_OPEN = "no `failure_policy = \"fail_open\"` in that block or its binding"


def _canon(line: str) -> str:
    """`key = value` with every space removed from the value and single quotes
    read as double quotes, so spellings of one setting compare equal."""
    if "=" not in line:
        return "".join(line.split())
    key, value = line.split("=", 1)
    value = "".join(value.split()).replace("'", '"')
    return f"{key.strip()} = {value}"


Group = Tuple[List[str], List[List[str]]]


def _interceptor_blocks(lines: Sequence[str]) -> List[Group]:
    """The settings of each interceptors table, with the settings of each
    bindings table that follows it. A table line that is neither ends the group.
    An inline `bindings = [...]` is one setting of the block, not a binding."""
    groups: List[Group] = []
    current: Optional[Group] = None
    binding: Optional[List[str]] = None
    for line in lines:
        if line == INTERCEPTORS_TABLE:
            current = ([], [])
            groups.append(current)
            binding = None
        elif line == BINDINGS_TABLE and current is not None:
            binding = []
            current[1].append(binding)
        elif _is_table(line):
            current, binding = None, None
        elif "=" in line and current is not None:
            (binding if binding is not None else current[0]).append(_canon(line))
    return groups


def interceptor_report(reading: Reading, home: str
                       ) -> List[Tuple[str, str, str]]:
    """`(claim, PRESENT|ABSENT|UNKNOWN, why)` for the mounts interceptor's
    registration. PRESENT means the requirement is met. The block is the one
    whose `name` is the interceptor's. Binding rows hold only when there is
    exactly one bindings table. An inline-table `bindings = [...]` reads as
    missing bindings tables, which is a failure."""
    wanted = gateway_fragment_settings(home)
    rows = ([(f"{INTERCEPTORS_TABLE} {s}") for s in wanted[0]]
            + [(f"{BINDINGS_TABLE} {s}") for s in wanted[1]]
            + [ONE_BLOCK, ONE_BINDING, NOT_DISABLED, NOT_FAIL_OPEN])
    if reading.state == NOT_THERE:
        return [(r, ABSENT, "the gateway configuration file is absent")
                for r in rows]
    if reading.state == UNREADABLE:
        why = reading.detail if reading.path is None else (
            f"the gateway configuration file cannot be read: {reading.detail}")
        return [(r, UNKNOWN, why) for r in rows]

    name_line = next(s for s in wanted[0] if s.startswith("name = "))
    mine = [g for g in _interceptor_blocks(reading.lines)
            if name_line in g[0]]
    if len(mine) != 1:
        why = ("no block names the mounts interceptor" if not mine else
               f"{len(mine)} blocks name the mounts interceptor")
        return [(r, ABSENT, why) for r in rows]
    settings, bindings = mine[0]
    out: List[Tuple[str, str, str]] = []
    for s in wanted[0]:
        ok = s in settings
        out.append((f"{INTERCEPTORS_TABLE} {s}", PRESENT if ok else ABSENT,
                    "found" if ok else "not set in the block"))
    one = len(bindings) == 1
    for s in wanted[1]:
        ok = one and s in bindings[0]
        why = ("found" if ok else "not set in the one bindings table" if one
               else f"the block has {len(bindings)} bindings tables, not one")
        out.append((f"{BINDINGS_TABLE} {s}", PRESENT if ok else ABSENT, why))
    everything = settings + [s for b in bindings for s in b]
    out.append((ONE_BLOCK, PRESENT, "found"))
    out.append((ONE_BINDING, PRESENT if one else ABSENT,
                "found" if one else f"{len(bindings)} bindings tables"))
    bad = "disabled = true" in everything
    out.append((NOT_DISABLED, ABSENT if bad else PRESENT,
                "`disabled = true` is set" if bad else "not set"))
    bad = 'failure_policy = "fail_open"' in everything
    out.append((NOT_FAIL_OPEN, ABSENT if bad else PRESENT,
                "`failure_policy = \"fail_open\"` is set" if bad
                else "not set"))
    return out


def gateway_fragment_settings(home: str) -> Tuple[List[str], List[str]]:
    """The canonical settings of the interceptor fragment's service table and
    its bindings table."""
    service: List[str] = []
    binding: List[str] = []
    table = ""
    for line in setting_lines(wire.gateway_fragment(home)):
        if _is_table(line):
            table = line
        elif "=" in line:
            (service if table == INTERCEPTORS_TABLE else binding).append(
                _canon(line))
    return service, binding
