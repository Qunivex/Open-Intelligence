"""Registry packages beyond the simple case: packages published from a subdirectory (monorepos),
Maven and NuGet specifics, and the build attestations registries hold. Offline: fake GitHub API and
fake registries."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import pytest

from conftest import FakeGitHub, FakeRegistries, make_repo, run, sha_for
from reuse_layer.adapters.cli import _package_line
from reuse_layer.core.engine import Engine
from reuse_layer.core.models import Pin
from reuse_layer.sources.github import GitHubSource, _PinnedFiles
from reuse_layer.sources.packages import _xml, fulcio_extensions, split_tag
from test_versions_and_packages import PIN, find, widget

# --------------------------------------------------------------------------- tags


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v1.2.3", ("", "1.2.3")),
        ("1.2.3", ("", "1.2.3")),
        ("@acme/core@1.2.3", ("@acme/core", "1.2.3")),
        ("core-v1.2", ("core", "1.2")),
        ("sub/v1.2.3", ("sub", "1.2.3")),
        ("exporters/otlp/v1.30.0", ("exporters/otlp", "1.30.0")),
        ("jackson-databind-2.17.0", ("jackson-databind", "2.17.0")),
        ("web3-1.0.0", ("web3", "1.0.0")),  # digits inside the name stay in the name
        ("py2neo-2021.1", ("py2neo", "2021.1")),
        ("serde_json-v1.0.120", ("serde_json", "1.0.120")),
        ("v2.0.0-rc.1", ("", "2.0.0-rc.1")),
        ("go1.21.0", ("go", "1.21.0")),
        ("nightly", None),
    ],
)
def test_a_tag_splits_into_its_package_and_version(tag: str, expected: tuple[str, str] | None) -> None:
    assert split_tag(tag) == expected


# --------------------------------------------------------------------------- monorepos


def _npm(reg: FakeRegistries, spec: str, repository: str = "https://github.com/acme/widget") -> None:
    reg.add(f"https://registry.npmjs.org/{spec}", {"repository": {"url": repository}})


def test_a_workspace_package_is_found_in_its_subdirectory(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    _npm(reg, "@acme%2Fwidget/1.5.0")
    files = {
        "package.json": '{"name": "root", "private": true, "workspaces": ["packages/*"]}',
        "packages/cli/package.json": '{"name": "@acme/widget-cli", "version": "0.9.0"}',
        "packages/widget/package.json": '{"name": "@acme/widget", "version": "1.5.0"}',
        "examples/widget/package.json": '{"name": "widget", "version": "1.5.0"}',  # never a package
        "packages/widget/README.md": "# widget",
    }
    pick, _, gh = find(make_engine, widget(language="TypeScript", files=files), registries=reg)
    package = pick["package"]
    assert (package["status"], package["name"], package["path"]) == (
        "published",
        "@acme/widget",
        "packages/widget",
    )
    assert package["install"] == "npm install @acme/widget@1.5.0"
    assert set(gh.contents_refs) == {PIN}  # the tree and every manifest are read at the pinned commit


def test_the_pinned_tag_decides_which_workspace_package_is_checked(
    make_engine: Callable[..., Engine],
) -> None:
    reg = FakeRegistries()
    _npm(reg, "@acme%2Fwidget-cli/2.0.0")
    files = {
        "package.json": '{"name": "root", "private": true}',
        "packages/cli/package.json": '{"name": "@acme/widget-cli"}',
        "packages/widget/package.json": '{"name": "@acme/widget"}',
    }
    repo = widget(language="TypeScript", files=files, release_tags=[("@acme/widget-cli@2.0.0", False)])
    pick, _, _ = find(make_engine, repo, policy="all", registries=reg)
    package = pick["package"]
    assert (package["name"], package["version"], package["path"]) == (
        "@acme/widget-cli",
        "2.0.0",
        "packages/cli",
    )
    assert package["status"] == "published"


def test_a_cargo_workspace_member_inherits_the_workspace_version(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://crates.io/api/v1/crates/widget/0.4.0",
        {"version": {"num": "0.4.0", "repository": "https://github.com/acme/widget", "trustpub_data": None}},
    )
    files = {
        "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n[workspace.package]\nversion = "0.4.0"\n',
        "crates/widget/Cargo.toml": '[package]\nname = "widget"\nversion.workspace = true\n',
        "crates/widget-macros/Cargo.toml": '[package]\nname = "widget-macros"\nversion.workspace = true\n',
    }
    pick, _, _ = find(make_engine, widget(language="Rust", files=files, releases=0), registries=reg)
    package = pick["package"]
    assert (package["status"], package["version"], package["path"]) == ("published", "0.4.0", "crates/widget")
    assert package["attested"] is False  # crates.io records trusted publishing, and has none here


def test_a_go_module_in_a_subdirectory_uses_its_prefixed_tag(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://proxy.golang.org/github.com/acme/widget/sub/@v/v1.5.0.info",
        {"Version": "v1.5.0", "Origin": {"URL": "https://github.com/acme/widget"}},
    )
    files = {"go.mod": "module github.com/acme/widget\n", "sub/go.mod": "module github.com/acme/widget/sub\n"}
    repo = widget(language="Go", files=files, release_tags=[("sub/v1.5.0", False)])
    pick, _, _ = find(make_engine, repo, policy="all", registries=reg)
    package = pick["package"]
    assert (package["status"], package["name"], package["version"], package["path"]) == (
        "published",
        "github.com/acme/widget/sub",
        "v1.5.0",
        "sub",
    )


def test_many_packages_and_none_named_like_the_repository_is_unknown(
    make_engine: Callable[..., Engine],
) -> None:
    files = {
        "package.json": '{"private": true}',
        "packages/alpha/package.json": '{"name": "alpha"}',
        "packages/beta/package.json": '{"name": "beta"}',
    }
    pick, _, _ = find(make_engine, widget(language="JavaScript", files=files))
    assert pick["package"] == {
        "status": "unknown",
        "registry": "npm",
        "detail": "the root package.json is private (a workspace root), and none of the 2 manifests in its "
        "subdirectories is named after the repository or its pinned tag",
    }


def test_the_only_package_in_a_subdirectory_is_used(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://pypi.org/pypi/pywidget/1.5.0/json",
        {"info": {"project_urls": {"Source": "https://github.com/acme/widget"}}},
    )
    files = {"CMakeLists.txt": "", "python/pyproject.toml": '[project]\nname = "pywidget"\n'}
    pick, _, _ = find(make_engine, widget(files=files), registries=reg)
    assert (pick["package"]["status"], pick["package"]["path"]) == ("published", "python")


def test_file_paths_are_read_one_segment_at_a_time(make_engine: Callable[..., Engine]) -> None:
    gh = FakeGitHub()
    gh.add(widget(files={"a/b.txt": "hello"}))
    source = make_engine(gh).sources["repository"]
    assert isinstance(source, GitHubSource)
    files = _PinnedFiles(source, "acme/widget", PIN)
    assert run(files.read("a/b.txt")) == "hello"
    assert run(files.read("a/../../secrets")) is None and run(files.read("/etc/passwd")) is None
    assert run(files.tree()) == ["a/b.txt"]


# --------------------------------------------------------------------------- Maven and NuGet


POM = '<project xmlns="http://maven.apache.org/POM/4.0.0">{}</project>'


def _pom(**parts: str) -> str:
    return POM.format("".join(f"<{k}>{v}</{k}>" for k, v in parts.items()))


def test_a_maven_module_inherits_its_scm_from_a_parent_in_the_same_group(
    make_engine: Callable[..., Engine],
) -> None:
    parent = "<parent><groupId>com.acme</groupId><artifactId>widget-parent</artifactId><version>1.5.0</version></parent>"
    reg = FakeRegistries()
    reg.add(
        "https://repo1.maven.org/maven2/com/acme/widget/1.5.0/widget-1.5.0.pom",
        POM.format(parent + "<artifactId>widget</artifactId><url>https://widget.example.org</url>"),
    )
    reg.add(
        "https://repo1.maven.org/maven2/com/acme/widget-parent/1.5.0/widget-parent-1.5.0.pom",
        _pom(groupId="com.acme", artifactId="widget-parent", scm="<url>https://github.com/acme/widget</url>"),
    )
    files = {
        "pom.xml": _pom(
            groupId="com.acme",
            artifactId="widget-parent",
            version="1.5.0",
            packaging="pom",
            modules="<module>widget</module><module>widget-bom</module>",
        ),
        "widget/pom.xml": POM.format(parent + "<artifactId>widget</artifactId>"),
        "widget-bom/pom.xml": POM.format(parent + "<artifactId>widget-bom</artifactId>"),
    }
    pick, _, _ = find(make_engine, widget(language="Java", files=files), registries=reg)
    package = pick["package"]
    assert (package["status"], package["name"], package["path"]) == ("published", "com.acme:widget", "widget")


def test_a_shared_parent_from_another_group_is_not_trusted_for_the_link(
    make_engine: Callable[..., Engine],
) -> None:
    parent = "<parent><groupId>org.example</groupId><artifactId>oss-parent</artifactId><version>9</version></parent>"
    reg = FakeRegistries()
    reg.add(
        "https://repo1.maven.org/maven2/com/acme/widget/1.5.0/widget-1.5.0.pom",
        POM.format(parent + "<groupId>com.acme</groupId><artifactId>widget</artifactId>"),
    )
    reg.add(  # names the right repository, but it is a parent shared by many projects: never fetched
        "https://repo1.maven.org/maven2/org/example/oss-parent/9/oss-parent-9.pom",
        _pom(scm="<url>https://github.com/acme/widget</url>"),
    )
    files = {"pom.xml": _pom(groupId="com.acme", artifactId="widget")}
    pick, _, _ = find(make_engine, widget(language="Java", files=files), registries=reg)
    assert pick["package"]["status"] == "not_linked"
    assert not any("oss-parent" in str(r.url) for r in reg.requests)


def test_gradle_coordinates_come_from_gradle_properties(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://repo1.maven.org/maven2/com/acme/widget-core/1.5.0/widget-core-1.5.0.pom",
        _pom(scm="<url>https://github.com/acme/widget/tree/main/widget-core</url>"),
    )
    files = {
        "build.gradle.kts": "plugins { }",
        "gradle.properties": "# shared by every module\nGROUP=com.acme\nVERSION_NAME=1.6.0-SNAPSHOT\n",
        "widget-core/gradle.properties": "POM_ARTIFACT_ID=widget-core\n",
        "widget-core/build.gradle.kts": "",
    }
    pick, _, _ = find(make_engine, widget(language="Kotlin", files=files), registries=reg)
    package = pick["package"]
    assert (package["status"], package["name"], package["path"]) == (
        "published",
        "com.acme:widget-core",
        "widget-core",
    )


def test_a_gradle_build_that_sets_its_coordinates_in_code_is_unknown(
    make_engine: Callable[..., Engine],
) -> None:
    files = {"build.gradle.kts": 'group = "com.acme"', "settings.gradle.kts": ""}
    pick, _, _ = find(make_engine, widget(language="Kotlin", files=files))
    assert pick["package"]["status"] == "unknown"
    assert pick["package"]["detail"].startswith("build.gradle.kts is a Gradle build script")


def test_an_unlisted_nuget_version_is_not_offered(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://api.nuget.org/v3-flatcontainer/widget/1.5.0/widget.nuspec",
        "<package><metadata><id>Widget</id><projectUrl>https://github.com/acme/widget</projectUrl>"
        "</metadata></package>",
    )
    reg.add("https://api.nuget.org/v3/registration5-gz-semver2/widget/1.5.0.json", {"listed": False})
    files = {
        "Widget.csproj": "<Project><PropertyGroup><AssemblyName>Widget</AssemblyName></PropertyGroup></Project>"
    }
    pick, _, _ = find(make_engine, widget(language="C#", files=files), registries=reg)
    assert pick["package"]["status"] == "yanked"
    assert pick["package"]["detail"] == "NuGet withdrew (unlisted) Widget 1.5.0: do not install it"


def test_xml_that_declares_a_dtd_or_entity_is_never_parsed(make_engine: Callable[..., Engine]) -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaa">]><project><artifactId>&a;</artifactId></project>'
    assert _xml(bomb) is None
    assert _xml("<project><artifactId>x</artifactId></project>") is not None

    reg = FakeRegistries()
    reg.add("https://repo1.maven.org/maven2/com/acme/widget/1.5.0/widget-1.5.0.pom", bomb)
    files = {"pom.xml": _pom(groupId="com.acme", artifactId="widget")}
    pick, _, _ = find(make_engine, widget(language="Java", files=files), registries=reg)
    assert (
        pick["package"]["detail"]
        == "Maven Central could not be checked: Maven Central returned a malformed POM"
    )


# --------------------------------------------------------------------------- build attestations


def _npm_provenance(repository: str, commit: str) -> dict[str, Any]:
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "externalParameters": {
                    "workflow": {"repository": repository, "path": ".github/workflows/r.yml"}
                },
                "resolvedDependencies": [
                    {"uri": f"git+{repository}@refs/tags/v1.5.0", "digest": {"gitCommit": commit}}
                ],
            }
        },
    }
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    return {
        "attestations": [
            {
                "predicateType": "https://github.com/npm/attestation/tree/main/specs/publish/v0.1",
                "bundle": {},
            },
            {
                "predicateType": "https://slsa.dev/provenance/v1",
                "bundle": {"dsseEnvelope": {"payload": payload}},
            },
        ]
    }


NPM_FILES = {"package.json": '{"name": "widget", "version": "1.5.0"}'}
NPM_ATTESTATIONS = "https://registry.npmjs.org/-/npm/v1/attestations/widget@1.5.0"


def test_npm_provenance_links_the_package_and_names_the_commit(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    # no repository link in the metadata at all: the attestation alone links the package
    reg.add("https://registry.npmjs.org/widget/1.5.0", {"dist": {"attestations": {"url": NPM_ATTESTATIONS}}})
    reg.add(NPM_ATTESTATIONS, _npm_provenance("https://github.com/acme/widget", PIN))
    pick, _, _ = find(make_engine, widget(language="JavaScript", files=NPM_FILES), registries=reg)
    package = pick["package"]
    assert (package["status"], package["attested"], package["same_commit"]) == ("published", True, True)


def test_an_attestation_naming_another_repository_is_not_linked(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://registry.npmjs.org/widget/1.5.0",
        {"repository": "github:acme/widget", "dist": {"attestations": {"url": NPM_ATTESTATIONS}}},
    )
    reg.add(NPM_ATTESTATIONS, _npm_provenance("https://github.com/squatter/widget", "e" * 40))
    pick, _, _ = find(make_engine, widget(language="JavaScript", files=NPM_FILES), registries=reg)
    package = pick["package"]
    assert package["status"] == "not_linked" and "install" not in package
    assert "build attestation for widget 1.5.0 names another repository" in package["detail"]


def test_an_unreadable_attestation_leaves_attested_unknown(make_engine: Callable[..., Engine]) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://registry.npmjs.org/widget/1.5.0",
        {"repository": "github:acme/widget", "dist": {"attestations": {"url": NPM_ATTESTATIONS}}},
    )
    bad = {"attestations": [{"predicateType": "https://slsa.dev/provenance/v1", "bundle": {}}]}
    reg.add(NPM_ATTESTATIONS, bad)
    pick, _, _ = find(make_engine, widget(language="JavaScript", files=NPM_FILES), registries=reg)
    assert pick["package"]["status"] == "published" and "attested" not in pick["package"]


PYPI_FILE = "acme_widget-1.5.0-py3-none-any.whl"
PYPI_PROVENANCE = f"https://pypi.org/integrity/acme-widget/1.5.0/{PYPI_FILE}/provenance"


@pytest.mark.parametrize(
    ("provenance", "attested"),
    [
        ({"attestation_bundles": [{"publisher": {"kind": "GitHub", "repository": "acme/widget"}}]}, True),
        (None, False),  # 404: PyPI holds no attestation for this file
    ],
)
def test_pypi_attestations_name_the_trusted_publisher(
    make_engine: Callable[..., Engine], provenance: dict[str, Any] | None, attested: bool
) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://pypi.org/pypi/acme-widget/1.5.0/json",
        {
            "info": {"project_urls": {"Source": "https://github.com/acme/widget"}},
            "urls": [{"filename": PYPI_FILE}],
        },
    )
    if provenance is not None:
        reg.add(PYPI_PROVENANCE, provenance)
    files = {"pyproject.toml": '[project]\nname = "acme_widget"\n'}
    pick, _, _ = find(make_engine, widget(files=files), registries=reg)
    assert (pick["package"]["status"], pick["package"]["attested"]) == ("published", attested)
    [asked] = [r for r in reg.requests if "/integrity/" in str(r.url)]
    assert asked.headers["accept"] == "application/vnd.pypi.integrity.v1+json"


def test_crates_trusted_publishing_from_another_repository_is_not_linked(
    make_engine: Callable[..., Engine],
) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://crates.io/api/v1/crates/widget/1.5.0",
        {
            "version": {
                "repository": "https://github.com/acme/widget",
                "trustpub_data": {"provider": "github", "repository": "squatter/widget", "sha": "e" * 40},
            }
        },
    )
    files = {"Cargo.toml": '[package]\nname = "widget"\nversion = "1.5.0"\n'}
    pick, _, _ = find(make_engine, widget(language="Rust", files=files), registries=reg)
    assert pick["package"]["status"] == "not_linked"


def _tlv(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        return bytes([tag, n]) + body
    size = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(size)]) + size + body


def _oid(dotted: str) -> bytes:
    arcs = [int(a) for a in dotted.split(".")]
    out = bytearray([arcs[0] * 40 + arcs[1]])
    for arc in arcs[2:]:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, bytes(out))


def fulcio_cert(repository: str, commit: str) -> bytes:
    """A DER certificate shaped like Fulcio's, with its source repository extensions."""

    def ext(oid: str, value: bytes) -> bytes:
        return _tlv(0x30, _oid(oid) + _tlv(0x04, value))

    extensions = _tlv(
        0xA3,
        _tlv(
            0x30,
            ext("2.5.29.17", _tlv(0x30, b"\x86" + bytes([180]) + b"u" * 180))  # a long (two-byte) length
            + ext("1.3.6.1.4.1.57264.1.12", _tlv(0x0C, repository.encode()))
            + ext("1.3.6.1.4.1.57264.1.13", _tlv(0x0C, commit.encode())),
        ),
    )
    empty = _tlv(0x30, b"")
    tbs = _tlv(0x30, _tlv(0xA0, _tlv(0x02, b"\x02")) + _tlv(0x02, b"\x01") + empty * 5 + extensions)
    return _tlv(0x30, tbs + empty + _tlv(0x03, b"\x00"))


