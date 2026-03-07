# Demo Feature Matrix

This matrix maps the investor demo implementation to product and engineering requirements.

## PRD Demo Scope

| Requirement | Coverage in Repo |
|---|---|
| DEM-01 Overview dashboard | `frontend/index.html` (`Overview`): financial summary, spend trends, provider/cloud distribution, optimization categories, team attribution |
| DEM-02 Six-layer attribution chain | `frontend/index.html` (`Attribution`): request selector, 6-layer chain, source/method confidence, request cost explanation |
| DEM-03 Model Intelligence | `frontend/index.html` (`Models`): per-model spend, request volume, latency, variance, trend, optimization potential |
| DEM-04 Teams view | `frontend/index.html` (`Teams`): per-team spend, budget utilization, optimization potential, drill-down detail |
| DEM-05 Interventions | `frontend/index.html` (`Interventions`): multi-status recommendation set, methodology indicators, session-persistent lifecycle actions |
| DEM-06 Governance view | `frontend/index.html` (`Governance`): deployment modes, policy table, fail-open matrix, trust boundary summary |
| DEM-09 Simulated data clarity | Persistent `SIMULATED DATA` watermark + synthetic dataset in `frontend/data/demo_dataset.json` |
| DEM-10 Deployable demo surface | FastAPI serves UI at `/platform/`; scenario runs via `/v1/demo/bootstrap` and `/v1/intercept` |

## Additional Implemented Capabilities

| Requirement | Coverage in Repo |
|---|---|
| FR-408 Cost forecasting | `frontend/index.html` (`Forecasting`) + `POST /v1/forecast/spend` |
| FR-503/FR-606 Cost simulation | `POST /v1/interventions/cost-simulate` endpoint in API surface |
| FR-800/FR-801/FR-803 Integrations | `frontend/index.html` (`Integrations`) + `/v1/integrations/notify` + `/v1/integrations/deliveries` |
| FR-110/FR-111 Pricing and reconciliation | `/v1/pricing/*` and `/v1/finops/*` endpoints |
| FR-607 Drift monitoring thresholds | `GET /v1/finops/drift` returns threshold-aware `investigation_required` + `dashboard_annotation` |
| FR-101a Identity ingestion (Okta) | `src/aci/ingestion/connectors/okta.py` (`OktaIdentityConnector`) for System Log + SCIM transforms |
| FR-102 SCM ingestion (GitHub) | `src/aci/ingestion/connectors/github.py` (`GitHubSCMConnector`) for push + PR-merge transforms |
| FR-103a CI/CD ingestion (GitHub Actions) | `src/aci/ingestion/connectors/github.py` (`GitHubActionsConnector`) for workflow-job deployment transforms |
| ES 6.4 / 6.5 index versioning & atomic durable writes | `src/aci/index/materializer.py` Redis hash schema with Lua atomic upsert and monotonic version persistence |
| FR-304 Advisory headers | Interceptor emits bounded `X-ACI-*` headers including cost, owner, confidence, advisory details, fail-open reason, and `X-ACI-Price-Snapshot-Id` |
| FR-301/FR-309 Fail-open posture | Fail-open scenario in execution stream + governance fail-open matrix |

## Reviewer Feedback Alignment

| Feedback Theme | Implemented Update |
|---|---|
| Remove carbon and benchmarking references | Removed from canonical demo UI/data/docs |
| Separate financial and technical context | Overview is financial-first; request-level data moved to collapsible execution drawer + attribution view |
| Intervention actions must work | Approve/reject/dismiss/review actions mutate state, persist for session, and update overview savings |
| Add methodology transparency | Intervention cards + FAQ + glossary include category, threshold condition, and conditional rule definitions |
| Improve architecture visuals | Vector diagram with bi-directional flow arrows and labeled control/feedback paths |
| Improve mode indicator clarity | Clickable top-bar mode control with explicit Passive/Advisory/Active behavior |

## Demo Entry Points

- One-command launch: `./scripts/run_demo.sh`
- Browser: `http://localhost:8000/platform/`
- Core walkthrough: Open **Execution Stream** -> **Bootstrap Demo Data** -> **Run Full Sequence**
