"""
Policy engine: evaluates governance policies against attribution context.

Policies are pre-evaluated during index materialization where possible,
so the interceptor only needs O(1) threshold checks at decision time.
Complex policies are evaluated here during the materialization phase.
"""

from __future__ import annotations

import structlog

from aci.models.carbon import (
    EnforcementAction,
    PolicyDefinition,
    PolicyEvaluationResult,
    PolicyType,
)

logger = structlog.get_logger()


class PolicyEngine:
    """
    Evaluates governance policies and pre-computes enforcement constraints
    for embedding in the attribution index.
    """

    def __init__(self) -> None:
        self.policies: dict[str, PolicyDefinition] = {}

    def register_policy(self, policy: PolicyDefinition) -> None:
        """Register or update a policy definition."""
        self.policies[policy.policy_id] = policy
        logger.info("policy.registered", policy_id=policy.policy_id, name=policy.name)

    def remove_policy(self, policy_id: str) -> bool:
        """Remove a policy."""
        return self.policies.pop(policy_id, None) is not None

    def evaluate_all(
        self,
        context: dict,
        scope_filter: str = "global",
    ) -> list[PolicyEvaluationResult]:
        """
        Evaluate all active policies against a context.

        Used during materialization to pre-compute constraints.
        """
        results: list[PolicyEvaluationResult] = []
        for policy in self.policies.values():
            if not policy.enabled:
                continue
            if scope_filter != "global" and policy.scope != scope_filter:
                if policy.scope != "global":
                    continue

            result = self._evaluate_single(policy, context)
            results.append(result)

        return results

    def get_model_allowlist(self, team_id: str) -> list[str]:
        """Get the production model allowlist for a team."""
        for policy in self.policies.values():
            if policy.policy_type == PolicyType.MODEL_ALLOWLIST and policy.enabled:
                return policy.parameters.get("allowed_models", [])
        return []

    def get_budget_context(self, team_id: str) -> dict:
        """Get budget constraints for a team."""
        for policy in self.policies.values():
            if policy.policy_type == PolicyType.BUDGET_CEILING and policy.enabled:
                params = policy.parameters
                if params.get("team_id") == team_id or policy.scope == "global":
                    return {
                        "budget_limit_usd": params.get("limit_usd", 0),
                        "budget_remaining_usd": params.get("remaining_usd", 0),
                    }
        return {}

    def get_token_budgets(self, team_id: str) -> dict:
        """Get token budget constraints."""
        for policy in self.policies.values():
            if policy.policy_type == PolicyType.TOKEN_BUDGET and policy.enabled:
                params = policy.parameters
                return {
                    "token_budget_output": params.get("max_output_tokens"),
                    "token_budget_input": params.get("max_input_tokens"),
                }
        return {}

    def _evaluate_single(
        self,
        policy: PolicyDefinition,
        context: dict,
    ) -> PolicyEvaluationResult:
        """Evaluate a single policy against context."""
        violated = False
        details = ""

        if policy.policy_type == PolicyType.BUDGET_CEILING:
            limit = policy.parameters.get("limit_usd", 0)
            current = context.get("current_spend_usd", 0)
            if current > limit:
                violated = True
                details = f"Spend ${current:,.0f} exceeds limit ${limit:,.0f}"

        elif policy.policy_type == PolicyType.MODEL_ALLOWLIST:
            model = context.get("model", "")
            allowed = policy.parameters.get("allowed_models", [])
            if model and allowed and model not in allowed:
                violated = True
                details = f"Model {model} not in allowlist"

        elif policy.policy_type == PolicyType.CONFIDENCE_FLOOR:
            confidence = context.get("confidence", 1.0)
            floor = policy.parameters.get("min_confidence", 0.8)
            if confidence < floor:
                violated = True
                details = f"Confidence {confidence:.2f} below floor {floor:.2f}"

        elif policy.policy_type == PolicyType.SHADOW_DETECTION:
            has_cicd = context.get("has_cicd_linkage", True)
            daily_cost = context.get("daily_cost_usd", 0)
            threshold = policy.parameters.get("cost_threshold_daily_usd", 500)
            if not has_cicd and daily_cost > threshold:
                violated = True
                details = f"Unregistered service: ${daily_cost:.0f}/day, no CI/CD linkage"

        return PolicyEvaluationResult(
            policy_id=policy.policy_id,
            policy_name=policy.name,
            action=policy.enforcement if violated else EnforcementAction.ALLOW,
            violated=violated,
            details=details,
        )
