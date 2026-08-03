# Testing

## Purpose

IMCodex tests only the product/composition behavior it still owns. Reusable
App Server wire handling, native Channel transports, admission, projection,
request routing, and delivery coordination are verified by the pinned
`im-agent-sdk` contract and component suites.

The consumer suite constructs the SDK Gateway, Codex Application, native
Channels, and product webhook composition directly. It retains offline tests
for:

- product commands, Thread/workspace selection, settings, and Full Access
  policy;
- generic webhook authentication, bounded parsing, pre-media admission, and
  immediate replies;
- product configuration, admin, observability, restart, and launch topology;
- artifact validation, spool quota, proactive lease lifetime, and delivery
  receipts;
- SDK state migration, composition, diagnostics, and Windows smoke scripts.

The suite uses fakes at external network and native model boundaries. It does
not require channel credentials, a Codex installation, model quota, or network
access. Native transport parity belongs to the SDK suite and is not copied
back into IMCodex.

## Running

Run the complete consumer regression suite:

```bash
python -m pytest
python -m compileall -q src tests
```

Run the isolated Windows composition smoke on a Windows worker:

```powershell
pwsh -File scripts/windows-sdk-smoke.ps1
```

When adding product behavior, test it at the narrowest retained boundary. When
changing a reusable SDK-owned behavior, add the contract in `im-agent-sdk`
rather than recreating a consumer harness.
