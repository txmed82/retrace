from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.storage import AppErrorAlertRuleRow, Storage


_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class AlertRuleDecision:
    state: str
    rule_id: str = ""
    rule_public_id: str = ""
    rule_name: str = ""
    action: str = "alert"

    def metadata(self) -> dict[str, Any]:
        payload = {"alert_state": self.state, "alert_action": self.action}
        if self.rule_id:
            payload["alert_rule_id"] = self.rule_id
        if self.rule_public_id:
            payload["alert_rule_public_id"] = self.rule_public_id
        if self.rule_name:
            payload["alert_rule_name"] = self.rule_name
        return payload


def evaluate_app_error_alert_rules(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    alert: Any,
) -> AlertRuleDecision:
    rules = store.list_app_error_alert_rules(
        project_id=project_id,
        environment_id=environment_id,
        enabled=True,
    )
    for rule in rules:
        if _matches(rule, alert):
            state = "suppressed" if rule.action == "suppress" else "active"
            return AlertRuleDecision(
                state=state,
                rule_id=rule.id,
                rule_public_id=rule.public_id,
                rule_name=rule.name,
                action=rule.action,
            )
    return AlertRuleDecision(state="active", action="alert")


def _matches(rule: AppErrorAlertRuleRow, alert: Any) -> bool:
    if rule.min_severity and _severity_score(alert.severity) < _severity_score(
        rule.min_severity
    ):
        return False
    if rule.provider and rule.provider != alert.provider.lower():
        return False
    if rule.title_contains and rule.title_contains.lower() not in (
        alert.title + "\n" + alert.summary
    ).lower():
        return False
    if rule.fingerprint_contains and rule.fingerprint_contains.lower() not in (
        alert.fingerprint + "\n" + alert.external_id
    ).lower():
        return False
    if rule.route_contains:
        route = "\n".join(
            str(alert.metadata.get(key) or "")
            for key in ("route", "transaction", "url", "current_url")
        )
        if rule.route_contains.lower() not in route.lower():
            return False
    return True


def _severity_score(value: str) -> int:
    return _SEVERITY_ORDER.get(str(value or "medium").strip().lower(), 2)
