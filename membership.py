"""The membership record: `membership.json`.

`membership.json` records which OpenShell sandboxes are members of the fleet, as
`(workspace, name, id)` triples. It is this deployment's own record of a
security decision ("this sandbox, and not merely a sandbox of this name, is a
member"), so it is strict: unknown keys, a wrong version, a duplicate name, a
duplicate ID and entries from more than one workspace are all refused.

The triple, and not the name alone, is pinned because OpenShell reuses names: a
deleted sandbox's name can be given to a new sandbox, which has a new ID:
  OpenShell main@acbac9c:docs/extensibility/supervisor-middleware/operations.mdx:27
A name that comes back under a different ID is a different sandbox, and `record`
and `check` report that as an `IdMismatch` instead of accepting it.

Names are unique only within a workspace:
  OpenShell main@acbac9c:crates/openshell-server/src/persistence/tests.rs:1027
so a fleet lives in exactly one workspace and every entry must carry it.

`load` returns `None` for an absent file and `[]` for a file whose `members` is
empty. Those are different answers: the first says nothing has been recorded, the
second says a record exists and lists no one.

Nothing here talks to OpenShell. The functions validate, compare and store what
they are given.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import (Any, Dict, Iterable, List, NamedTuple, Optional, Sequence,
                    Union)

VERSION = 1

# OpenShell main@acbac9c:crates/openshell-server/src/grpc/mod.rs:140
# A workspace, sandbox or service name is at most 19 characters, so that three
# names and two `--` delimiters fit one 63-character DNS label.
MAX_LABEL_LEN = 19

# The charset and the hyphen rules are OpenShell's validate_dns1123_label:
#   OpenShell main@acbac9c:crates/openshell-server/src/grpc/validation.rs:120-160
# The sandbox-name length limit:
#   OpenShell main@acbac9c:crates/openshell-server/src/grpc/validation.rs:241-252
# The workspace-name rule, which is the same one:
#   OpenShell main@acbac9c:crates/openshell-server/src/grpc/workspace.rs:78-90
# The ID is a UUIDv4 string made by the server:
#   OpenShell main@acbac9c:crates/openshell-server/src/grpc/sandbox.rs:504
# The format is not a contract, so an ID is treated as an opaque string here:
# it is compared and never interpreted.
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")

_KEYS = ("workspace", "name", "id")
_TOP_KEYS = ("version", "members")


class Member(NamedTuple):
    workspace: str
    name: str
    id: str


class MembershipError(Exception):
    """A membership record or a proposed change to one is refused."""


class IdMismatch(MembershipError):
    """A name is recorded under one ID and observed under another."""

    def __init__(self, recorded: Member, observed: Member) -> None:
        self.recorded = recorded
        self.observed = observed
        super().__init__(
            f"ID mismatch for {recorded.name!r} in workspace "
            f"{recorded.workspace!r}: membership.json records {recorded.id!r}, "
            f"and the sandbox now has {observed.id!r}. OpenShell reuses a "
            f"deleted sandbox's name under a new ID, so this is a different "
            f"sandbox. Deprovision the old record before recording this one.")


# ---------------------------------------------------------------------------
# Field rules. Each returns the reason a value is refused, or None when valid.

def label_problem(value: object, what: str) -> Optional[str]:
    """Why `value` is not a valid OpenShell `what` (a sandbox or workspace
    name), or None when it is."""
    if not isinstance(value, str):
        return f"a {what} must be a string, got {type(value).__name__}"
    if not value:
        return (f"a {what} is required: OpenShell generates one when none is "
                f"given, so record the name OpenShell reports")
    if len(value) > MAX_LABEL_LEN:
        return (f"{value!r} is {len(value)} characters; an OpenShell {what} "
                f"is at most {MAX_LABEL_LEN}")
    if not all(c in _LABEL_CHARS for c in value):
        return f"{value!r} may contain only lowercase letters a-z, digits and '-'"
    if value.startswith("-") or value.endswith("-"):
        return f"{value!r} must not start or end with '-'"
    if "--" in value:
        return f"{value!r} must not contain '--'"
    return None


def name_problem(name: object) -> Optional[str]:
    return label_problem(name, "sandbox name")


def workspace_problem(workspace: object) -> Optional[str]:
    return label_problem(workspace, "workspace name")


def id_problem(sandbox_id: object) -> Optional[str]:
    if not isinstance(sandbox_id, str):
        return f"a sandbox ID must be a string, got {type(sandbox_id).__name__}"
    if not sandbox_id:
        return "a sandbox ID is required"
    if any(c.isspace() or not c.isprintable() for c in sandbox_id):
        return f"{sandbox_id!r} must not contain whitespace or control characters"
    return None


def _member_problem(member: Member) -> Optional[str]:
    return (workspace_problem(member.workspace) or name_problem(member.name)
            or id_problem(member.id))


def _by_name(members: Iterable[Member]) -> List[Member]:
    return sorted(members, key=lambda m: m.name)


# ---------------------------------------------------------------------------
# The file.

def parse(doc: object, where: str) -> List[Member]:
    """The members of a decoded `membership.json`, sorted by name. `where`
    names the source in every refusal."""
    if not isinstance(doc, dict):
        raise MembershipError(f"{where}: expected a JSON object at the top level")
    if set(doc) != set(_TOP_KEYS):
        raise MembershipError(
            f"{where}: the top level must have exactly {list(_TOP_KEYS)}, "
            f"got {sorted(doc)}")
    version = doc["version"]
    if isinstance(version, bool) or version != VERSION:
        raise MembershipError(
            f"{where}: 'version' is {version!r}; this build only understands "
            f"{VERSION}")
    entries = doc["members"]
    if not isinstance(entries, list):
        raise MembershipError(f"{where}: 'members' must be an array")

    members: List[Member] = []
    names: Dict[str, int] = {}
    ids: Dict[str, str] = {}
    for i, entry in enumerate(entries):
        at = f"{where}: members[{i}]"
        if not isinstance(entry, dict) or set(entry) != set(_KEYS):
            raise MembershipError(
                f"{at}: an entry must be an object with exactly "
                f"{list(_KEYS)}, got {entry!r}")
        member = Member(entry["workspace"], entry["name"], entry["id"])
        reason = _member_problem(member)
        if reason:
            raise MembershipError(f"{at}: {reason}")
        if member.name in names:
            raise MembershipError(
                f"{at}: {member.name!r} is recorded twice (also members"
                f"[{names[member.name]}]); a name has one ID")
        if member.id in ids:
            raise MembershipError(
                f"{at}: the ID {member.id!r} is recorded under both "
                f"{ids[member.id]!r} and {member.name!r}")
        names[member.name] = i
        ids[member.id] = member.name
        members.append(member)

    spaces = sorted({m.workspace for m in members})
    if len(spaces) > 1:
        raise MembershipError(
            f"{where}: entries span more than one OpenShell workspace "
            f"({', '.join(spaces)}); names are unique only within a workspace, "
            f"so a fleet lives in one")
    return _by_name(members)


def load(path: Union[str, os.PathLike]) -> Optional[List[Member]]:
    """The recorded members, or None when there is no file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise MembershipError(f"cannot read {p}: {e}") from e
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise MembershipError(f"{p} is not valid JSON: {e}") from e
    return parse(doc, str(p))


