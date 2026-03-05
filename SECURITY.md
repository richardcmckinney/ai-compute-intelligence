# Security Policy

This repository is security-sensitive because it handles attribution, governance, and optimization logic for enterprise AI workloads.

## Supported Versions

We support security updates for:

| Branch / Version | Status |
|---|---|
| `main` | Supported |
| Historical commits/tags | Not supported |

## Reporting a Vulnerability

Please do **not** open public issues for vulnerabilities.

Use one of these channels:
1. GitHub private vulnerability reporting (preferred):
   - Go to the Security tab and use "Report a vulnerability".
2. If private reporting is unavailable, open a draft security advisory in this repository.

Include:
- Affected file(s) and function(s)
- Impact and exploitability
- Reproduction steps or PoC
- Suggested remediation (if available)

## Response Targets

- Initial acknowledgment: within 1 business day
- Triage and severity assessment: within 3 business days
- Mitigation plan: within 5 business days for High/Critical issues

## Disclosure Process

- We follow coordinated disclosure.
- We will publish a security advisory after a fix is available and validated.
- Credits are provided unless anonymity is requested.

## Secure Development Baseline

This repository enforces:
- CI checks on pull requests
- CodeQL static analysis
- Dependency review on pull requests
- Dependabot update automation
- Secret scanning and push protection (repository settings)