def test_fulcio_extensions_are_read_from_a_signing_certificate() -> None:
    ext = fulcio_extensions(fulcio_cert("https://github.com/acme/widget", PIN))
    assert set(ext) == {"12", "13"}
    assert ext["12"].endswith(b"https://github.com/acme/widget")


def test_a_rubygems_attestation_names_the_repository_in_its_certificate(
    make_engine: Callable[..., Engine],
) -> None:
    reg = FakeRegistries()
    reg.add(
        "https://rubygems.org/api/v2/rubygems/widget/versions/1.5.0.json",
        {"source_code_uri": "https://github.com/acme/widget"},
    )
    cert = base64.b64encode(fulcio_cert("https://github.com/acme/widget", PIN)).decode()
    reg.add(
        "https://rubygems.org/api/v1/attestations/widget-1.5.0.json",
        [{"verificationMaterial": {"certificate": {"rawBytes": cert}}}],
    )
    files = {"widget.gemspec": 'Gem::Specification.new do |s|\n  s.name = "widget"\nend\n'}
    pick, _, _ = find(make_engine, widget(language="Ruby", files=files), registries=reg)
    package = pick["package"]
    assert (package["status"], package["attested"], package["same_commit"]) == ("published", True, True)


def test_attestation_commit_outranks_the_metadata_commit(make_engine: Callable[..., Engine]) -> None:
    """npm's gitHead is written by the publisher's tooling; the attestation is the registry's record."""
    reg = FakeRegistries()
    reg.add(
        "https://registry.npmjs.org/widget/1.5.0",
        {"gitHead": PIN, "dist": {"attestations": {"url": NPM_ATTESTATIONS}}},
    )
    reg.add(NPM_ATTESTATIONS, _npm_provenance("https://github.com/acme/widget", sha_for("acme/widget", "x")))
    pick, _, _ = find(make_engine, widget(language="JavaScript", files=NPM_FILES), registries=reg)
    assert pick["package"]["same_commit"] is False


