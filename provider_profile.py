"""This deployment's OpenShell provider profile: the shipped template, its
rendering and its comparison with the gateway's copy.

Pure: no subprocess, no file writes. The template is
`providers/amap-claude-code.json`. It has `"binaries": []`, and `render_profile`
fills that list from the real paths of the built image, so nothing here guesses
a path.

OpenShell facts, cited at OpenShell main@acbac9c (v0.1.2 where the lines differ):

- The document's fields and the identity fields:
  docs/how-it-works/providers/profiles.mdx:477-490; `id` and `display_name` have
  no serde default: crates/openshell-providers/src/profiles.rs:683-712 (v0.1.2:
  691-720).
- The profile ID is lowercase kebab-case: profiles.mdx:327, 329-341 and
  profiles.rs:1996-2006 (`is_valid_profile_id`), lint rules at 2009-2110.
- Reserved credential variable names: crates/openshell-core/src/secrets.rs:788-817.
- Both scalar and `{path}` forms of a binary deserialize: profiles.rs:1124-1158.

Standard library only, and Python 3.9 compatible.
"""

from __future__ import annotations

import copy
import json
import posixpath
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

PROFILE_ID = "amap-claude-code"
PROFILE_NAME = PROFILE_ID + ".json"
TEMPLATE = Path(__file__).absolute().parent / "providers" / PROFILE_NAME
KEY_VARIABLE = "ANTHROPIC_API_KEY"
REQUIRED_FIELDS = ("id", "display_name")               # serde-required
IDENTITY_FIELDS = ("id", "display_name", "description")
CATEGORIES = ("other", "inference", "agent", "source_control", "messaging",
              "data", "knowledge")
ALLOWED_KEYS = ("id", "display_name", "description", "category",
                "inference_capable", "credentials", "discovery", "endpoints",
                "binaries")
IMAGE_PATH_KEYS = ("node", "claude")
IMAGE_PATHS_SCRIPT = ('printf "node=%s\\nclaude=%s\\n" '
                      '"$(readlink -f "$(command -v node)")" '
                      '"$(readlink -f "$(command -v claude)")"')

