# AI Compute Intelligence Wiki

Welcome. This wiki is optimized for technical and business diligence.

## Quick Links
- [Platform Overview](Platform-Overview.md)
- [Technical Architecture](Technical-Architecture.md)
- [Security and Trust Boundary](Security-and-Trust-Boundary.md)
- [Operations and Deployment](Operations-and-Deployment.md)
- [Technical Due Diligence Guide](Technical-Due-Diligence-Guide.md)

## What This Platform Does
AI Compute Intelligence helps enterprises understand and optimize AI spend at decision time.

Core value:
1. Attribute spend and usage to the true organizational owner.
2. Surface confidence and uncertainty explicitly.
3. Intervene safely (fail-open) with advisory/active optimization signals.

## Operating Principles
- Optimizer, not cost cop.
- Zero request breakage guarantee through fail-open interception.
- O(1) decision-time path via precomputed attribution index.
- Security-first deployment inside customer trust boundaries.

## Operational Posture
- Authenticated `/v1/*` API surface with tenant-scoped JWT validation.
- Role-segregated gateway and processor runtime profiles.
- O(1) decision-time serving path backed by a materialized attribution index.
- CI, CodeQL, dependency review, and release workflow configured in-repo.
