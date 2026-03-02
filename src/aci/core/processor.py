"""
Attribution processor: orchestrates Phases 2-4 of the data flow.

Phase 2: Reconciliation - HRE processes events, resolves entities.
Phase 3: Attribution   - Graph traversal, fractional attribution, confidence.
Phase 4: Materialization - Index builder emits precomputed entries.

This processor operates ASYNCHRONOUSLY. It is never on the decision-time
critical path. The interceptor reads only from the materialized index.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from aci.confidence.calibration import CalibrationEngine
from aci.core.event_bus import InMemoryEventBus
from aci.graph.store import GraphStore
from aci.hre.engine import HeuristicReconciliationEngine, ReconciliationContext
from aci.index.materializer import IndexMaterializer
from aci.models.attribution import AttributionResult
from aci.models.events import DomainEvent, EventType
from aci.models.graph import EdgeProvenance, EdgeType, GraphEdge, GraphNode, NodeType
from aci.policy.engine import PolicyEngine

logger = structlog.get_logger()


class AttributionProcessor:
    """
    End-to-end attribution pipeline connecting event ingestion to
    index materialization.

    Subscribes to the event bus and processes events through:
    1. Graph update (add/modify nodes and edges).
    2. Entity resolution via HRE.
    3. Confidence calibration.
    4. Index materialization.

    This is the closed-loop control mechanism described in Section 3.3:
    derived attribution data influences decision-time behavior, and execution
    outcomes generate events that update subsequent attribution.
    """

    def __init__(
        self,
        event_bus: InMemoryEventBus,
        graph_store: GraphStore,
        hre: HeuristicReconciliationEngine,
        calibration: CalibrationEngine,
        materializer: IndexMaterializer,
        policy_engine: PolicyEngine,
    ) -> None:
        self.event_bus = event_bus
        self.graph = graph_store
        self.hre = hre
        self.calibration = calibration
        self.materializer = materializer
        self.policy_engine = policy_engine

        self._processed_count: int = 0

        # Subscribe to relevant event types.
        self.event_bus.subscribe(EventType.INFERENCE_REQUEST.value, self._handle_inference)
        self.event_bus.subscribe(EventType.DEPLOYMENT.value, self._handle_deployment)
        self.event_bus.subscribe(EventType.BILLING_LINE_ITEM.value, self._handle_billing)
        self.event_bus.subscribe(EventType.ATTRIBUTION_CORRECTION.value, self._handle_correction)
        self.event_bus.subscribe(EventType.ORG_CHANGE.value, self._handle_org_change)

    def _handle_inference(self, event: DomainEvent) -> None:
        """
        Process an inference event.

        Attempts to resolve the inference workload to an organizational owner,
        calibrate the confidence, and materialize an index entry.
        """
        attrs = event.attributes
        workload_id = attrs.get("service_name") or attrs.get("cloud_resource_arn") or event.subject_id

        # Build reconciliation context from graph state.
        context = self._build_context(workload_id, event.event_time)

        # Run HRE.
        result = self.hre.resolve(
            entity_id=workload_id,
            entity_type="inference_endpoint",
            event_time=event.event_time,
            context=context,
        )

        if result.combined_confidence > 0:
            # Calibrate the confidence score.
            method = result.explanation.method_used if result.explanation else "unknown"
            primary_method = method.split("+")[0] if "+" in method else method
            calibrated = self.calibration.calibrate_score(primary_method, result.combined_confidence)

            # Update the result with calibrated confidence.
            result = AttributionResult(
                workload_id=result.workload_id,
                attribution_path=result.attribution_path,
                combined_confidence=calibrated,
                explanation=result.explanation,
                fractional_attributions=result.fractional_attributions,
                conflicts=result.conflicts,
            )

            # Collect policy context.
            team_id = result.explanation.target_entity if result.explanation else ""
            policy_ctx = {}
            if team_id:
                budget = self.policy_engine.get_budget_context(team_id)
                allowlist = self.policy_engine.get_model_allowlist(team_id)
                tokens = self.policy_engine.get_token_budgets(team_id)
                policy_ctx = {**budget, "model_allowlist": allowlist, **tokens}

            # Materialize into the index.
            self.materializer.materialize_attribution(result, policies=policy_ctx)

        self._processed_count += 1
        logger.info(
            "processor.inference_processed",
            workload_id=workload_id,
            confidence=result.combined_confidence,
        )

    def _handle_deployment(self, event: DomainEvent) -> None:
        """Process a deployment event: update graph with deployment linkages."""
        attrs = event.attributes
        service_name = attrs.get("service_name", "")
        deployer = attrs.get("deployer_identity", "")
        target_arn = attrs.get("target_resource_arn", "")

        # Upsert deployment node.
        deploy_id = f"deploy:{attrs.get('deploy_job_id', event.event_id)}"
        self.graph.upsert_node(GraphNode(
            node_id=deploy_id,
            node_type=NodeType.DEPLOYMENT,
            label=service_name,
            properties=attrs,
        ))

        # Link deployment to resource.
        if target_arn:
            self.graph.upsert_node(GraphNode(
                node_id=f"resource:{target_arn}",
                node_type=NodeType.CLOUD_RESOURCE,
                label=target_arn.split("/")[-1] if "/" in target_arn else target_arn,
            ))
            self.graph.add_edge(GraphEdge(
                edge_type=EdgeType.TRIGGERS,
                from_id=deploy_id,
                to_id=f"resource:{target_arn}",
                confidence=1.0,
                weight=1.0,
                valid_from=event.event_time,
                provenance=EdgeProvenance(source="cicd", method="R1"),
            ))

        # Link deployer to deployment.
        if deployer:
            self.graph.add_edge(GraphEdge(
                edge_type=EdgeType.DEPLOYED_BY,
                from_id=f"person:{deployer}",
                to_id=deploy_id,
                confidence=1.0,
                weight=1.0,
                valid_from=event.event_time,
                provenance=EdgeProvenance(source="cicd", method="R1"),
            ))

        self._processed_count += 1

    def _handle_billing(self, event: DomainEvent) -> None:
        """Process a billing line item: update resource cost data."""
        attrs = event.attributes
        resource_arn = attrs.get("resource_arn", "")

        if resource_arn:
            node = self.graph.get_node(f"resource:{resource_arn}")
            if node:
                # Update resource cost properties.
                cost = attrs.get("cost_usd", 0.0)
                node.properties["last_billed_cost_usd"] = cost
                node.properties["billing_tags"] = attrs.get("tags", {})
                self.graph.upsert_node(node)

        self._processed_count += 1

    def _handle_correction(self, event: DomainEvent) -> None:
        """
        Process a manual attribution correction.

        Corrections feed back as ground truth labels for calibration
        (Section 5.2) and trigger re-materialization.
        """
        attrs = event.attributes
        from aci.models.confidence import GroundTruthLabel

        label = GroundTruthLabel(
            attribution_id=attrs.get("attribution_id", ""),
            true_team_id=attrs.get("true_team_id", ""),
            true_cost_center_id=attrs.get("true_cost_center_id", ""),
            source="user_correction",
            predicted_team_id=attrs.get("predicted_team_id", ""),
            predicted_confidence=attrs.get("predicted_confidence", 0.0),
            method_used=attrs.get("method_used", ""),
            was_correct=attrs.get("was_correct", False),
        )

        self.calibration.add_ground_truth(label)
        self._processed_count += 1
        logger.info("processor.correction_processed", attribution_id=label.attribution_id)

    def _handle_org_change(self, event: DomainEvent) -> None:
        """
        Process organizational hierarchy changes.

        Time-versioned edges: close old membership, create new one.
        Triggers re-attribution for affected workloads.
        """
        attrs = event.attributes
        person_id = f"person:{attrs.get('person_id', '')}"
        old_team = attrs.get("previous_team", "")
        new_team = attrs.get("new_team", "")

        if old_team and new_team:
            # Close old membership edge and create new one.
            self.graph.add_edge(GraphEdge(
                edge_type=EdgeType.MEMBER_OF,
                from_id=person_id,
                to_id=f"team:{new_team}",
                confidence=1.0,
                weight=1.0,
                valid_from=event.event_time,
                provenance=EdgeProvenance(source="hr", method="R1"),
            ))

        self._processed_count += 1

    def _build_context(
        self,
        workload_id: str,
        event_time: datetime,
    ) -> ReconciliationContext:
        """
        Build reconciliation context from current graph state.

        Extracts identity mappings, temporal events, naming patterns,
        and historical attributions relevant to the workload.
        """
        ctx = ReconciliationContext()

        # R1: Check for direct identifier mappings in the graph.
        # Look for edges from the workload to known entities.
        edges = self.graph.get_edges_from(workload_id, at_time=event_time)
        for edge in edges:
            target = self.graph.get_node(edge.to_id)
            if target and target.node_type in (NodeType.TEAM, NodeType.PERSON):
                ctx.identity_mappings[workload_id] = target.node_id

        # R3: Build naming patterns from team-owned repositories.
        teams = self.graph.get_nodes_by_type(NodeType.TEAM)
        for team in teams:
            owned_edges = self.graph.get_edges_from(team.node_id)
            patterns = []
            for e in owned_edges:
                target = self.graph.get_node(e.to_id)
                if target and target.node_type == NodeType.REPOSITORY:
                    patterns.append(target.label)
            if patterns:
                ctx.naming_patterns[team.node_id] = patterns

        return ctx

    @property
    def stats(self) -> dict:
        return {
            "processed_events": self._processed_count,
            "graph_nodes": self.graph.get_stats()["total_nodes"],
            "graph_edges": self.graph.get_stats()["total_edges"],
            "index_size": self.materializer.store.size,
        }
