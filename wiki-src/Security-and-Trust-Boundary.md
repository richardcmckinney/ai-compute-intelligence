# Security and Trust Boundary

## Security Model
- Customer telemetry and operational data stay in customer boundary.
- Read-only integrations by default.
- No hard dependency of application availability on platform availability.

## Repository Controls
- CodeQL static analysis.
- Dependency review in pull requests.
- Dependabot for dependency and workflow updates.
- Secret scanning and push protection enabled.

## Vulnerability Reporting
Security issues should be reported privately using GitHub Security Advisories.

Policy details are documented in `/.github/SECURITY.md`.

## YC Diligence Perspective
Security posture is intentionally conservative:
- explicit threat surface reduction,
- predictable CI controls,
- documented incident intake path,
- clear trust-boundary narrative for enterprise buyers.