def test_a_failed_search_is_reported_not_answered_with_the_root_package(
    make_engine: Callable[..., Engine],
) -> None:
    gh = FakeGitHub()
    files = {"go.mod": "module github.com/acme/widget\n", "sub/go.mod": "module github.com/acme/widget/sub\n"}
    gh.add(widget(language="Go", files=files, release_tags=[("sub/v1.5.0", False)]))
    gh.rate_limit_paths.add("/git/trees/")
    source = make_engine(gh).sources["repository"]
    assert isinstance(source, GitHubSource) and source.packages is not None
    pin = Pin(ref="sub/v1.5.0", kind="tag", sha=sha_for("acme/widget", "sub/v1.5.0"), signature="valid")
    repo = make_repo("acme/widget", language="Go")
    package = run(source.packages.check(repo, pin, _PinnedFiles(source, repo.full_name, pin.sha)))
    assert package.status == "unknown"
    assert package.detail == (
        "the pinned tag names sub, not the package in the root go.mod, and its subdirectories could not be "
        "listed (rate limited)"
    )


def test_the_cli_names_the_directory_and_the_attestation() -> None:
    line = _package_line(
        {
            "registry": "npm",
            "name": "@acme/core",
            "version": "2.0.0",
            "path": "packages/core",
            "status": "published",
            "install": "npm install @acme/core@2.0.0",
            "same_commit": True,
            "attested": True,
        }
    )
    assert line == (
        "npm install @acme/core@2.0.0  (npm @acme/core 2.0.0 from packages/core/, published); the registry's "
        "build attestation names this repository; built from the pinned commit"
    )
