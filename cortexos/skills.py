from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    id: str
    name: str
    domain: str
    description: str
    trigger_phrases: list[str]
    tier: int
    approval_required: bool
    automation_ready: bool
    path: Path
    instructions: str


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root

    def list(self) -> list[Skill]:
        found = []
        for manifest in sorted(self.root.glob("**/skill.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                instructions_path = manifest.parent / data.get("instructions", "SKILL.md")
                found.append(Skill(
                    id=data.get("id", manifest.parent.name),
                    name=data.get("name", manifest.parent.name),
                    domain=data.get("domain", manifest.parent.parent.name),
                    description=data.get("description", ""),
                    trigger_phrases=data.get("trigger_phrases", []),
                    tier=int(data.get("tier", 3)),
                    approval_required=bool(data.get("approval_required", False)),
                    automation_ready=bool(data.get("automation_ready", False)),
                    path=manifest.parent,
                    instructions=instructions_path.read_text(encoding="utf-8") if instructions_path.exists() else "",
                ))
            except (OSError, ValueError, TypeError):
                continue
        return found

    def get(self, skill_id: str) -> Skill | None:
        return next((item for item in self.list() if item.id == skill_id), None)

    def match(self, prompt: str) -> Skill | None:
        lowered = prompt.lower()
        scored = []
        for item in self.list():
            score = max((len(phrase) for phrase in item.trigger_phrases if phrase.lower() in lowered), default=0)
            if score:
                scored.append((score, item))
        return max(scored, key=lambda pair: pair[0])[1] if scored else None
