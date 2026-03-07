# PRD and Engineering Spec Alignment Audit

Audit date: 2026-03-06  
Scope: `PRD_AI_Compute_Intelligence_Platform.pdf` and `Engineering_Specification_AI_Compute_Intelligence.pdf` versus current repository implementation.

## Executive Summary

- The repository is now strongly aligned with MVP/P0 requirements across ingestion, attribution, interception, policy, interventions, and demo surfaces.
- This audit cycle closed several concrete implementation gaps that were still feasible without external cloud control planes.
- Remaining items are primarily infrastructure- or enterprise-ops dependent (for example service mesh failover, production IAM/secret provisioning, formal compliance programs), not local code blockers.

## Gaps Closed in This Audit Cycle

1. P0 ingestion coverage expanded (FR-101a, FR-102, FR-103a)
   - Added Okta identity connector (System Log + SCIM transform path).
   - Added GitHub SCM webhook connector (push and PR merge transforms).
   - Added GitHub Actions connector (workflow job to deployment event transform).
   - Paths:
     - `src/aci/ingestion/connectors/okta.py`
     - `src/aci/ingestion/connectors/github.py`
     - `src/aci/ingestion/connectors/__init__.py`

2. FR-607 drift monitoring behavior hardened
   - `GET /v1/finops/drift` now returns threshold-window context (`daily`/`weekly`), investigation flags, and dashboard annotation (`reconciled` vs `unreconciled`).
   - Path: `src/aci/api/app.py`

3. Engineering Spec index durability/versioning alignment (Section 6.4/6.5)
   - Added Redis hash durable model with monotonic version metadata and schema marker.
   - Added atomic Lua upsert path and TTL refresh behavior.
   - Added rebuild clear to remove stale durable keys by prefix.
   - Path: `src/aci/index/materializer.py`

4. Response header contract alignment (Engineering Spec 9.2, FR-110/304)
   - Added deterministic `X-ACI-Price-Snapshot-Id` emission path.
   - Added pricing catalog snapshot identifier generation.
   - Paths:
     - `src/aci/pricing/catalog.py`
     - `src/aci/api/app.py`
     - `src/aci/interceptor/gateway.py`

5. Security/correctness hardening from prior review inputs
   - DP budget/noise hardening, AWS connector sanitization, org-change edge closure, circuit-breaker degraded-state resilience, frontend JS-handler escaping, and k8s secret/role placeholders.
   - Paths include:
     - `src/aci/benchmark/federated.py`
     - `src/aci/ingestion/connectors/aws.py`
     - `src/aci/core/processor.py`
     - `src/aci/graph/store.py`
     - `src/aci/interceptor/circuit_breaker.py`
     - `frontend/index.html`
     - `k8s/base/deployment.yaml`

6. Onboarding automation scope expanded (FR-112)
   - Added read-only onboarding guides for Okta token scoping and GitHub App installation/permissions, alongside existing AWS IaC assets.
   - Paths:
     - `infra/onboarding/okta/README.md`
     - `infra/onboarding/github/README.md`
     - `infra/onboarding/README.md`

## Validation Evidence

- Full test suite: `165 passed`
- Ruff: `All checks passed`
- Mypy: `Success: no issues found in 77 source files`

## Coverage Verdict (PRD + Engineering)

Implemented in-repo (code and/or executable demo behavior):

- Ingestion and schema foundation: FR-100a, FR-101a, FR-102, FR-103a, FR-108, FR-109
- Pricing + reconciliation + drift: FR-110, FR-111, FR-607
- Graph/HRE/confidence core: FR-200 through FR-204, FR-210 through FR-214
- Interceptor fail-open and hot-path behavior: FR-300 through FR-304, FR-309
- Active-Lite guardrails and gate response schema (MVP subset): FR-305a (shape/gate thresholds, semantic bypass conditions, bounded headers, fail-open fallbacks)
- Dashboard/demo views and flows: FR-400 through FR-405, FR-407, FR-409, DEM-01 through DEM-06, DEM-09, DEM-10
- Interventions and policy surfaces: FR-500 through FR-502, FR-504, FR-600 through FR-606
- Forecasting and integrations API surfaces: FR-408, FR-800, FR-801, FR-803
- Engineering spec index contract and versioning semantics: Section 6.4/6.5, Section 9.2 header contract alignment
- Onboarding automation artifacts: FR-112 (AWS IaC + Okta/GitHub scoped onboarding guides)

Partially implemented in local code (remaining pieces require tenant cloud wiring, control plane, or external providers):

- FR-305 full Active mode and FR-306 advanced equivalence modes (Mode 2a/2b are scaffolded but not productionized)
- FR-307 probabilistic shadow warming exists; full semantic cache service isolation hard caps are policy/config-level and infra-dependent
- FR-503/FR-606 CI/CD pre-deploy simulation is implemented at API/domain level; organization-specific PR-comment workflows depend on tenant CI auth/secrets
- FO-06 crash-level fail-open depends on service mesh/ingress outlier-ejection in customer environment

Roadmap or external-by-definition items not fully implementable inside this repository alone:

- Additional provider/connectors: FR-100b, FR-101b, FR-103b, FR-104, FR-105, FR-106, FR-107
- Carbon-ledger roadmap scope: FR-406, FR-700 through FR-703 (architectural modules exist; full enterprise-grade reporting pipeline is phased)
- Organization/compliance milestones: SEC-07 (SOC 2 programs), SLA/contract commitments (NFR-07), deployment-specific trust-boundary controls that require customer infra ownership

## Remaining Alignment Items (Non-Code / External Dependency)

1. FO-06 mesh-level crash bypass
   - Requires customer-side service mesh or equivalent traffic failover control.

2. Production mTLS/TLS and enterprise secret backends
   - Final posture depends on cluster ingress/service mesh, cert management, and customer KMS/Secrets Manager integrations.

3. Live connector credentialing and principal scoping
   - Okta/GitHub/AWS live auth wiring requires tenant-specific secrets and IAM trust setup.

4. Compliance program milestones
   - SOC 2 Type 1/2 and formal security review packets are organizational programs, not code-only changes.

5. P1/P2 roadmap scope
   - Azure/GCP ingestion expansions, full Active mode, and non-MVP carbon reporting remain intentionally phased.
