import re

from app.modules.co_debug.dependency.models import MakeRule


class PatternRuleResolver:
    """Resolve the small, safe subset of single-`%` Make pattern rules."""

    def resolve(self, target: str, rules: list[MakeRule]) -> MakeRule | None:
        for rule in rules:
            stem = self._match(rule.target, target)
            if stem is None:
                continue
            return MakeRule(
                target=target,
                normal_prerequisites=[
                    item.replace("%", stem) for item in rule.normal_prerequisites
                ],
                order_only_prerequisites=[
                    item.replace("%", stem) for item in rule.order_only_prerequisites
                ],
                recipes=list(rule.recipes),
                line_number=rule.line_number,
            )
        return None

    def target_for_prerequisite(self, prerequisite: str, rules: list[MakeRule]) -> str | None:
        for rule in rules:
            if rule.target.count("%") != 1:
                continue
            for pattern in rule.normal_prerequisites:
                stem = self._match(pattern, prerequisite)
                if stem is not None:
                    return rule.target.replace("%", stem)
        return None

    @staticmethod
    def _match(pattern: str, target: str) -> str | None:
        if pattern.count("%") != 1:
            return None
        prefix, suffix = pattern.split("%", 1)
        match = re.fullmatch(f"{re.escape(prefix)}(.*){re.escape(suffix)}", target)
        return match.group(1) if match else None
