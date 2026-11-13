"""Which version a pick is pinned to (``ref_policy``), and whether its language's package registry
publishes it at that version (``package``). Offline: a fake GitHub API and fake registries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from conftest import NOW, TOKEN, FakeGitHub, FakeRegistries, FakeRepo, make_repo, run, sha_for
from reuse_layer.adapters.cli import _print_find
from reuse_layer.config import Settings
from reuse_layer.core import engine as engine_module
from reuse_layer.core.engine import Engine
from reuse_layer.core.models import CallerContext, FindRequest, Release
from reuse_layer.core.store import Store
from reuse_layer.sources import build_sources
from reuse_layer.sources.github import GitHubSource, any_version, choose_release, choose_tag, standard_version
from reuse_layer.sources.packages import PYPI, choose_registry, github_key

README = "# widget\n\nParse widget files.\n\n## Features\n- Parse widget files into records\n"
PIN = sha_for("acme/widget", "v1.5.0")  # the default fake releases run v1.0.0 ... v1.5.0 (newest)


def widget(**fields: Any) -> FakeRepo:
    return FakeRepo(
        **{"full_name": "acme/widget", "description": "Parse widget files", "readme": README, **fields}
    )


def find(
    make_engine: Callable[..., Engine],
    repo: FakeRepo,
    *,
    policy: str = "standard",
    registries: FakeRegistries | None = None,
) -> tuple[dict[str, Any], dict[str, Any], FakeGitHub]:
    gh = FakeGitHub()
    gh.add(repo)
    engine = make_engine(gh, registries=registries)
    language = (repo.language or "").lower() or None
    req = FindRequest(
        task="Parse widget files into structured records",
        capabilities=["parse widget files"],
        candidates=[repo.full_name],
        context=CallerContext(language=language, ref_policy=policy),  # type: ignore[arg-type]
    )
    out = run(engine.find_reusable(req))
    assert out["picks"], out.get("rejected")
    return out["picks"][0], out, gh


# --------------------------------------------------------------------------- what counts as a version


@pytest.mark.parametrize(
    ("tag", "project", "expected"),
    [
        ("v1.2.3", None, (1, 2, 3, 0)),
        ("1.2", None, (1, 2, 0, 0)),
        ("V2.0.1.4", None, (2, 0, 1, 4)),
        ("tokio-1.37.0", "tokio", (1, 37, 0, 0)),  # the project's own name and a version
        ("ai@3.4.0", "ai", (3, 4, 0, 0)),
        ("v1", None, None),  # a major-only tag is usually moved to each new release
        ("v2.0.0-rc.1", None, None),
        ("release-1.2.3", None, None),
        ("tokio-macros-2.2.0", "tokio", None),  # a sub-package of the project, not the project
        ("nightly", None, None),
    ],
)
def test_standard_version_tags(tag: str, project: str | None, expected: tuple[int, ...] | None) -> None:
    assert standard_version(tag, project) == expected


def test_any_version_ranks_a_pre_release_just_below_its_release() -> None:
    rc, final, older = any_version("v2.0.0-rc.1"), any_version("v2.0.0"), any_version("v1.9.9")
    assert rc is not None and final is not None and older is not None
    assert older < rc < final
    assert any_version("2.0.0b1") == (2, 0, 0, 0) and any_version("1.0.0.dev3") == (1, 0, 0, 0)
    assert any_version("express@4.19.2") == (4, 19, 2, 1)  # "pre" inside a name is not a pre-release
    assert any_version("nightly") is None


def _rel(tag: str, days: int, pre: bool = False) -> Release:
    return Release(tag, NOW - timedelta(days=days), pre)


def test_standard_takes_the_highest_stable_plain_version_not_the_newest_release() -> None:
    releases = [_rel("v2.2.0-rc.1", 1, pre=True), _rel("nightly", 2), _rel("v1.9.5", 3), _rel("v2.1.0", 9)]
    release, passed = choose_release(releases, "standard")
    assert release is not None and release.tag == "v2.1.0"  # the later 1.9.5 backport does not win
    assert passed == {"v2.2.0-rc.1", "nightly"}


def test_all_takes_the_highest_version_of_any_kind() -> None:
    releases = [_rel("v2.2.0-rc.1", 1, pre=True), _rel("nightly", 2), _rel("v2.1.0", 9)]
    release, passed = choose_release(releases, "all")
    assert release is not None and release.tag == "v2.2.0-rc.1" and passed == set()
    rolling, _ = choose_release([_rel("latest", 5), _rel("nightly", 1)], "all")
    assert rolling is not None and rolling.tag == "nightly"  # no version anywhere: the newest
    assert choose_release([_rel("nightly", 1)], "standard") == (None, {"nightly"})


def test_choose_tag_by_policy() -> None:
    names = ["v1.2.0", "v1.10.0", "v2.0.0-beta.1", "docs", "widget-plugin-3.0.0"]
    assert choose_tag(names, "standard", "widget") == ("v1.10.0", {"v2.0.0-beta.1", "widget-plugin-3.0.0"})
    assert choose_tag(names, "all", "widget") == ("widget-plugin-3.0.0", set())
    assert choose_tag(["docs", "latest"], "standard") == (None, set())


# --------------------------------------------------------------------------- pinning under each policy


TAGGED = dict(release_tags=[("v2.0.0-rc.1", True), ("v1.5.1", False), ("v1.5.0", False)])


def test_standard_policy_pins_the_highest_stable_release(make_engine: Callable[..., Engine]) -> None:
    pick, out, _ = find(make_engine, widget(**TAGGED))
    assert (pick["version"], pick["version_kind"]) == ("v1.5.1", "release")
    assert pick["revision"] == sha_for("acme/widget", "v1.5.1")
    assert out["constraints"]["ref_policy"] == "standard"
    assert not any("pre-release" in r for r in pick.get("integration_risks", []))


def test_all_policy_pins_a_newer_pre_release_and_says_so(make_engine: Callable[..., Engine]) -> None:
    pick, out, _ = find(make_engine, widget(**TAGGED), policy="all")
    assert (pick["version"], pick["version_kind"]) == ("v2.0.0-rc.1", "prerelease")
    assert pick["revision"] == sha_for("acme/widget", "v2.0.0-rc.1")
    assert (
        "Pinned to a pre-release (v2.0.0-rc.1): its API may change before the final release"
        in (pick["integration_risks"])
    )
    assert out["constraints"]["ref_policy"] == "all"


def test_standard_policy_pins_the_branch_when_no_version_is_plain_and_says_why(
    make_engine: Callable[..., Engine],
) -> None:
    repo = widget(release_tags=[("widget-plugin@2.0.0", False), ("nightly", False)], tags=["docs-1.0"])
    pick, _, _ = find(make_engine, repo)
    assert (pick["version"], pick["version_kind"]) == ("main", "branch")
    assert pick["revision"] == sha_for("acme/widget", "main")
    risks = pick["integration_risks"]
    assert any(
        r.startswith("Pinned to the main branch head, not a release: its 3 releases or version tags")
        for r in risks
    )
    assert not any("No releases or version tags" in r for r in risks)  # it does publish versions

    pick_all, _, _ = find(make_engine, repo, policy="all")
    assert (pick_all["version"], pick_all["version_kind"]) == ("widget-plugin@2.0.0", "release")


def test_a_tag_named_after_the_project_is_its_standard_version(make_engine: Callable[..., Engine]) -> None:
    repo = widget(release_tags=[("widget-plugin-3.0.0", False), ("widget-2.1.0", False)])
    pick, _, _ = find(make_engine, repo)
    assert (pick["version"], pick["version_kind"]) == ("widget-2.1.0", "release")


def test_tags_without_releases_follow_the_policy(make_engine: Callable[..., Engine]) -> None:
    repo = widget(releases=0, tags=["v1.2.0", "v1.10.0", "v2.0.0-beta.1"])
    pick, _, _ = find(make_engine, repo)
    assert (pick["version"], pick["version_kind"]) == ("v1.10.0", "tag")
    pick_all, _, _ = find(make_engine, repo, policy="all")
    assert (pick_all["version"], pick_all["version_kind"]) == ("v2.0.0-beta.1", "prerelease")


# --------------------------------------------------------------------------- the registry package


PYPROJECT = '[project]\nname = "acme_widget"\ndynamic = ["version"]\n'


def _pypi(
    reg: FakeRegistries,
    *,
    source: str = "https://github.com/acme/widget",
    version: str = "1.5.0",
    yanked: bool = False,
) -> None:
    reg.add(
        f"https://pypi.org/pypi/acme-widget/{version}/json",
        {
            "info": {
                "name": "acme_widget",
                "version": version,
                "yanked": yanked,
                "project_urls": {"Source": source},
            }
        },
    )


def test_a_published_package_is_offered_next_to_the_github_source(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    _pypi(reg)
    pick, _, gh = find(make_engine, widget(files={"pyproject.toml": PYPROJECT}), registries=reg)
    assert pick["package"] == {
        "registry": "PyPI",
        "name": "acme_widget",
        "version": "1.5.0",
        "status": "published",
        "install": "pip install acme_widget==1.5.0",
    }
    assert pick["get"].startswith("git clone --depth 1 --branch v1.5.0 ")
    assert gh.contents_refs and set(gh.contents_refs) == {PIN}  # the manifest is read at the pinned commit
    assert reg.requests
    for req in reg.requests:  # a registry gets no token, and nothing but the package name and version
        assert "authorization" not in req.headers
        assert req.url.host == "pypi.org" and TOKEN not in str(req.url)


def test_a_package_that_only_shares_the_name_is_not_offered(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    _pypi(reg, source="https://github.com/squatter/acme-widget")
    pick, _, _ = find(make_engine, widget(files={"pyproject.toml": PYPROJECT}), registries=reg)
    package = pick["package"]
    assert package["status"] == "not_linked" and "install" not in package
    assert "does not name this repository as its source" in package["detail"]


def test_a_yanked_version_is_not_offered(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    _pypi(reg, yanked=True)
    pick, _, _ = find(make_engine, widget(files={"pyproject.toml": PYPROJECT}), registries=reg)
    assert pick["package"]["status"] == "yanked" and "install" not in pick["package"]


def test_a_version_the_registry_does_not_have_is_not_published(make_engine: Callable[..., Engine]) -> None:
    pick, _, _ = find(make_engine, widget(files={"pyproject.toml": PYPROJECT}), registries=FakeRegistries())
    assert pick["package"] == {
        "registry": "PyPI",
        "name": "acme_widget",
        "version": "1.5.0",
        "status": "not_published",
        "detail": "PyPI has no acme_widget 1.5.0",
    }


def test_a_branch_pin_is_looked_up_at_the_version_its_manifest_declares(
    make_engine: Callable[..., Engine],
) -> None:
    reg = FakeRegistries()
    _pypi(reg, version="0.4.0")
    repo = widget(
        releases=0, files={"pyproject.toml": '[project]\nname = "acme-widget"\nversion = "0.4.0"\n'}
    )
    pick, _, _ = find(make_engine, repo, registries=reg)
    assert pick["version_kind"] == "branch"
    package = pick["package"]
    assert (package["status"], package["version"]) == ("published", "0.4.0")
    assert "the branch may be ahead of that release" in package["detail"]


ECOSYSTEMS = [
    pytest.param(
        "TypeScript",
        {"package.json": '{"name": "@acme/widget", "version": "1.5.0"}'},
        "https://registry.npmjs.org/@acme%2Fwidget/1.5.0",
        {"repository": {"type": "git", "url": "git+https://github.com/acme/widget.git"}, "gitHead": PIN},
        {
            "registry": "npm",
            "name": "@acme/widget",
            "install": "npm install @acme/widget@1.5.0",
            "same_commit": True,
            "attested": False,  # npm keeps provenance, and has none for this version
        },
        id="npm",
    ),
    pytest.param(
        "Rust",
        {"Cargo.toml": '[package]\nname = "widget"\nversion.workspace = true\n'},
        "https://crates.io/api/v1/crates/widget/1.5.0",
        {"version": {"num": "1.5.0", "yanked": False, "repository": "https://github.com/acme/widget"}},
        {"registry": "crates.io", "name": "widget", "install": "cargo add widget@=1.5.0"},
        id="crates",
    ),
    pytest.param(
        "Go",
        {"go.mod": "module example.com/Acme/widget\n\ngo 1.22\n"},
        "https://proxy.golang.org/example.com/!acme/widget/@v/v1.5.0.info",  # vanity path, case-escaped
        {"Version": "v1.5.0", "Origin": {"VCS": "git", "URL": "https://github.com/acme/widget", "Hash": PIN}},
        {
            "registry": "Go module proxy",
            "name": "example.com/Acme/widget",
            "version": "v1.5.0",
            "install": "go get example.com/Acme/widget@v1.5.0",
            "same_commit": True,
        },
        id="go",
    ),
    pytest.param(
        "Ruby",
        {
            "widget.gemspec": 'Gem::Specification.new do |s|\n  s.name = "widget"\n  s.version = Widget::VERSION\nend\n'
        },
        "https://rubygems.org/api/v2/rubygems/widget/versions/1.5.0.json",
        {
            "name": "widget",
            "number": "1.5.0",
            "yanked": False,
            "source_code_uri": "https://github.com/acme/widget",
        },
        {
            "registry": "RubyGems",
            "name": "widget",
            "install": "gem install widget -v 1.5.0",
            "attested": False,
        },
        id="rubygems",
    ),
    pytest.param(
        "PHP",
        {"composer.json": '{"name": "acme/widget"}'},
        "https://repo.packagist.org/p2/acme/widget.json",
        {  # "minified": each entry lists only what changed from the one before
            "minified": "composer/2.0",
            "packages": {
                "acme/widget": [
                    {
                        "name": "acme/widget",
                        "version": "v1.6.0",
                        "source": {"url": "https://github.com/acme/widget.git", "reference": "f" * 40},
                    },
                    {
                        "version": "v1.5.0",
                        "source": {"url": "https://github.com/acme/widget.git", "reference": PIN},
                    },
                    {"version": "v1.4.0", "source": "__unset"},
                ]
            },
        },
        {
            "registry": "Packagist",
            "name": "acme/widget",
            "install": "composer require acme/widget:1.5.0",
            "same_commit": True,
        },
        id="packagist",
    ),
    pytest.param(
        "Java",
        {
            "pom.xml": '<?xml version="1.0"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">'
            "<groupId>com.acme</groupId><artifactId>widget</artifactId><version>1.6.0-SNAPSHOT</version>"
            "</project>"
        },
        "https://repo1.maven.org/maven2/com/acme/widget/1.5.0/widget-1.5.0.pom",
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>com.acme</groupId>'
        "<artifactId>widget</artifactId><version>1.5.0</version><scm>"
        "<connection>scm:git:git@github.com:acme/widget.git</connection>"
        f"<tag>{PIN}</tag></scm></project>",
        {
            "registry": "Maven Central",
            "name": "com.acme:widget",
            "install": "com.acme:widget:1.5.0",
            "same_commit": True,
        },
        id="maven",
    ),
    pytest.param(
        "C#",
        {
            "Acme.Widget.sln": "",
            "src/Acme.Widget/Acme.Widget.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            "<PackageId>Acme.Widget</PackageId><Version>$(WidgetVersion)</Version></PropertyGroup></Project>",
            "src/Acme.Widget.Tests/Acme.Widget.Tests.csproj": '<Project Sdk="Microsoft.NET.Sdk"/>',
        },
        "https://api.nuget.org/v3-flatcontainer/acme.widget/1.5.0/acme.widget.nuspec",
        '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://schemas.microsoft.com/packaging/2013/05/'
        'nuspec.xsd"><metadata><id>Acme.Widget</id><version>1.5.0</version>'
        f'<repository type="git" url="https://github.com/acme/widget" commit="{PIN}"/></metadata></package>',
        {
            "registry": "NuGet",
            "name": "Acme.Widget",
            "path": "src/Acme.Widget",  # found in a subdirectory; the test project is not a package
            "install": "dotnet add package Acme.Widget --version 1.5.0",
            "same_commit": True,
        },
        id="nuget",
    ),
]


@pytest.mark.parametrize(("language", "files", "url", "doc", "expected"), ECOSYSTEMS)
def test_each_registry_is_found_from_its_manifest(
    make_engine: Callable[..., Engine],
    language: str,
    files: dict[str, str],
    url: str,
    doc: Any,
    expected: dict[str, Any],
) -> None:
    reg = FakeRegistries()
    reg.add(url, doc)
    pick, _, _ = find(make_engine, widget(language=language, files=files), registries=reg)
    assert pick["package"] == {"version": "1.5.0", "status": "published", **expected}


def test_a_build_from_another_commit_is_a_warning(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://registry.npmjs.org/widget/1.5.0",
        {"repository": "github:acme/widget", "gitHead": "e" * 40},
    )
    repo = widget(language="JavaScript", files={"package.json": '{"name": "widget", "version": "1.5.0"}'})
    pick, _, _ = find(make_engine, repo, registries=reg)
    assert pick["package"]["status"] == "published" and pick["package"]["same_commit"] is False
    assert any(
        "built from a different commit than the pinned revision" in r for r in pick["integration_risks"]
    )


@pytest.mark.parametrize(
    ("language", "files", "detail"),
    [
        (
            "JavaScript",
            {"package.json": '{"name": "root", "private": true}'},
            "the root package.json is private",
        ),
        ("Rust", {"Cargo.toml": '[workspace]\nmembers = ["a", "b"]\n'}, "Cargo.toml is a workspace root"),
        ("Python", {}, "no pyproject.toml or setup.cfg or setup.py at the root"),
        ("Go", {"go.mod": "module github.com/acme/widget\n"}, None),  # checked below: no Go version
    ],
)
def test_what_cannot_be_looked_up_is_unknown_with_the_reason(
    make_engine: Callable[..., Engine], language: str, files: dict[str, str], detail: str | None
) -> None:
    repo = widget(language=language, files=files)
    if language == "Go":
        repo.release_tags = [("v1.5", False)]  # a release, but not a version Go can resolve
        detail = "v1.5 is not a version Go module proxy can look up"
    pick, _, _ = find(make_engine, repo)
    assert pick["package"]["status"] == "unknown"
    assert pick["package"]["detail"].startswith(detail)


def test_a_language_without_a_registry_check_is_unsupported(make_engine: Callable[..., Engine]) -> None:
    pick, _, _ = find(make_engine, widget(language="C++"))
    assert pick["package"] == {
        "status": "unsupported",
        "detail": "a C++ project with no PyPI, npm, crates.io, Go module proxy, RubyGems, Packagist, Maven "
        "Central, NuGet manifest at its root",
    }


def test_registry_failures_leave_the_package_unknown_and_the_decision_alone(
    make_engine: Callable[..., Engine],
) -> None:
    repo = widget(files={"pyproject.toml": PYPROJECT})
    _, baseline, _ = find(make_engine, repo)

    failing = FakeRegistries()
    failing.add("https://pypi.org/pypi/acme-widget/1.5.0/json", {"message": "boom"}, status=500)
    pick, out, _ = find(make_engine, repo, registries=failing)
    assert pick["package"]["status"] == "unknown"
    assert pick["package"]["detail"] == "PyPI could not be checked: pypi.org returned HTTP 500"
    assert (out["verdict"], out["evidence"]) == (baseline["verdict"], baseline["evidence"])

    redirected = FakeRegistries()
    redirected.add("https://pypi.org/pypi/acme-widget/1.5.0/json", "https://evil.example/x", status=302)
    pick, _, _ = find(make_engine, repo, registries=redirected)
    assert pick["package"]["detail"].endswith("pypi.org redirected to another host")
    assert all(r.url.host == "pypi.org" for r in redirected.requests)


def test_a_slow_registry_is_cut_off_and_says_so(
    make_engine: Callable[..., Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "PACKAGE_CHECK_MIN_S", 0.2)
    monkeypatch.setattr(engine_module, "PACKAGE_CHECK_MAX_S", 0.2)
    reg = FakeRegistries()
    reg.delay_s = 5
    _pypi(reg)
    pick, out, _ = find(make_engine, widget(files={"pyproject.toml": PYPROJECT}), registries=reg)
    assert pick["package"] == {"status": "unknown", "detail": "registry not checked within the time budget"}
    assert out["elapsed_ms"] < 3000


# --------------------------------------------------------------------------- helpers and settings


@pytest.mark.parametrize(
    ("link", "key"),
    [
        ("https://github.com/Acme/Widget", "acme/widget"),
        ("git+https://github.com/acme/widget.git", "acme/widget"),
        ("git@github.com:acme/widget.git", "acme/widget"),
        ("github:acme/widget", "acme/widget"),
        ("acme/widget", "acme/widget"),  # npm's shorthand
        ("https://github.com/acme/widget/issues", "acme/widget"),
        ("https://evil.example/github.com/acme/widget", None),  # github.com in the path is not GitHub
        ("https://github.com.evil.example/acme/widget", None),
        ("https://gitlab.com/acme/widget", None),
        (None, None),
    ],
)
def test_github_links_are_recognised_only_on_github(link: object, key: str | None) -> None:
    assert github_key(link) == key


def test_a_project_in_another_language_is_checked_by_its_root_manifest() -> None:
    bindings = make_repo("acme/fastparse", language="C++")
    assert choose_registry(bindings, ["CMakeLists.txt", "pyproject.toml"]) == (PYPI, ["pyproject.toml"])
    assert choose_registry(bindings, ["CMakeLists.txt"]) == (None, [])


def test_the_registry_check_can_be_turned_off() -> None:
    settings = Settings.from_mapping({"packages": {"check": False}})
    assert settings.packages.check is False
    [source] = build_sources(settings, Store(":memory:"))
    assert isinstance(source, GitHubSource) and source.packages is None
    run(source.aclose())
    [source] = build_sources(Settings.from_mapping({}), Store(":memory:"))
    assert isinstance(source, GitHubSource) and source.packages is not None
    run(source.aclose())


def test_the_cli_shows_what_is_pinned_and_both_ways_to_take_it(capsys: pytest.CaptureFixture[str]) -> None:
    _print_find(
        {
            "verdict": "reuse",
            "build_yourself": {"reason": "one project covers this task"},
            "picks": [
                {
                    "id": "pkg:github/acme/widget@v1.5.0",
                    "scores": {"total": 0.8},
                    "summary": "Parse widget files",
                    "why": [],
                    "version": "v1.5.0",
                    "version_kind": "release",
                    "get": "git clone --depth 1 --branch v1.5.0 https://github.com/acme/widget.git",
                    "package": {
                        "registry": "npm",
                        "name": "widget",
                        "version": "1.5.0",
                        "status": "published",
                        "install": "npm install widget@1.5.0",
                        "same_commit": True,
                    },
                }
            ],
            "considered": 1,
            "relevance_judge": "lexical",
            "elapsed_ms": 10,
            "recommendation_id": "rec_example",
        }
    )
    output = capsys.readouterr().out
    assert "pinned: release v1.5.0" in output
    assert (
        "package: npm install widget@1.5.0  (npm widget 1.5.0, published); built from the pinned commit"
        in output
    )