def dump(members: Iterable[Member]) -> str:
    """The text of a `membership.json` for `members`. An invalid list is
    refused, so the text is always one `load` accepts."""
    doc: Dict[str, Any] = {
        "version": VERSION,
        "members": [{"workspace": m.workspace, "name": m.name, "id": m.id}
                    for m in _by_name(members)],
    }
    parse(doc, "the members to write")
    return json.dumps(doc, indent=2) + "\n"


def write(path: Union[str, os.PathLike], members: Iterable[Member]) -> None:
    """Replace `path` with `members`, all at once: a reader sees the old file or
    the new one. The parent directory must exist. A failure leaves the old file
    and no temporary file."""
    text = dump(members)
    p = Path(path)
    parent = p.parent
    if not parent.is_dir():
        raise MembershipError(f"cannot write {p}: {parent} is not a directory")
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".membership.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(p))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Changes and comparisons. None of these mutates its input.

def find(members: Iterable[Member], name: str) -> Optional[Member]:
    for m in members:
        if m.name == name:
            return m
    return None


def record(members: Sequence[Member], member: Member) -> List[Member]:
    """`members` with `member` recorded. The same triple changes nothing. The
    same name under a new ID is an `IdMismatch`: it is never accepted silently."""
    reason = _member_problem(member)
    if reason:
        raise MembershipError(reason)
    if any(m.workspace != member.workspace for m in members):
        raise MembershipError(
            f"recording {member.name!r} in workspace {member.workspace!r} would "
            f"span more than one OpenShell workspace; a fleet lives in one")
    existing = find(members, member.name)
    if existing is not None:
        if existing.id == member.id:
            return _by_name(members)
        raise IdMismatch(existing, member)
    for m in members:
        if m.id == member.id:
            raise MembershipError(
                f"the ID {member.id!r} is already recorded as {m.name!r}; "
                f"OpenShell has no rename, so one ID is one name")
    return _by_name([*members, member])


def remove(members: Sequence[Member], name: str) -> List[Member]:
    if find(members, name) is None:
        raise MembershipError(f"{name!r} is not recorded in membership.json")
    return _by_name(m for m in members if m.name != name)


def check(members: Iterable[Member], observed: Member) -> Optional[str]:
    """None when exactly `observed` is recorded, else the reason it is not."""
    recorded = find(members, observed.name)
    if recorded is None:
        return (f"{observed.name!r} (workspace {observed.workspace!r}) is not "
                f"recorded in membership.json")
    if recorded.workspace != observed.workspace:
        return (f"workspace mismatch for {observed.name!r}: recorded in "
                f"{recorded.workspace!r}, observed in {observed.workspace!r}; "
                f"a fleet spans one OpenShell workspace")
    if recorded.id != observed.id:
        return str(IdMismatch(recorded, observed))
    return None


def require_workspace(members: Iterable[Member], workspace: str) -> None:
    """Refuse when any member is outside `workspace`."""
    outside = [m for m in members if m.workspace != workspace]
    if outside:
        listed = ", ".join(f"{m.name!r} (in {m.workspace!r})" for m in outside)
        raise MembershipError(
            f"member(s) outside the fleet's workspace {workspace!r}: {listed}")
