from __future__ import annotations

from dataclasses import dataclass
import re

from .skills import Skill, SkillRegistry

SIDE_EFFECT_WORDS = ("send", "email", "publish", "post", "spend", "pay", "purchase", "buy", "invoice", "transfer")
QUICK_PREFIXES = ("what is ", "who is ", "when is ", "where is ", "define ", "summarize my ", "list my ")


def needs_human_approval(prompt: str) -> bool:
    words = set(re.findall(r"[a-z]+", prompt.lower()))
    return any(word in words for word in SIDE_EFFECT_WORDS)


@dataclass
class Route:
    tier: int
    skill: Skill | None
    reason: str


class Router:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def route(self, prompt: str) -> Route:
        lowered = prompt.strip().lower()
        skill = self.registry.match(prompt)
        if lowered in {"help", "status", "list skills", "show skills", "show metrics", "list metrics"} or lowered.startswith(("list skills", "metric ")):
            return Route(1, skill, "explicit local command")
        if skill:
            return Route(skill.tier, skill, f"matched skill: {skill.id}")
        if needs_human_approval(prompt):
            return Route(3, None, "possible external side effect; human review required")
        if lowered.startswith(QUICK_PREFIXES) and len(lowered) < 180:
            return Route(2, None, "short factual question candidate")
        return Route(3, None, "substantial or ambiguous work defaults to agent backend")
