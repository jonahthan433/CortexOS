from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

RISK_LEVELS = {"read_only", "local_write", "external_action", "financial", "unknown"}
HARD_RISK_LEVELS = {"external_action", "financial", "unknown"}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([+.-][0-9A-Za-z.-]+)?$")
DERIVED_VERSION = "1.0.0"
HISTORY_LIMIT = 20
TEST_EXPECT_KEYS = {"skill_id", "tier", "requires_approval"}


@dataclass
class Skill:
    id: str
    name: str
    domain: str
    description: str
    trigger_phrases: list[str]
    tier: int
    risk_level: str
    approval_required: bool
    automation_ready: bool
    path: Path
    instructions: str
    version: str = DERIVED_VERSION
    version_source: str = "derived"
    content_hash: str = ""
    tests: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    instructions_name: str = "SKILL.md"


@dataclass
class SkillScan:
    skills: list[Skill]
    problems: list[dict]


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")


def declared_gate(skill: Skill | None) -> dict | None:
    """The manifest-declared approval gate for one skill, or None without a skill."""
    if skill is None:
        return None
    if skill.risk_level in HARD_RISK_LEVELS:
        return {"risk_level": skill.risk_level, "requires_approval": True,
                "reason": "Declared in skill manifest"}
    return {"risk_level": skill.risk_level, "requires_approval": skill.approval_required,
            "reason": "Declared in skill manifest"}


