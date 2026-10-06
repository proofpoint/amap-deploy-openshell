# Plan

## Phase 0: answer the blocking questions

Answer Q1, Q3 and Q5 in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md). If Q1 comes back
"no host directories and no working read-only split", stop and report. Don't
work around it with a network transport (see DESIGN.md §7).

Done 2026-09-28, by reading only: see [FINDINGS.md](FINDINGS.md).

## Phase 1: a manual proof of concept on one host (no code)

Two agents, alpha and beta, in two OpenShell sandboxes, with router-local on the
host. The policy is that alpha may task beta, and beta may not task alpha. It is
the same demo as amap-deploy-sandy's runbook, which makes the two directly
comparable.

1. Create the lane directories and the roster directory by hand. Write
   `router.json` by hand, or render it with amap-deploy-sandy's policy code.
2. Configure the gateway as in DESIGN.md §6, then write one OpenShell policy
   per sandbox, as in DESIGN.md §3 and §4.
3. Start both sandboxes with Claude Code, the connector's MCP servers and
   `inbox-delivery`.
4. Start the router.
5. Alpha tasks beta; beta replies. Confirm the delegation is injected into
   beta's session, not just found in its inbox.

**Pass criteria:**
- `amap-spec/fixtures/validate.py` gates every document the connector reads and
  writes;
- the connector's operational checks pass;
- a write to `inbound/` from inside the sandbox is denied, and the host shows
  both layers: the `:ro` mount and the Landlock `read_only` entry. OpenShell
  logs no filesystem denials, so there is no log line to expect (DESIGN.md §5);
- a direct network call to a mail API from inside the sandbox is denied and
  logged;
- beta tasking alpha (never granted) is held at the router.

**Deliverable:** a short write-up of what worked, plus a table mapping each item
in `fixtures/validate.py`'s NOT-CHECKED list to one of three outcomes: enforced
by OpenShell, still operational, or not applicable.

## Phase 2: tooling

Port amap-deploy-sandy's verbs. Every verb is a dry run without `--apply`.

- `install`: writes the payload, `fleet.json` template, `router.json` and the
  roster directory.
- `provision <sandbox>`: creates the sandbox's lanes, renders its policy
  fragment, and records it in `membership.json`. The reverse is `deprovision`.
- `verify`: PASS/FAIL/UNKNOWN, including the in-sandbox write attempt from
  DESIGN.md §5.
- `list`, `router-config`, `teardown`: as in amap-deploy-sandy.

**Share rather than copy** `fleet_policy.py`, the router config rendering and
`router_health.py` with amap-deploy-sandy, if that repo's owner agrees. Two
copies of the policy model will drift.

## Phase 3: optional

- If Q2 finds lifecycle hooks, a gateway interceptor or plugin that runs
  `provision` and `deprovision` automatically.
- Kubernetes (Q7), for example router-local as a sidecar with a shared volume.
  Only if Landlock still applies to the volume.
- Router decisions sent to OpenShell's log collector (Q8).

## Phase 4: publish

Publish only after phase 1 passes. Name only OpenShell and the AMAP repos.
