# Contributing to amap-deploy-openshell

Thanks for your interest. This repo deploys AMAP on an OpenShell host: it
provisions each agent's sandbox with its lanes, mounts, identity, MCP
configuration and delivery daemon, and renders the config for
[amap-router-local](https://github.com/proofpoint/amap-router-local). Read
`README.md` for what it does, `DESIGN.md` for why, and `CLAUDE.md` for the
conventions that are load-bearing. A change that breaks one of those
conventions will be asked to change, however small it is.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Report security issues privately, as described in [SECURITY.md](SECURITY.md),
never in a public issue.

## Where your change belongs

| you want to change | open it against |
|---|---|
| how AMAP is deployed on an OpenShell host, the mounts interceptor, `verify`'s checks | **here** |
| how messages are routed, authorised or delivered | [amap-router-local](https://github.com/proofpoint/amap-router-local) |
| the delivery daemon or the agent's MCP tools | [amap-connector-claude](https://github.com/proofpoint/amap-connector-claude) |
| the fleet policy model, or the text every agent receives | [amap-deploy-sandy](https://github.com/proofpoint/amap-deploy-sandy) |
| what the wire contract *says*: a field, a schema, an obligation | [amap-spec](https://github.com/proofpoint/amap-spec) |
| OpenShell itself | [OpenShell](https://github.com/NVIDIA/OpenShell) |

This repo deploys; it does not define. The connector and the router are used
unmodified: if one of them seems to need an OpenShell-specific change, open it
against that repo rather than patching it here.

## Setting up

The tests need four sibling sources **checked out beside this repo**:
amap-router-local, amap-connector-claude and amap-deploy-sandy at their pins
in `siblings.json`, and amap-spec.

```sh
git clone https://github.com/proofpoint/amap-deploy-openshell
cd amap-deploy-openshell
python3 amap-openshell.py siblings --apply
python3 -m pip install pytest
python3 -m pytest tests -q
```

`$AMAP_ROUTER_REPO`, `$AMAP_CONNECTOR_REPO`, `$AMAP_SANDY_REPO` and
`$AMAP_SPEC_DIR` name the checkouts if they live elsewhere. A missing source
fails the run; it does not pass as a smaller set of tests. The suite needs no
OpenShell, no Docker and no network: OpenShell is faked, and a call to
OpenShell, Docker or Podman that no test stubbed fails. The interceptor's gRPC
tests run in their own hash-locked virtualenv; `interceptor/tests/conftest.py`
gives the command.

## Making a change

1. **Open an issue first** for anything beyond a small fix, so the approach
   can be agreed before you write it.
2. **Keep behaviour and its test together.** A new check needs a test that
   breaks the thing it checks and sees *that* check go FAIL, not just
   something go red. A fixture is built from what the producer really emits,
   never from a field it cannot carry.
3. **Mark assumptions about OpenShell.** `DESIGN.md` tags each claim with
   where it was read or whether it was verified. Never upgrade a tag without
   evidence.
4. **Run the suite** on your own machine, with the siblings checked out.
   CI runs the same suite, on Python 3.9 and 3.13, against the pins in
   `siblings.json`.
5. **Keep shipped text identifier-clean**: no absolute host paths, no
   personal names, no real addresses or domains. Use `$AMAP_OPENSHELL_HOME`
   and `<name>` placeholders and `example.org` domains.
6. **No credentials, ever.** Nothing in this repo holds or acquires one.

### Changes that reach other repos

- The router's config (`router.json`) is rendered in the router's exact
  vocabulary, and the router refuses a key it does not know. A new key needs
  the router's change first.
- `payload/amap-main` exports the variables the connector's delivery daemon
  requires. Changing them means changing the daemon's contract in
  amap-connector-claude.
- `payload/INBOX-POLICY.md` and `payload/mcp-servers.json` are
  amap-deploy-sandy's, with named differences that the tests check. A change
  to their text belongs in amap-deploy-sandy.
- A capability the wire contract lacks goes to amap-spec as a proposal first.

## Pull requests

- Keep each PR to one concern, with a description that says what changed and
  why.
- Say which versions of OpenShell, amap-router-local, amap-connector-claude
  and amap-deploy-sandy you tested against, and whether you ran anything on a
  live host.
- Expect review comments on tests as much as on code. A check that cannot
  fail, or cannot pass, is a defect.

## License

This project is licensed under the [Apache License 2.0](LICENSE). Unless you
state otherwise, any contribution you submit is licensed under the same terms,
as described in section 5 of the license.