def compute_content_hash(manifest: dict, instructions_name: str, instructions: str) -> str:
    """Normalized hash over the manifest data and its instruction text.

    Whitespace-only reformatting of skill.json does not change the hash, but any
    semantic change to the manifest, the instructions name, or the instructions
    text does — which scopes run/test evidence to an exact skill version.
    """
    digest = hashlib.sha256()
    digest.update(json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(instructions_name.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(instructions.encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _validate_tests(value) -> tuple[list[dict], list[str]]:
    problems: list[str] = []
    if value is None:
        return [], problems
    if not isinstance(value, list):
        return [], ["tests must be a list of test cases"]
    tests: list[dict] = []
    for index, case in enumerate(value):
        if not isinstance(case, dict):
            problems.append(f"tests[{index}] must be an object")
            continue
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            problems.append(f"tests[{index}] needs a non-empty prompt")
            continue
        expect = case.get("expect", {})
        if not isinstance(expect, dict):
            problems.append(f"tests[{index}].expect must be an object")
            continue
        unknown = sorted(set(expect) - TEST_EXPECT_KEYS)
        if unknown:
            problems.append(f"tests[{index}].expect has unsupported keys: {unknown}")
        normalized: dict = {}
        if "skill_id" in expect:
            if isinstance(expect["skill_id"], str):
                normalized["skill_id"] = expect["skill_id"]
            else:
                problems.append(f"tests[{index}].expect.skill_id must be a string")
        if "tier" in expect:
            if expect["tier"] in (1, 2, 3):
                normalized["tier"] = int(expect["tier"])
            else:
                problems.append(f"tests[{index}].expect.tier must be 1, 2, or 3")
        if "requires_approval" in expect:
            if isinstance(expect["requires_approval"], bool):
                normalized["requires_approval"] = expect["requires_approval"]
            else:
                problems.append(f"tests[{index}].expect.requires_approval must be a boolean")
        if not normalized:
            problems.append(f"tests[{index}] does not assert any supported outcome")
            continue
        name = case.get("name")
        tests.append({"name": name if isinstance(name, str) and name.strip() else prompt.strip()[:60],
                      "prompt": prompt.strip(), "expect": normalized})
    return tests, problems


def validate_manifest(data, manifest: Path) -> tuple[dict | None, list[str]]:
    """Normalize one skill manifest. Returns (fields, problems).

    fields is None only when the definition must be rejected outright: a
    non-object manifest, a missing/invalid id, or an unsafe instructions path.
    Everything else loads with conservative defaults and is surfaced through
    problems, so a bad edit never silently disables an existing skill.
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        return None, ["manifest is not a JSON object"]
    skill_id = data.get("id")
    if not isinstance(skill_id, str) or not ID_PATTERN.match(skill_id):
        return None, [f"missing or invalid skill id: {skill_id!r}"]
    # Two sanctioned shapes: id == folder, or "<domain>-<folder>" (the first-party
    # convention, e.g. skills/research/morning-intel -> research-morning-intel).
    if skill_id not in {manifest.parent.name, f"{manifest.parent.parent.name}-{manifest.parent.name}"}:
        problems.append(f"id '{skill_id}' does not match directory '{manifest.parent.name}'")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("missing or invalid name; falling back to id")
        name = skill_id
    domain = data.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        problems.append("missing or invalid domain; falling back to folder name")
        domain = manifest.parent.parent.name
    description = data.get("description")
    if not isinstance(description, str):
        problems.append("description must be a string")
        description = ""
    tier = data.get("tier")
    if tier not in (1, 2, 3):
        problems.append(f"invalid tier {tier!r}; defaulting to 3")
        tier = 3
    risk = data.get("risk_level")
    if risk not in RISK_LEVELS:
        problems.append(f"unknown risk_level {risk!r}; treated as unknown (approval required)")
        risk = "unknown"
    approval = data.get("approval_required", False)
    if not isinstance(approval, bool):
        problems.append("approval_required must be a boolean; treating the skill as gated")
        approval = True
    automation = data.get("automation_ready", False)
    if not isinstance(automation, bool):
        problems.append("automation_ready must be a boolean; treating as not ready")
        automation = False
    triggers = data.get("trigger_phrases", [])
    if not isinstance(triggers, list) or not all(isinstance(item, str) and item.strip() for item in triggers):
        problems.append("trigger_phrases must be a list of non-empty strings")
        triggers = [item for item in triggers if isinstance(item, str)] if isinstance(triggers, list) else []
    instructions_name = data.get("instructions", "SKILL.md")
    if not isinstance(instructions_name, str) or not instructions_name.strip():
        problems.append("instructions must name a file inside the skill folder")
        instructions_name = "SKILL.md"
    elif Path(instructions_name).is_absolute() or ".." in Path(instructions_name).parts:
        return None, [f"unsafe instructions path: {instructions_name!r}"]
    version = data.get("version")
    if version is None:
        version, version_source = DERIVED_VERSION, "derived"
    elif isinstance(version, str) and VERSION_PATTERN.match(version):
        version_source = "manifest"
    else:
        problems.append(f"invalid version {version!r}; deriving instead")
        version, version_source = DERIVED_VERSION, "derived"
    tests, test_problems = _validate_tests(data.get("tests"))
    problems.extend(test_problems)
    instructions_path = manifest.parent / instructions_name
    instructions = ""
    if instructions_path.is_file():
        try:
            instructions = instructions_path.read_text(encoding="utf-8")
        except OSError:
            problems.append(f"could not read instructions file {instructions_name!r}")
    if not instructions.strip():
        problems.append(f"instructions file {instructions_name!r} is missing or empty")
    fields = {
        "id": skill_id, "name": name.strip(), "domain": domain.strip(), "description": description,
        "trigger_phrases": triggers, "tier": int(tier), "risk_level": risk,
        "approval_required": approval, "automation_ready": automation,
        "instructions_name": instructions_name, "instructions": instructions,
        "version": version, "version_source": version_source, "tests": tests,
        "manifest": data,
    }
    return fields, problems


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root

    def scan(self) -> SkillScan:
        skills: list[Skill] = []
        problems: list[dict] = []
        seen: dict[str, str] = {}
        for manifest in sorted(self.root.glob("**/skill.json")):
            try:
                relative = str(manifest.relative_to(self.root))
            except ValueError:
                relative = str(manifest)
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                problems.append({"path": relative, "id": None, "rejected": True,
                                 "problems": [f"unreadable manifest: {exc}"]})
                continue
            fields, found = validate_manifest(data, manifest)
            if fields is None:
                problems.append({"path": relative, "id": data.get("id") if isinstance(data, dict) else None,
                                 "rejected": True, "problems": found})
                continue
            skill_id = fields["id"]
            if skill_id in seen:
                problems.append({"path": relative, "id": skill_id, "rejected": True,
                                 "problems": [f"duplicate id (first seen at {seen[skill_id]})"]})
                continue
            seen[skill_id] = relative
            if found:
                problems.append({"path": relative, "id": skill_id, "rejected": False, "problems": found})
            skills.append(Skill(
                id=skill_id, name=fields["name"], domain=fields["domain"], description=fields["description"],
                trigger_phrases=fields["trigger_phrases"], tier=fields["tier"], risk_level=fields["risk_level"],
                approval_required=fields["approval_required"], automation_ready=fields["automation_ready"],
                path=manifest.parent, instructions=fields["instructions"],
                version=fields["version"], version_source=fields["version_source"],
                content_hash=compute_content_hash(fields["manifest"], fields["instructions_name"], fields["instructions"]),
                tests=fields["tests"], problems=found,
                manifest=fields["manifest"], instructions_name=fields["instructions_name"],
            ))
        return SkillScan(skills=skills, problems=problems)

    def list(self) -> list[Skill]:
        return self.scan().skills

    def problems(self) -> list[dict]:
        return self.scan().problems

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


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=".skill-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SkillHistory:
    """Version snapshots per skill: baseline on first sight, appended on change, capped."""

    def __init__(self, root: Path, limit: int = HISTORY_LIMIT):
        self.root = root
        self.limit = limit

    def _path(self, skill_id: str) -> Path:
        return self.root / f"{skill_id}.json"

    def entries(self, skill_id: str) -> list[dict]:
        try:
            data = json.loads(self._path(skill_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("entries") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    def entry(self, skill_id: str, target_hash: str) -> dict | None:
        return next((item for item in self.entries(skill_id) if item.get("hash") == target_hash), None)

    def baseline_hash(self, skill_id: str) -> str | None:
        items = self.entries(skill_id)
        return items[0].get("hash") if items else None

    @staticmethod
    def _stamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _snapshot(skill: Skill, source: str) -> dict:
        return {
            "hash": skill.content_hash, "version": skill.version, "version_source": skill.version_source,
            "at": SkillHistory._stamp(), "source": source,
            "instructions_name": skill.instructions_name,
            "manifest": skill.manifest, "instructions": skill.instructions,
        }

    def _write(self, skill_id: str, items: list[dict]) -> None:
        payload = json.dumps({"skill": skill_id, "entries": items}, indent=2, ensure_ascii=False)
        _write_atomic(self._path(skill_id), payload + "\n")

    def sync(self, skills: list[Skill]) -> list[dict]:
        """Snapshot first-seen and changed skills; returns the newly recorded entries."""
        recorded: list[dict] = []
        for skill in skills:
            items = self.entries(skill.id)
            if items and items[-1].get("hash") == skill.content_hash:
                continue
            entry = self._snapshot(skill, "baseline" if not items else "change")
            items.append(entry)
            self._write(skill.id, items[-self.limit:])
            recorded.append({"skill": skill.id, "hash": entry["hash"], "version": entry["version"],
                             "source": entry["source"]})
        return recorded

    def record_rollback(self, skill_id: str, entry: dict, *, from_hash: str) -> dict:
        items = self.entries(skill_id)
        restored = {key: entry.get(key) for key in
                    ("hash", "version", "version_source", "instructions_name", "manifest", "instructions")}
        restored.update({"at": self._stamp(), "source": "rollback", "rolled_back_from": from_hash})
        items.append(restored)
        self._write(skill_id, items[-self.limit:])
        return restored

    def rollback(self, skill: Skill, entry: dict) -> dict:
        """Restore one stored version to disk. Callers must confirm and check the hash."""
        name = entry.get("instructions_name") or "SKILL.md"
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Stored version has an unsafe instructions path")
        manifest = entry.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("Stored version has no manifest to restore")
        _write_atomic(skill.path / "skill.json",
                      json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        _write_atomic(skill.path / name, entry.get("instructions") or "")
        return {"manifest": str(skill.path / "skill.json"), "instructions": str(skill.path / name)}
