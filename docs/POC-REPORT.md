# POC-REPORT: amap-deploy-openshell, L1

Drafted by `amap-openshell.py l1-run` from the run's evidence directory, then reviewed and completed by hand: Pass criterion 1's coverage question, the NOT-CHECKED mapping and the DESIGN.md tags are filled in below, each from the evidence file named beside it.

Date: 2026-09-30
Accepted by the operator: 2026-09-30
Host: Linux 6.8.0-142-generic, Docker 29.8.1
OpenShell: openshell 0.1.2
Claude Code: 2.1.284
Fleet: examples/fleet.json: alpha may task beta; beta may not task alpha

Accepted gateway posture (D6, Decision 1): a dedicated gateway, loopback only, no OIDC, one operator, no other sandboxes. The risk accepted: any caller that may create a sandbox on it can bind-mount any host path the container runtime can see. Checked by l1-run as Unknown 6: PASS.

Every outcome below is PASS, FAIL or UNKNOWN. UNKNOWN is not a pass.

Commit references to this repository point to its pre-publication history, which is kept privately. References to the sibling repositories' commits are unaffected.

Evidence files cited below (`step-N/...`, `evidence/...`) are the runner's
records on the test host. They are not part of this repository.

## What worked

alpha delegated a task to beta through the connector's inbox-submit CLI, the drop-box the agent's tool writes. The router placed a notice in beta's peer lane, and beta's daemon reported the outcome delivered. beta's reply reached alpha's peer lane. The reply carried real work, not an acknowledgement:

> Workspace /sandbox (beta@agents.example.org) contains no project files, only tool/config entries: `.cache/` (directory), `.claude/` (directory, Claude Code state), `.claude.json` (file, 1349 bytes). It isn't a git repo, and nothing else is there.

Every pass criterion and every unknown is PASS. The runner left Pass criterion 1 UNKNOWN because three document classes had no live document; that question is answered under it, and the criterion is PASS.

## Pass criteria

### Pass criterion 1: validate.py gates every document
Outcome: PASS
Evidence: 15 live documents of 5 classes reached `check_document` (schema plus the post-checks), and every one validated with no errors: roster.schema.json, peer-notice.schema.json, inbound-message.schema.json, submit-request.schema.json, result.schema.json; evidence file: step-8/20260930T124202Z-061-documents.json

Coverage, as amap-spec's validator requires ("name the document classes you emit, and show that one of each reached the validator"). The runner reported UNKNOWN because three classes produced no document. Each is accounted for:

| Class | Status | Why |
|---|---|---|
| `roster`, `peer-notice`, `inbound-message`, `submit-request`, `result` | emitted and gated | 15 documents, 0 errors |
| `directory` | not emitted by this deployment | amap-router-local publishes `roster.json` and deliberately not `directory.json`, which it calls "a different artifact" carrying a projection of the graph (`router/roster.py:14,28`). Nothing else here writes one |
| `binding-record` (`identity.json`) | not emitted by this deployment | No component writes it: not this repo, not amap-deploy-sandy, not the router. The schema calls it informational and not authoritative, and requires a consumer to tolerate its absence (`schemas/binding-record.schema.json`) |
| `deliver-notice` | not exercised by this fleet | The mail lane carries it. `examples/fleet.json` sets `peers: {}` and `default_peers: []`, so no mail pair exists and no mail can flow. The lane is mounted and the connector registers its server, so a mail-carrying fleet would emit this class. Out of L1's scope, and the one class a later run should cover |

### Pass criterion 2: the connector's operational checks
Outcome: PASS
Evidence: beta's daemon reported delivered; beta's reply reached alpha; alpha's submit_result says accepted; one claude session is visible in alpha; one claude session is visible in beta; beta's outcome for the delegation: delivered; beta replied, and the notice is in alpha's peer lane; alpha's submit_result outcome: accepted; the lister shows one claude session in alpha; the lister shows one claude session in beta; evidence files: step-8/20260930T124202Z-023-audit-outcome.json, step-8/20260930T124202Z-024-audit-reply.json, step-8/20260930T124202Z-025-submit-result-alpha.json, step-8/20260930T124202Z-026-ready-alpha.json, step-8/20260930T124202Z-027-ready-beta.json

