"""Provider registry and parent discovery skill."""

from __future__ import annotations

from reuse_layer.integrations.providers import load_providers


def test_provider_registry_separates_available_and_planned_sources() -> None:
    providers = {provider.id: provider for provider in load_providers()}

    assert providers["github"].status == "available"
    assert providers["github"].skill == "github-oss-discovery"
    assert {"search_github", "find_reusable"} <= set(providers["github"].tools)
    assert providers["huggingface"].status == "planned"
    assert providers["kaggle"].status == "planned"
    assert providers["huggingface"].tools == ()


def test_parent_skill_routes_via_registry_and_shared_result_contract() -> None:
    from reuse_layer.adapters.mcp_server import parent_discovery_workflow

    skill = parent_discovery_workflow()
    assert "list_providers" in skill
    assert "normalized" in skill
    assert "not searchable yet" in skill
