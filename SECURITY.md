# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report privately to: **`resero-labs@proofpoint.com`**

Include what you found, how to reproduce it, and the versions of
amap-deploy-openshell, OpenShell, amap-router-local and amap-connector-claude
involved. A partial report is worth sending. You should expect an
acknowledgement that your report was received. If you do not get one, assume
it did not arrive and say so through any other channel you have.

The same address covers the router, the connector and the other AMAP
repositories (amap-spec, amap-deploy-sandy), so you do not need to work out
which repository a problem belongs to before reporting it.

## Supported versions

Fixes are made on the `main` branch. There are no maintained release
branches.

## What is in scope

This repo runs host-side, as the operator, on one OpenShell host with one
operator (`DESIGN.md` section 8). The properties it is responsible for, and
whose failure we treat as a vulnerability, are these:

- **It holds no credentials and sends no messages.** Nothing here runs inside
  a sandbox, or sends or receives a message. The provider key is needed only
  to create a member, reaches `sandbox create` and nothing else, and is never
  written or printed.
- **The inbox and peer lanes are read-only to agents, in both layers.** Each
  is a `:ro` bind mount on the agent's container and a Landlock `read_only`
  entry in its policy, so a write from inside fails with `EROFS`
  (`docs/POC-REPORT.md`, Unknown 2).
- **Each member's lanes are its own.** Every lane is mounted from that
  member's own `instances/<name>/` directory, and nothing else in the fleet
  mounts the same source.
- **The mounts interceptor refuses a wrong create.** Bound to `CreateSandbox`,
  it refuses a create whose bind sources are not the member's rendered rows,
  or whose read-only flags differ from them (`docs/INTERCEPTOR.md`). It fails
  closed: a timeout or an error is a refusal.
- **Egress only to the provider's endpoints.** An agent's network policy
  allows the model provider's endpoint and nothing else.
- **`verify` never reports a clean result it did not establish.** A check
  that could not be made is reported as UNKNOWN, never as a pass.

Out of scope by design (`DESIGN.md` section 8): protecting the host from its
own operator. Several operators, or a fleet of hosts, need the work listed in
`DESIGN.md` section 9 first.

`payload/INBOX-POLICY.md` is guidance to a cooperating agent and enforces
nothing. An agent ignoring it is not a vulnerability in this repo. An agent
being *able* to act beyond what the router allows is, and belongs in the
router's report.

## Disclosure

We will work with you on timing. The preference is coordinated disclosure
after a fix is available, and we would rather agree a date with you than
impose one.
