"""
Policy engine: evaluates governance policies against attribution context.

Policies are pre-evaluated during index materialization where possible,
so the interceptor only needs O(1) threshold checks at decision time.
Complex policies are evaluated here during the materialization phase.
"""

from __future__ import annotations

from typing import Any

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
        context: dict[str, Any],
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
            if (
                scope_filter != "global"
                and policy.scope != scope_filter
                and policy.scope != "global"
            ):
                continue

            result = self._evaluate_single(policy, context)
            results.append(result)

        return results

    def get_model_allowlist(self, team_id: str) -> list[str]:
        """Get the production model allowlist for a team."""
        global_allowlist: list[str] = []
        for policy in self.policies.values():
            if policy.policy_type != PolicyType.MODEL_ALLOWLIST or not policy.enabled:
                continue
            if self._policy_matches_team(policy, team_id):
                allowed = policy.parameters.get("allowed_models", [])
                return [str(model) for model in allowed]
            if policy.scope == "global":
                global_allowlist = [str(model) for model in policy.parameters.get("allowed_models", [])]
        return global_allowlist

    def get_budget_context(self, team_id: str) -> dict[str, float]:
        """Get budget constraints for a team."""
        global_budget: dict[str, float] = {}
        for policy in self.policies.values():
            if policy.policy_type != PolicyType.BUDGET_CEILING or not policy.enabled:
                continue
            params = policy.parameters
            budget = {
                "budget_limit_usd": float(params.get("limit_usd", 0)),
                "budget_remaining_usd": float(params.get("remaining_usd", 0)),
            }
            if self._policy_matches_team(policy, team_id):
                return budget
            if policy.scope == "global":
                global_budget = budget
        return global_budget

    def get_token_budgets(self, team_id: str) -> dict[str, int | None]:
        """Get token budget constraints."""
        global_tokens: dict[str, int | None] = {}
        for policy in self.policies.values():
            if policy.policy_type != PolicyType.TOKEN_BUDGET or not policy.enabled:
                continue
            params = policy.parameters
            token_budget = {
                "token_budget_output": (
                    int(params["max_output_tokens"])
                    if params.get("max_output_tokens") is not None
                    else None
                ),
                "token_budget_input": (
                    int(params["max_input_tokens"])
                    if params.get("max_input_tokens") is not None
                    else None
                ),
            }
            if self._policy_matches_team(policy, team_id):
                return token_budget
            if policy.scope == "global":
                global_tokens = token_budget
        return global_tokens

    def _evaluate_single(
        self,
        policy: PolicyDefinition,
        context: dict[str, Any],
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

    @staticmethod
    def _policy_matches_team(policy: PolicyDefinition, team_id: str) -> bool:
        """Return True when the policy explicitly targets the given team."""
        policy_team = str(policy.parameters.get("team_id", ""))
        return policy.scope == team_id or policy_team == team_id