### Pass criterion 3: a write to the inbox lane is denied at both layers
Outcome: PASS
Evidence: the write into the inbox lane was denied; the mount flags match the mount table; the policy lists the inbox lane under read_only and the outbox lane under read_write; the write into the inbox lane failed: touch: cannot touch '/opt/amap/lanes/inbox/probe': Read-only file system; the container mounts the inbox lane read-only and the outbox lane read-write; Landlock layer: the inbox lane is under read_only, the outbox lane under read_write; evidence files: step-8/20260930T124202Z-006-touch-inbox.json, step-8/20260930T124202Z-007-docker-ps-sandbox.json, step-8/20260930T124202Z-008-inspect-mounts-1.json, step-8/20260930T124202Z-009-inspect-mounts-2.json, step-8/20260930T124202Z-010-policy-only.json

### Pass criterion 4: a network call to a mail API is denied and logged
Outcome: PASS
Evidence: the call was denied and the denial is logged; the call failed: DENIED URLError; the sandbox log names the host: [1790772211.766] [sandbox] [OCSF ] [ocsf] NET:REFUSE [MED] DENIED mail.example.org [reason:policy_dns_ineligible]; evidence files: step-8/20260930T124202Z-012-network-probe.json, step-8/20260930T124202Z-013-sandbox-logs.json, step-8/20260930T124202Z-014-sandbox-logs.json

### Pass criterion 5: beta tasking alpha is held at the router
Outcome: PASS
Evidence: the router holds a copy of the request; submit_result says queued_for_human; no notice was placed in alpha's peer lane; the router holds the request under its private state directory; submit_result: queued_for_human, reason recipient_not_allowlisted; the router's audit log for alpha has no placement for the request; evidence files: step-8/20260930T124202Z-028-submit-reverse.json, step-8/20260930T124202Z-029-held-copy.json, step-8/20260930T124202Z-030-submit-result-beta.json, step-8/20260930T124202Z-031-audit-placement.json

## Unknowns

### Unknown 1: router writes appear inside a running sandbox
Outcome: PASS
Evidence: the notice the router wrote is listed inside the running beta; the notice is listed on the host and inside beta; evidence files: step-8/20260930T124202Z-015-host-listing.json, step-8/20260930T124202Z-016-ls-peer-notices.json

### Unknown 2: the errno of a write to the inbox lane
Outcome: PASS
Evidence: the write failed with EROFS; errno: EROFS; evidence files: step-8/20260930T124202Z-011-write-probe-inbox.json

### Unknown 3: a lane mounted but missing from the policy
Outcome: PASS
Evidence: the unlisted lane is unreadable and the listed lane is readable; the listing of the unlisted lane failed: ls: cannot open directory '/opt/amap/lanes/peer': Permission denied; the listing of the listed lane worked; evidence files: step-8/20260930T124202Z-032-get-amap-l1-probe.json, step-8/20260930T124202Z-033-confirm-absent-amap-l1-probe.json, step-8/20260930T124202Z-034-create-amap-l1-probe.json, step-8/20260930T124202Z-035-get-amap-l1-probe.json, step-8/20260930T124202Z-036-ls-probe-peer.json, step-8/20260930T124202Z-037-ls-probe-inbox.json, step-8/20260930T124202Z-038-get-amap-l1-probe.json, step-8/20260930T124202Z-039-delete-amap-l1-probe.json

### Unknown 4: injection over AF_UNIX, and the refusing receiver
Outcome: PASS
Evidence: the delegation was delivered and the socket directory is under a read-write path; with the receiver set to refuse the outcome is held; positive case outcome: delivered; the socket directory is under /tmp; negative case outcome: held; evidence files: step-8/20260930T124202Z-040-audit-outcome.json, step-8/20260930T124202Z-041-ready-beta.json, step-8/20260930T124202Z-042-refuse-state.json, step-8/20260930T124202Z-043-write-refuse.json, step-8/20260930T124202Z-044-stop-beta.json, step-8/20260930T124202Z-045-phase-beta.json, step-8/20260930T124202Z-046-start-beta.json, step-8/20260930T124202Z-047-ready-beta.json, step-8/20260930T124202Z-048-ready-beta.json, step-8/20260930T124202Z-049-submit-negative.json, step-8/20260930T124202Z-050-audit-negative-placed.json, step-8/20260930T124202Z-051-audit-negative-outcome.json, step-8/20260930T124202Z-052-remove-refuse.json, step-8/20260930T124202Z-053-stop-beta.json, step-8/20260930T124202Z-054-phase-beta.json, step-8/20260930T124202Z-055-start-beta.json, step-8/20260930T124202Z-056-ready-beta.json, step-8/20260930T124202Z-057-ready-beta.json

