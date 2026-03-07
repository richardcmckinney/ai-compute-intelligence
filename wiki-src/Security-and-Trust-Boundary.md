# Security and Trust Boundary

## Security Model
- Customer telemetry and operational data stay in customer boundary.
- Read-only integrations by default.
- No hard dependency of application availability on platform availability.
- Kubernetes default-deny network policies enforced.
- Gateway pod egress restricted to DNS, the attribution index, and Kafka shadow-event publication.
- Neo4j is reachable only by processor pods; Kafka is reachable by the processor tier and the gateway publisher path.

## Repository Controls
- CodeQL static analysis.
- Dependency review in pull requests.
- Dependabot for dependency and workflow updates.
- Secret scanning and push protection enabled.

## Vulnerability Reporting
Security issues should be reported privately using GitHub Security Advisories.

Policy details are documented in `/SECURITY.md`.

## Security Posture
Security posture is intentionally conservative:
- explicit threat surface reduction,
- predictable CI controls,
- documented incident intake path,
- clear trust-boundary narrative for enterprise buyers.
