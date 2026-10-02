# Security Policy

Cairn executes actions on behalf of language models, so security reports are treated as the
highest priority work in this project.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | yes       |

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through
[GitHub security advisories](https://github.com/nagendhra-web/New-Repo/security/advisories/new).
Include:

- the affected component (policy engine, executor, sandbox, MCP bridge, API, ...),
- a reproduction, ideally an offline plan or script using `ScriptedProvider`,
- the impact: what an attacker controls and what they can cause.

You will receive an acknowledgement within 72 hours and a remediation plan within 7 days.
Fixes are released as patch versions with a credited advisory unless you prefer otherwise.

## What counts as a vulnerability

In scope (examples):

- Untrusted data reaching a sensitive tool parameter or privileged effect **without** a policy
  decision of `require_approval` or `deny` (a provenance label being lost or laundered).
- An approval being reused for different arguments than the ones approved.
- Secret values appearing in the journal, run reports, API responses, model prompts or logs.
- Escaping the filesystem sandbox or the network allowlist of the built-in tools.
- A sub-agent obtaining tool grants beyond its parent's.
- Journal tampering that `cairn verify` fails to detect.
- API authentication or rate-limit bypass.

Out of scope:

- A model producing wrong or harmful *text*: Cairn constrains what models can cause, not what
  they say. Report content issues to the model provider.
- The documented limits in [docs/security.md](docs/security.md), for example that `code.python`
  provides process-level isolation without network isolation.
- Deployments that disable the policy engine (`policy.enabled = false`) or run with
  `allow_private_network = true`.

## Design references

- Threat model and boundaries: [docs/security.md](docs/security.md)
- Provenance labels and flow policies: [docs/provenance.md](docs/provenance.md)
- Measured injection-suite results: [benchmarks/results/injection_suite.md](benchmarks/results/injection_suite.md)