**What `held` means here (settled 2026-10-02).** The negative case was a real refusal by the receiver. The `refuse` in beta's `.claude/settings.local.json` applied, as Claude Code's cross-session-messaging docs say a project or local `refuse` does. It was not a hold for human approval. The daemon reports a receiver's refusal as `held` by design: in its vocabulary `refused` means its own gate rejected the notice, while a receiver configured to refuse is something the operator can fix, so the notice is offered again later (amap-connector-claude `bin/inbox-delivery`, the `receiver_refused` branch, pinned by `test_receiver_refusal_is_a_held_with_an_operator_detail_and_no_reinjection`). The outcome's own detail confirms it: "receiver refuses cross-session messages — crossSessionInbound is not `accept` in this sandbox; fix the setting and the daemon re-offers", not "held for the recipient user's approval" (`step-8/20260930T124202Z-051-audit-negative-outcome.json`, read from the stopped L1 host's disk, mounted read-only).

### Unknown 5: where the cross-session receiver setting is fixed
Outcome: PASS
Evidence: the payload's settings file is not writable from inside; step 7 was delivered under accept; tightening through settings.local.json made the receiver refuse (reported by the daemon as `held`; see Unknown 4); a write to the payload's settings file fails: EROFS; listing of the agent's settings directory: total 48; the agent's user creating a file in /sandbox/.claude: writable; whether /sandbox/.claude/settings.json changes the value: UNKNOWN (not exercised by the runner); evidence files: step-8/20260930T124202Z-058-write-probe-payload.json, step-8/20260930T124202Z-059-ls-claude-dir.json, step-8/20260930T124202Z-060-home-write-probe.json

### Unknown 6: the accepted gateway posture
Outcome: PASS
Evidence: the gateway file, the listener and the sandbox list all match D6; ok: the gateway configuration file exists; ok: gateway.toml has every setting of the gateway fragment (D6, DESIGN.md section 6); ok: gateway.toml has no bind_address: the gateway keeps its built-in loopback listener (D6); ok: gateway.toml has no OIDC setting (D6); ok: openshell status reports a gateway; ok: openshell-gateway config preflight accepts the gateway configuration file; ok: the gateway listens on 127.0.0.1:17670 and nowhere else (D6, loopback only); ok: no sandbox exists but the fleet's (D6); evidence files: step-8/20260930T124202Z-002-status.json, step-8/20260930T124202Z-003-gateway-preflight.json, step-8/20260930T124202Z-004-listeners.json, step-8/20260930T124202Z-005-sandbox-list.json

### Unknown 7: the shared uid holds end to end
Outcome: PASS
Evidence: the host owner of alpha's outbox is 1000:1000; the host owner of beta's peer notices is 1000:1000; alpha's policy run_as is 1000:1000; beta's policy run_as is 1000:1000; the router container's user is 1000:1000; the uid:gid inside alpha is 1000:1000; the uid:gid inside beta is 1000:1000; the notice is 1000:1000 with mode 600; beta reads the message the router wrote; the operator's uid:gid: 1000:1000; the host owner of alpha's outbox: 1000:1000; the host owner of beta's peer notices: 1000:1000; alpha's policy run_as: 1000:1000; beta's policy run_as: 1000:1000; the router container's user: 1000:1000; the uid:gid inside alpha: 1000:1000; the uid:gid inside beta: 1000:1000; the notice inside beta: 1000:1000 600; beta read the delivered message; evidence files: step-8/20260930T124202Z-017-host-ids.json, step-8/20260930T124202Z-018-inspect-router-user.json, step-8/20260930T124202Z-019-ids-alpha.json, step-8/20260930T124202Z-020-ids-beta.json, step-8/20260930T124202Z-021-stat-notice.json, step-8/20260930T124202Z-022-cat-message.json

## NOT-CHECKED mapping

One row for each item in the NOT CHECKED HERE list of the amap-spec validator,
filled in during L1. Mark one column with X.

| NOT-CHECKED item | enforced by OpenShell | still operational | not applicable | evidence |
|---|---|---|---|---|
| sidecar file existence: extra file, missing file, or dir present when the descriptor array is empty/absent. |  | X |  | no attachment was sent in L1; the obligation stays with the connector and the router |
| `size_bytes` / `sha256` actually matching the on-disk bytes. |  | X |  | as above |
| `filename` display-sanitization and the "never a path" invariant. |  | X |  | as above |
| ordinal <-> directory-entry binding on disk (the schema only checks the *descriptor* array's own index field, not that dir entry `N` exists). |  | X |  | as above |
| inbound sidecar dirs keyed on the runtime-minted notice-id (not the provider message.id). |  | X |  | as above; the mail lane carried nothing |
| runtime-private staging location (outside the mounted volume). | X |  |  | the router's `state_dir` is not a mount in any sandbox, and an unmounted or unlisted path is unreachable from inside: Unknown 3, and the held request under `router-state` in Pass criterion 5 |
| write-ordering / commit-sentinel atomicity (sidecars before req-<id>.json; `.tmp` + os.replace). |  | X |  | no attachment was sent in L1 |
| caps / allowlists (type allowlist, size caps, volume quotas). |  | X |  | as above |
| no `inbound/messages/notice-<id>.json` exists for a quarantined/blocked message (enforcement-by-absence, v2.1.0 §5) — a filesystem invariant no document validator can see. |  | X |  | the mail lane carried nothing in this fleet |
| connector tolerance of a read-only `inbound/` tree (v2.2.0 §2) — a behavioral invariant no document validator can see. Prescribed check is operational, not a fixture: run the connector against an inbound tree it cannot write and confirm it starts, relays every pending notice, resolves bodies, and submits outbound, without requiring any inbound write to succeed (contract.md §7). |  | X |  | demonstrated on the peer lane, which is the read-only tree this fleet uses: the lane is `:ro` and Landlock `read_only`, a write to a read-only lane gives EROFS, and the daemon still started, relayed the notice, resolved the body and submitted outbound. Pass criteria 2 and 3, Unknowns 1 and 2. The mail lane was not exercised |
| ingest-by-copy (v3.0.0, contract.md §2): validation, policy evaluation, and message composition running over a runtime-private snapshot taken before any of those steps, rather than re-reading an agent-writable path — a filesystem/timing invariant no document validator can see. |  | X |  | the router's, and not exercised in L1 |
| write-side path discipline (v3.0.0, contract.md §2), the three conjunctive requirements: (a) full parent-chain resolution through pinned directory descriptors from the namespace root, (b) each directory component opened `O_NOFOLLOW\ |  | X |  | as above |
| read-side open discipline (v3.0.0, contract.md §2): per-component no-symlink-follow opens, non-blocking opens, fstat-based regular-file verification on the open descriptor, and bounded reads/caps on every agent-influenced path — a filesystem invariant no document validator can see. |  | X |  | as above |
| single writer, single drainer per namespace (v3.0.0, contract.md §2) — an operational/deployment invariant (e.g. an OS-level lock held for the process lifetime) no document validator can see. |  | X |  | partly evidenced: one router container, one delivery daemon per sandbox holding its consumer claims, and the lister shows exactly one session in each sandbox (Pass criterion 2) |
| compose-time header-boundary stripping (v3.0.0, contract.md §3): even where the schema's control-character patterns below are somehow bypassed upstream, the runtime MUST still reject or strip CR/LF/NUL before composing the outbound message — a runtime-side belt-and-braces obligation, not something this document-level gate re-checks. |  | X |  | the runtime's, and not exercised in L1 |
| peer-origin profile (v3.1.0 DRAFT, spec/peer-origin.md) — all operational, none visible to a document validator: | | | | the six rows below are its parts |
| write-authority partition: no component that admits messages from a transport not restricted to authorised peer senders holds write authority over `peer/`; prescribed check is a write attempt (`touch`) from inside the admitting component against `peer/`, which MUST fail. | X |  |  | both layers hold for `peer/`: the container mounts it read-only (`RW=false`) and the effective policy lists it under `read_only` with `landlock.compatibility: hard_requirement`. The prescribed write attempt was made against the inbox lane, which carries the identical pair, and failed with EROFS. Pass criterion 3, Unknown 2, and the mount and policy evidence files |
| intake isolation: shared-transport peer candidates are held in a location that is neither `inbound/` nor `peer/`, unreachable from any agent namespace, until the verifier places them exactly once. |  |  | X | no shared transport: this is a single host, and the router takes candidates only from each agent's own outbox. The analogous local property holds, in that a request the router refuses is held under its private state directory, unreachable from any sandbox (Pass criterion 5) |
| verification placement: signature verification runs in a component holding peer write authority, hashing body/attachment bytes itself. |  |  | X | no cross-host signed peer message: the router asserts `sender_exposure` itself and the audit record's `signature_verdict` is null |
| `sender_exposure` not sender-settable: a submit-request carrying it is `rejected` (closed envelope); cross-host it is taken only from the verified statement and stripped on downgrade. |  | X |  | the router set `sender_exposure` itself (`asserted_by: amap.router@agents.example.org` in the audit record), and every submit-request document validated against its closed schema |
| `in_reply_to` iff resolved: a peer notice carries `in_reply_to` only when the placing runtime resolved it from its own ledger; a reply key it cannot resolve is `rejected` (`unresolved_reply`), never stripped and re-routed as a fresh task. |  | X |  | demonstrated: the reply carries `in_reply_to` set to the delegation's notice id, resolved from the router's own ledger (`reply_basis: reply_window`). Evidence: step-7/…-005-audit-reply.json |
| at-most-once action: a connector never causes one peer message to be acted on more than once; an ambiguous target is held, not fanned out. |  | X |  | partly evidenced: exactly one session in each sandbox, so no ambiguous target arose. The at-most-once ledger is the daemon's and was not exercised beyond this run's single delivery |

**Reading this table.** "Still operational" means the obligation stays with the connector, the router or the runtime: OpenShell does not enforce it, and L1 did not settle it unless the evidence column says so. No row is marked "not applicable" because it was merely unexercised.

## DESIGN.md tags

Moved to `[verified]` on the strength of this run, each citing the line above that holds its evidence:

| DESIGN.md claim | Now | Evidence here |
|---|---|---|
| P1: the host's writes appear inside a running sandbox, with no delay | `[verified]` | Unknown 1 |
| P3: a write to a read-only lane gives `EROFS`, not `EACCES` | `[verified]` | Unknown 2, Pass criterion 3 |
| P4: end-to-end injection into a live session, and a refusing receiver yields `held` | `[verified]` | Pass criterion 2, Unknown 4 |
| Section 3: the operator's `crossSessionInbound` cannot be undone from inside | `[verified]` | Unknown 5 |
| Section 4: the image paths, the profile import and the single endpoint | `[verified]` | step 3, Pass criterion 4 |
| Section 5: the write attempt fails, and both layers are in place | `[verified]` | Unknown 2, Pass criterion 3 |

Not moved: whether a `settings.json` of the agent's own changes the receiver value (not exercised, noted under Unknown 5), and the mail lane, which this fleet does not carry.

## After L1: the first live run of `deprovision` and `teardown` (2026-10-01)

Before the L1 host was destroyed, the operator ran the two verbs that take a fleet down, as `amap`, from `main`, on L1's home. Neither had run outside the test fakes before. The router container was stopped between the two deprovisions and `teardown`, because `teardown` removes the config and state the router polls.

| Verb | Outcome | What the run showed |
|---|---|---|
| `deprovision alpha`, `deprovision beta` | PASS | Each dry run printed exactly what `--apply` then did. Each found its sandbox's recorded ID and deleted only that sandbox. Each emptied its lanes' contents and kept the lane directories. Each cleared its `router-state/<name>/`, keeping `audit/`, and rewrote `membership.json` and `selected.json`. `sandbox list` was empty afterwards |
| `teardown` | PASS | Before removing anything it reported 2 audit files, and the file counts of `payload/` (11), `roster/` (1) and `router-state/` (4). It removed those, plus `router.json` and `selected.json`. It kept `fleet.json`, `membership.json`, `instances/`, `evidence/`, `policies/`, `providers/`, `commands/` and `gateway-fragment.toml` |

One finding. `deprovision beta` removed `router-state/beta/held` as an ordinary removal line. That held the request L1 left awaiting a human (Pass criterion 5). `teardown` warns explicitly that a held request "will be lost"; `deprovision` does not, so the two verbs disagree about whether a pending human-approval item deserves a warning. Nothing was lost that the operator meant to keep. The inconsistency is a follow-up for the verbs.

After the teardown, the L1 close-out was completed: the L1 host's credentials were removed.

## L2: a full live run with the tooling (2026-10-04)

Accepted by the operator: 2026-10-04

L2 ran on a rebuilt host, not on L1's. Bring-up was by `bring-up` (D15).
Step 10 was done by hand. So were the step 11 experiments. Then
`l1-run --only 7` and `--only 8` re-checked the pass criteria against the
verb-provisioned fleet. Outcomes are PASS, FAIL or UNKNOWN, as above.

| | |
|---|---|
| This repository | `main` at `14ee8d4` |
| Siblings (`siblings.json`, D13) | amap-router-local `9854a5e`, amap-connector-claude `37875a5`, amap-deploy-sandy `0ec0ce3` |
| The spec | unpinned; the checkout was at `2ffdfeb` |
| Claude Code | 2.1.286. D13 named 2.1.284. Step 10 showed that D9's pre-seeded first-run answers still hold: both sessions started unattended |
| Host, Docker, OpenShell | Linux 6.8.0-146-generic, Docker 29.8.2, openshell 0.1.2 (`evidence/step-0/facts.json`). L1 was 6.8.0-142 and 29.8.1 |

### Bring-up

The rebuilt host was brought up by the operator's unattended bootstrap, which calls
`bring-up --apply`. The first two attempts stopped, and each stop named its
stage:

1. **At router: "the router published no roster within 180 s".** The cause
   was on the host. A default ACL on the service account's home had made the
   sibling checkouts 0750. The router image built from them had an entrypoint
   its uid could not read, so the container crash-looped with exit 126. The
   operator fixed the ACL and removed the container and image. Not a defect
   here.
2. **At profile: "the provider profile does not lint".** This was a defect
   in `import_profile`, which `l1-run` step 3 shares. It linted before it
   listed, and OpenShell 0.1.2's lint rejects a profile whose ID the gateway
   already has ("custom provider profile 'amap-claude-code' already
   exists"). So the stage could not be rerun after a later stage had
   stopped. Fixed in `14ee8d4`: it lints only before an import, and it
   reports every line of the diagnostic. The fake `openshell` now behaves
   the same way, and a test replays this sequence.

The third run ended `AMAP is up`, and every `verify` check was PASS. It was
also the idempotence check: stages 1-6 all skipped, with no build, no
import, no create and no start, and no key was needed. `verify` was rerun
from a fresh shell: exit 0.

### Step 10: the agent chooses to delegate

The operator gave alpha the tutorial's prompt word for word. The router's
audit log and both sessions agree:

| UTC | Event |
|---|---|
| 17:52:55 | `peer_notice_placed` in beta, from alpha, `message_id c860d742…` |
| 17:53:05 | `peer_notice_placed` in alpha, from beta, `in_reply_to c860d742…`, `reply_basis: reply_window` |
| 17:53:26 | `outcome_consumed` `delivered` for `c860d742…` in beta |
| 17:53:36 | `outcome_consumed` `delivered` for the reply `43ae0c27…` in alpha |

- **Outcome: PASS.** The request arrived in beta's session as a turn, not
  by polling. Beta listed its workspace and replied with `submit`. The reply
  arrived in alpha's session as a turn.
- **The peer-key path S8b introduced worked live.** No `inject_failed`
  occurred.
- **Both agents sent names only, and said so.**

One finding, for amap-router-local and amap-connector-claude, not this
repository. The router logged the correct reply path at WARNING: "supplied
draft.to/cc — discarded outright, bound to 'alpha' instead per the private
ledger". The connector's guidance tells an agent to name the sender in `to`.
The router then warns every time an agent does so on a reply.

### Step 11: the experiments

| Experiment | Outcome | What the run showed |
|---|---|---|
| The agent cannot write its own lanes | PASS | `Read-only file system` and `Errno 30` |
| Both layers, not one | PASS | Policy: `inbox` and `peer` `read_only`, only `outbox` `read_write`. Mounts: `inbox`, `peer`, `payload` and `roster` RW false, `outbox` RW true, each from the member's own directory. `landlock: hard_requirement`, run as 1001:1001, egress only to `api.anthropic.com:443` for the image's real `node` and `claude.exe` |
| A delegation edge is directed | PASS | Beta's new task for alpha: `queued_for_human`, `recipient_not_allowlisted`, no message id. The router drained it as `queued_for_human: 1`. Alpha's peer lane held only step 10's reply. Beta explained the reply-versus-new-task rule unprompted |
| No way out but the provider | PASS | `Errno 13` at `connect()`. Logged as `NET:REFUSE DENIED mail.example.org [reason:policy_dns_ineligible]` and `NET:OPEN DENIED /usr/bin/python3.11 -> mail.example.org:443 [reason:transparent_tcp_policy_denied]`. OpenShell also staged the name "for TCP policy review", and it was left unapproved |
| A generated file is not yours to edit | PASS | `router-config` reported drift and exited 1. `verify` failed only "router.json is what this fleet renders" and exited 1. `router-config --apply` restored it, and the next run printed "in sync" with exit 0 |
| A stopped router is not a healthy one (was "Could not tell" is not "fine") | PASS, prediction corrected | Immediately after `docker stop`, only "the router container is running" failed, and `verify` exited 1. The health checks read `status.json` and stayed PASS within 3 × the announced interval (15 s). After 20 s, "the last poll is recent" also failed (22.09 s against 15). No check was UNKNOWN, because the evidence was stale, not missing. `docker start` restored it, and `verify` exited 0 |

The tutorial's predictions for the last experiment were wrong on two counts.
It predicted UNKNOWN for the health checks. It also said to restart with step
8's `run.sh --detach`, which fails on the stopped container's name.
`verify`'s own remedy text, copied from sandy's router sections, has the same
`run.sh` advice.

### `l1-run --only 7` and `--only 8`

`--only 8` alone left Pass criterion 2 and Unknowns 1, 4, 5 and 7 UNKNOWN,
because step 7 had recorded no delegation. Step 10 was by hand, so no record
existed. The plan's claim that `--only 8` re-checks everything "at no extra
cost" was wrong. That first run's results are kept as
`evidence/step-8/checks.before-step-7.json`. Then `--only 7` planted a
delegation by the CLI. Beta's agent answered it by itself. A rerun of
`--only 8` then gave:

| Check | Outcome |
|---|---|
| Pass criterion 1 | UNKNOWN from the runner: the same three classes as L1 had no live document. The L1 table above accounts for them unchanged |
| Pass criteria 2-5 | PASS |
| Unknowns 1-7 | PASS. Unknown 4's refusing receiver again gave `held`. Unknown 7 found 1001:1001 on every host file, policy, container and process it checks |

### Startup observations (not faults)

- Alpha's log at startup showed `claude.exe -> downloads.claude.ai:443`
  DENIED. That is Claude Code's download check, blocked by the policy as
  intended.
- It also showed three `api.anthropic.com:443` connections closed: "policy
  generation is stale [captured_generation:1 current_generation:2]". The
  sandbox's policy changed once while it was starting, and Claude
  reconnected. Step 10 ran normally afterwards.

### Left on the host

- Beta's reverse-task requests (`00000001` from the experiment, and
  `00000002` from `l1-run`) are held at the router. So are the refusing
  receiver's negative-case notices.
- `deprovision beta` would delete them without a warning. That is the
  follow-up recorded after L1.

## L2 rerun with the mounts interceptor on (2026-10-05): L3's precondition

This run is on a VM built fresh by the operator's unattended bootstrap, with
`--recreate`, on main as of the run. Outcomes are PASS, FAIL or UNKNOWN, as
above.

| | |
|---|---|
| New since L2 | the mounts interceptor (S10, S10b, D24); router `e43dbba` (D16); sandy `eeecad0` (S10c, D23); the derived fleet domain (D21, S11); the home record (D25) |
| Connector | `37875a5`, unchanged |
| OpenShell | 0.1.2, unchanged |

### What the run found, and fixed

Each stop named its cause, and each fix landed on main with a test that the
old code fails.

1. **The gateway would not start with the interceptor registered.** The
   packaged gateway signs its calls to extensions by default (it logged
   `gateway-minted sandbox JWT enabled`). Our interceptor refuses a signed
   `Describe`, by design, so under `fail_closed` the gateway exited.
   - **Fix (D24):** the registration sets `allow_insecure_transport = true`,
     OpenShell's per-registration opt-out. The gateway logs a warning about
     it at every start.
   - The refusal stays, so removing the opt-out fails loudly again.
2. **V3 classified the interceptor's refusal as the gateway's.** The CLI
   wraps an error message, breaking at spaces and after hyphens, with a `│`
   before each continuation line. Off a terminal the wrap is narrower, so the
   prefix and the path were split.
   - **Fix:** V3 matches with whitespace and box characters removed.
3. **`l1-run` step 7 sent to `beta@agents.example.org`.** It built addresses
   from the template, not the installed `fleet.json`, which since D21 carries
   the per-host domain. The router answered `recipient_unknown`.
   - **Fix:** it reads the installed `fleet.json` once one exists.
4. **`l1-run` step 7 then resumed that rejected request**, found by its
   subject line, and waited for a notice that could not come.
   - **Fix:** it resumes only a request addressed to a current member's
     address that the router has not rejected.

### Bring-up and the interceptor

The final recreate ended `AMAP is up`. Run from a fresh shell with no export,
`verify` gave 39 PASS and exit 0, so the home record works (D25). The
interceptor, live:

| Check | Outcome | Evidence |
|---|---|---|
| `config preflight` accepts the interceptor block | PASS | `provision-guest.sh` step 8 |
| Members provision through it | PASS | the interceptor's log: `evaluate allow`, twice, one per member |
| A create mounting another member's outbox is refused, by the interceptor | PASS | V3 PASS. The gateway's log: `decision="deny"` for `amap-openshell-mounts` |
| The refusal gives the right reasons | PASS | the unknown driver key; a name outside the fleet; a source that is not that name's own |
| Latency | PASS | `latency_ms=1` per evaluation, against a 2 s timeout |
| The gateway keeps working after the interceptor restarts | PASS | after `systemctl --user restart amap-openshell-interceptor`, V3's probes were evaluated `deny` and `verify` exited 0 |
| Addresses use the derived domain | PASS | the roster holds `alpha@openshell.<host>.internal` and `beta@openshell.<host>.internal` |

### Step 10, on the new stack

PASS, in both directions. The agents saw it, and so did the router.
- Alpha found `beta@openshell.<host>.internal` in the roster and submitted, and
  the router accepted it as `peer_routed`.
- The request arrived in beta's session as a turn. Beta replied, and the
  reply arrived in alpha's session as a turn.
- Both agents sent file names only.
- On router `e43dbba`, the correctly addressed reply logged no WARNING. L2's
  finding is fixed upstream.

### `l1-run --only 7` and `--only 8`

Step 7: PASS. Step 8:

| Check | Outcome |
|---|---|
| Pass criterion 1 | UNKNOWN from the runner, for L1's three classes. The L1 table accounts for them unchanged |
| Pass criteria 2-5 | PASS |
| Unknowns 1, 2, 4, 5, 6, 7 | PASS. Unknown 7 found 1001:1001 throughout |
| Unknown 3 | UNKNOWN: the interceptor refused the probe, as it should |

Unknown 3's probe is a sandbox named `amap-l1-probe` that mounts alpha's
lanes. It tests whether a lane that is mounted but missing from the policy can
be read. With the interceptor on, that create is refused before the sandbox
exists, because the name is not a fleet member and the sources are another
member's. That refusal is the interceptor doing its job. The property Unknown
3 tests is Landlock's, and L1 and L2 settled it PASS with the interceptor off.
It stays UNKNOWN for this run, never a pass. `l1-run` now says why.

### Open after this run

- **The wire spelling of field names** (camelCase or snake_case) is still
  unverified. The rule reads both, so it does not matter for correctness.
- **Step 8 of `provision-guest.sh` cannot yet replace its own interceptor
  block** when the fragment changes (IMPLEMENTATION-PLAN.md, S10b
  follow-up).