_ID_RE = re.compile(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")
_VERSIONED_ENV_RE = re.compile(r"\Av\d+_\w+\Z")
_SECRET_ENV_RE = re.compile(r"\As[0-9a-f]{64}_\w+\Z")
_BAD_PATH_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")


class ProfileError(ValueError):
    """The profile or the image's paths are not what the runner needs."""


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def load_template(path: Optional[str] = None) -> Dict[str, Any]:
    """The shipped template, parsed and checked as a template (empty
    `binaries`)."""
    where = Path(path) if path is not None else TEMPLATE
    try:
        with open(where, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise ProfileError(f"cannot read the provider profile {where.name}: "
                           f"{e}") from e
    if not isinstance(doc, dict):
        raise ProfileError(f"{where.name} is not a JSON object")
    problems = profile_problems(doc, rendered=False)
    if problems:
        raise ProfileError(f"{where.name}: " + "; ".join(problems))
    return doc


def binaries_problem(paths: Sequence[str]) -> Optional[str]:
    """Why `paths` cannot be the profile's `binaries`, or None."""
    if not isinstance(paths, (list, tuple)) or not paths:
        return "binaries is empty: the profile would allow nothing"
    for p in paths:
        if not isinstance(p, str) or not p:
            return f"binary {p!r} is not a non-empty string"
        if _BAD_PATH_CHARS.search(p):
            return f"binary {p!r} has whitespace or a control character"
        if not p.startswith("/"):
            return f"binary {p!r} is not an absolute path"
        if posixpath.normpath(p) != p:
            return f"binary {p!r} is not a normalized path"
    return None


def render_profile(template: Mapping[str, Any], binaries: Sequence[str]
                   ) -> Dict[str, Any]:
    """A copy of `template` with `binaries` set to `binaries`, without
    duplicates and in order. The template is never changed."""
    reason = binaries_problem(binaries)
    if reason:
        raise ProfileError(reason)
    result = copy.deepcopy(dict(template))
    seen: List[str] = []
    for p in binaries:
        if p not in seen:
            seen.append(p)
    result["binaries"] = seen
    problems = profile_problems(result, rendered=True)
    if problems:
        raise ProfileError("; ".join(problems))
    return result


def profile_text(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=2) + "\n"


def profile_problems(doc: Mapping[str, Any], rendered: bool = True
                     ) -> List[str]:
    """Every way `doc` breaks a rule of OpenShell's profile schema or of this
    deployment's D10 profile."""
    probs: List[str] = []
    unknown = sorted(str(k) for k in doc if k not in ALLOWED_KEYS)
    if unknown:
        probs.append("unknown top-level key(s): " + ", ".join(unknown))
    for key in sorted(set(REQUIRED_FIELDS) | set(IDENTITY_FIELDS)):
        if not _is_text(doc.get(key)):
            probs.append(f"{key} must be a non-empty string")
    pid = doc.get("id")
    if isinstance(pid, str):
        if not _ID_RE.match(pid):
            probs.append(f"id {pid!r} is not lowercase kebab-case")
        if pid == "claude-code":
            probs.append("id must differ from OpenShell's example, "
                         "claude-code")
    if doc.get("category") not in CATEGORIES:
        probs.append(f"category must be one of {', '.join(CATEGORIES)}")

    creds = doc.get("credentials")
    names: List[Any] = []
    if not isinstance(creds, list) or len(creds) != 1 \
            or not isinstance(creds[0], dict):
        probs.append("there must be exactly one credential")
    else:
        cred = creds[0]
        names.append(cred.get("name"))
        env = cred.get("env_vars")
        if env != [KEY_VARIABLE]:
            probs.append(f"the credential's env_vars must be "
                         f"[{KEY_VARIABLE!r}] (D10)")
        for var in env if isinstance(env, list) else []:
            if isinstance(var, str) and (_VERSIONED_ENV_RE.match(var)
                                         or _SECRET_ENV_RE.match(var)):
                probs.append(f"env var {var!r} is a reserved name")
        if cred.get("auth_style") == "header" \
                and not _is_text(cred.get("header_name")):
            probs.append("auth_style header requires a header_name")

    disc = doc.get("discovery")
    listed = disc.get("credentials") if isinstance(disc, dict) else None
    if not isinstance(listed, list) or not listed:
        probs.append("discovery.credentials must be a non-empty list")
    else:
        extra = [n for n in listed if n not in names]
        if extra:
            probs.append(f"discovery.credentials names undeclared "
                         f"credential(s): {extra}")

    eps = doc.get("endpoints")
    if not isinstance(eps, list) or len(eps) != 1 \
            or not isinstance(eps[0], dict):
        probs.append("there must be exactly one endpoint")
    else:
        ep = eps[0]
        if ep.get("port") != 443:
            probs.append("the endpoint's port must be 443")
        if ep.get("protocol") != "rest":
            probs.append("the endpoint's protocol must be rest")
        if ep.get("tls") == "skip":
            probs.append("the endpoint must not skip TLS")
        if "allow_uninspected_credentials" in ep:
            probs.append("the endpoint must not set "
                         "allow_uninspected_credentials")

    bins = doc.get("binaries")
    if rendered:
        reason = binaries_problem(bins) if isinstance(bins, list) \
            else "binaries must be a list"
        if reason:
            probs.append(reason)
    elif bins != []:
        probs.append("the template's binaries must be empty")
    return probs


def parse_image_paths(stdout: str) -> Dict[str, str]:
    """The `key=value` lines `IMAGE_PATHS_SCRIPT` prints. Every key must be
    present once, with an absolute, normalized value. There is no default."""
    found: Dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or key not in IMAGE_PATH_KEYS:
            raise ProfileError(f"unexpected line in the image's paths: "
                               f"{line!r}")
        if key in found:
            raise ProfileError(f"the image's paths name {key} twice")
        found[key] = value.strip()
    for key in IMAGE_PATH_KEYS:
        if key not in found:
            raise ProfileError(f"the image's paths do not name {key}")
        reason = binaries_problem([found[key]])
        if reason:
            raise ProfileError(f"the image's path for {key} is unusable: "
                               f"{reason}")
    return found


def image_binaries(paths: Mapping[str, str]) -> List[str]:
    out: List[str] = []
    for key in IMAGE_PATH_KEYS:
        if paths[key] not in out:
            out.append(paths[key])
    return out


def _binary_path(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("path")
    return item


def comparable(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """The fields that decide whether two copies are the same profile. Every
    other field, such as the defaults an export adds, is ignored."""
    out: Dict[str, Any] = {k: doc.get(k) for k in
                           ("id", "display_name", "description", "category",
                            "inference_capable")}
    out["credentials"] = [
        {k: c.get(k) for k in ("name", "env_vars", "required", "auth_style",
                               "header_name")}
        for c in doc.get("credentials") or [] if isinstance(c, dict)]
    disc = doc.get("discovery")
    out["discovery_credentials"] = (disc.get("credentials")
                                    if isinstance(disc, dict) else None)
    out["endpoints"] = [
        {k: e.get(k) for k in ("host", "port", "protocol", "access",
                               "enforcement")}
        for e in doc.get("endpoints") or [] if isinstance(e, dict)]
    out["binaries"] = [_binary_path(b) for b in doc.get("binaries") or []]
    return out


def gateway_copies(listing: Any, profile_id: str = PROFILE_ID
                   ) -> List[Dict[str, Any]]:
    """The entries of `provider list-profiles -o json` whose id is
    `profile_id`."""
    if not isinstance(listing, list) \
            or not all(isinstance(e, dict) for e in listing):
        raise ProfileError("the profile listing is not a list of objects")
    return [e for e in listing if e.get("id") == profile_id]


def same_profile(rendered: Mapping[str, Any], gateway: Mapping[str, Any]
                 ) -> bool:
    return comparable(rendered) == comparable(gateway)


def update_document(rendered: Mapping[str, Any], resource_version: Any
                    ) -> Dict[str, Any]:
    """`rendered` with the gateway copy's `resource_version`, which an update
    must carry and must be non-zero."""
    if isinstance(resource_version, bool) \
            or not isinstance(resource_version, int) or resource_version <= 0:
        raise ProfileError(f"the gateway's copy has no usable "
                           f"resource_version ({resource_version!r})")
    doc = copy.deepcopy(dict(rendered))
    doc["resource_version"] = resource_version
    return doc
