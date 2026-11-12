"""Provider capability registry shared with the MCP discovery parent."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

ProviderStatus = Literal["available", "planned"]


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    status: ProviderStatus
    artifact_types: tuple[str, ...]
    strengths: tuple[str, ...]
    skill: str | None = None
    tools: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "artifact_types": list(self.artifact_types),
            "strengths": list(self.strengths),
            "skill": self.skill,
            "tools": list(self.tools),
        }


def load_providers() -> tuple[Provider, ...]:
    """Load and validate the packaged provider registry."""
    data = tomllib.loads(
        resources.files(__package__).joinpath("providers.toml").read_text(encoding="utf-8")
    )
    if data.get("schema_version") != 1:
        raise ValueError("unsupported provider registry schema_version")

    raw_providers = data.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise ValueError("provider registry must contain at least one provider")

    providers: list[Provider] = []
    seen: set[str] = set()
    for raw in raw_providers:
        if not isinstance(raw, dict):
            raise ValueError("provider registry entries must be tables")
        provider_id = raw.get("id")
        status = raw.get("status")
        if not isinstance(provider_id, str) or not provider_id or provider_id in seen:
            raise ValueError("provider ids must be non-empty and unique")
        if status not in ("available", "planned"):
            raise ValueError(f"provider {provider_id!r} has an invalid status")
        artifact_types = raw.get("artifact_types")
        strengths = raw.get("strengths")
        tools = raw.get("tools", [])
        skill = raw.get("skill")
        name = raw.get("name")
        if (
            not isinstance(name, str)
            or not isinstance(artifact_types, list)
            or not all(isinstance(item, str) and item for item in artifact_types)
            or not isinstance(strengths, list)
            or not all(isinstance(item, str) and item for item in strengths)
            or not isinstance(tools, list)
            or not all(isinstance(item, str) and item for item in tools)
            or (skill is not None and not isinstance(skill, str))
        ):
            raise ValueError(f"provider {provider_id!r} has invalid registry fields")
        if status == "available" and (skill is None or not tools):
            raise ValueError(f"available provider {provider_id!r} must declare a skill and tools")
        if status == "planned" and (skill is not None or tools):
            raise ValueError(f"planned provider {provider_id!r} cannot declare active integrations")
        seen.add(provider_id)
        providers.append(
            Provider(
                id=provider_id,
                name=name,
                status=status,
                artifact_types=tuple(artifact_types),
                strengths=tuple(strengths),
                skill=skill,
                tools=tuple(tools),
            )
        )
    return tuple(providers)
