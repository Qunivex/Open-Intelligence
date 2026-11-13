"""Is a pick also published, at its pinned version, on its language's official package registry?

A project found on GitHub can usually be taken two ways: from source at the pinned commit, or as the
package its maintainers publish to the language's registry. This module finds out whether the second
way exists for the exact version that was pinned, so the user can choose between them. It is
informational: it runs for the final picks only, after the decision, and is never a gate or a score.

Per pick:

1. Choose the registry from the repository's language: Python -> PyPI, JavaScript and TypeScript ->
   npm, Rust -> crates.io, Go -> the Go module proxy, Ruby -> RubyGems, PHP -> Packagist, Java,
   Kotlin, Scala and Groovy -> Maven Central, C#, F# and Visual Basic -> NuGet. For any other
   language, use the first of their manifests found at the repository root.
2. Find the package's manifest at the pinned commit. The root manifest is used when it describes a
   package. When the root is a workspace (a monorepo), has no manifest, or the pinned tag names
   another package (``@scope/core@1.2.0``, ``core-v1.2.0``, ``sub/v1.2.3``), the repository's file
   tree is searched for manifests in subdirectories, and the one named after the tag's package or
   the repository is used. Its package name, and a static version if it declares one, are read.
3. Ask the registry for that name at the pinned version: the version in the pinned tag when the tag
   is this package's, else the version the manifest declares. A branch pin has no tag.
4. Report ``published`` only when the registry links the package back to this repository: its
   metadata names it as the source, or a build attestation the registry holds does. Anyone can
   register a name, so a package that only shares it is ``not_linked``, and so is one whose
   attestation names another repository. When the registry records the commit it was built from,
   ``same_commit`` says whether it is the pinned one.

Build attestations (``attested``): npm provenance, PyPI attestations (PEP 740), crates.io trusted
publishing and RubyGems Sigstore attestations each record, at publish time, the GitHub repository
whose CI workflow built and published the version. They are read here, not re-verified: no signature
is checked, so this is the registry's record (npm and PyPI verify attestations on upload), not an
independent proof. Maven Central's optional Sigstore bundles are not read, because Central does not
verify them; NuGet, Packagist and the Go proxy keep no such record.

Metadata links are claims, written by the repository owner (the manifest) and by the publisher (the
registry metadata). Two claims that agree are good evidence, not proof; an attestation and
``same_commit`` are stronger.

Security:

* Only the registry hosts in ``HOSTS`` are contacted, over HTTPS, with no credentials and no data
  about the caller. Redirects are followed only within the same host.
* Package names come from a manifest the repository owner wrote: each is checked against the
  registry's naming rules before it is put in a URL, and URL-quoted.
* XML (Maven POMs, NuGet package specs and project files) is parsed only after refusing any document
  that declares a DTD or an entity.
* No text a registry returns is passed on. A result carries only the name, version and directory
  that were checked and fixed wording.
"""

from __future__ import annotations

import asyncio
import base64
import configparser
import json
import logging
import posixpath
import re
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlsplit

import httpx

from reuse_layer import __version__
from reuse_layer.core.models import Package, PackageStatus, Pin, Repo
from reuse_layer.core.store import Store
from reuse_layer.sources.base import RateLimited, SourceError

log = logging.getLogger(__name__)

HOSTS = frozenset(
    {
        "pypi.org",
        "registry.npmjs.org",
        "crates.io",
        "proxy.golang.org",
        "rubygems.org",
        "repo.packagist.org",
        "repo1.maven.org",
        "api.nuget.org",
    }
)
# crates.io asks API clients to identify themselves. This names the tool, never the person using it.
USER_AGENT = f"reuse-layer/{__version__} (+https://github.com/Qunivex/Open-Intelligence)"
HOUR = 3600
JSON = "application/json"
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,63}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class RegistryError(SourceError):
    """A registry answered with an error, refused, or did not answer in time."""


class _NotChecked(Exception):
    """The manifest was read, and says why no registry lookup applies (for example a Gradle script)."""


class _Workspace(Exception):
    """The manifest is a workspace root: its packages are in subdirectories.

    ``inherit`` holds what the members take from it (a Maven group, a Cargo workspace version).
    """

    def __init__(self, reason: str, inherit: Mapping[str, str | None] | None = None) -> None:
        super().__init__(reason)
        self.inherit = {k: v for k, v in (inherit or {}).items() if v}


class _Unknown(Exception):
    """The package could not be determined; the message says why."""


class _Unsearched(_Unknown):
    """The subdirectories could not be searched (rate limit, listing failed): a root manifest that
    does not fit the pinned tag is then no answer either."""


# --------------------------------------------------------------------------- HTTP


class RegistryClient:
    """GET from a fixed set of registry hosts. No credentials; same-host redirects only."""

    def __init__(
        self,
        *,
        budget_ms: int = 4000,
        store: Store | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_concurrency: int = 6,
    ) -> None:
        self._store = store
        self._sem = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(budget_ms / 1000),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, url: str, *, max_bytes: int = 4_000_000, accept: str = JSON) -> Any | None:
        """The JSON document at ``url``, or None when the registry has nothing there (404, 410)."""
        text = await self._get(url, max_bytes, accept)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RegistryError(f"{urlsplit(url).hostname} returned a malformed response") from exc

    async def get_text(self, url: str, *, max_bytes: int = 1_000_000) -> str | None:
        """The text (XML) document at ``url``, or None when the registry has nothing there."""
        return await self._get(url, max_bytes, "application/xml, text/xml;q=0.9, */*;q=0.5")

    async def _get(self, url: str, max_bytes: int, accept: str) -> str | None:
        host = urlsplit(url).hostname
        if not url.startswith("https://") or host not in HOSTS:
            raise ValueError("not a registry URL")
        key = f"REGISTRY GET {url} {accept}"
        cached = self._store.cache_get(key) if self._store else None
        if cached is not None and time.time() - cached.fetched_at < (
            HOUR if cached.status == 404 else 6 * HOUR
        ):
            return None if cached.status == 404 else cached.body

        current = url
        for _ in range(3):  # at most two redirects, never off the host
            async with self._sem:
                try:
                    async with self._client.stream("GET", current, headers={"Accept": accept}) as resp:
                        status = resp.status_code
                        location = resp.headers.get("location", "")
                        body = await _read_capped(resp, max_bytes) if status == 200 else b""
                except httpx.TimeoutException as exc:
                    raise RegistryError(f"{host} did not answer in time") from exc
                except httpx.HTTPError as exc:
                    raise RegistryError(f"{host} request failed ({type(exc).__name__})") from exc
            if status in (301, 302, 303, 307, 308):
                current = urljoin(current, location)
                target = urlsplit(current)
                if target.scheme != "https" or target.hostname != host:
                    raise RegistryError(f"{host} redirected to another host")
                continue
            break
        else:
            raise RegistryError(f"{host} redirected too many times")

        log.debug("registry response: GET %s status=%d bytes=%d", current, status, len(body))
        if status in (404, 410):
            if self._store is not None:
                self._store.cache_put(key, 404, None, "", None)
            return None
        if status == 429:
            raise RegistryError(f"{host} rate limit reached")
        if status != 200:
            raise RegistryError(f"{host} returned HTTP {status}")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RegistryError(f"{host} returned a malformed response") from exc
        if self._store is not None:
            self._store.cache_put(key, 200, None, text, None)
        return text


async def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise RegistryError(f"{resp.url.host} response is larger than {max_bytes // 1_000_000} MB")
        chunks.append(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------------- shared helpers


def github_key(link: object) -> str | None:
    """``owner/name`` (lower-cased) of a GitHub repository link in any form registries use, else None.

    Accepts https/git/ssh URLs on github.com, ``git+`` and Maven ``scm:git:`` prefixes,
    ``git@github.com:owner/name``, npm's ``github:owner/name`` and its bare ``owner/name`` shorthand.
    Any other host is None, so a link that only mentions github.com in its path never counts.
    """
    if not isinstance(link, str):
        return None
    s = re.sub(r"^(?:scm:(?:git:)?|git\+)", "", link.strip(), flags=re.IGNORECASE)
    m = re.match(r"^(?:github:|git@github\.com:)([^/\s]+)/([^/\s#?]+)", s, re.IGNORECASE)
    if m:
        owner, name = m.groups()
    elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+", s):
        owner, name = s.split("/")
    else:
        u = urlsplit(s)
        if u.scheme.lower() not in ("https", "http", "git", "ssh") or (u.hostname or "").lower() not in (
            "github.com",
            "www.github.com",
        ):
            return None
        parts = [p for p in u.path.split("/") if p]
        if len(parts) < 2:
            return None
        owner, name = parts[0], parts[1]
    if name.lower().endswith(".git"):
        name = name[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner) or not re.fullmatch(
        r"[A-Za-z0-9._-]{1,100}", name
    ):
        return None
    return f"{owner}/{name}".lower()


_TAG_VERSION = re.compile(r"\d+(?:\.\d+)*(?:[-.+]?[0-9A-Za-z][0-9A-Za-z.+-]*)?")


def split_tag(tag: str) -> tuple[str, str] | None:
    """A tag as (package prefix, version), or None when it names no version.

    ``v1.2.3`` -> ("", "1.2.3"); ``@scope/core@1.2.3`` -> ("@scope/core", "1.2.3"); ``core-v1.2`` ->
    ("core", "1.2"); ``sub/v1.2.3`` -> ("sub", "1.2.3"); ``log4j-2.17.0`` -> ("log4j", "2.17.0");
    ``go1.21.0`` -> ("go", "1.21.0"). The version starts after a separator (``-@_/``) and an optional
    ``v``, so digits inside the prefix (``web3``, ``py2neo``) stay in the prefix.
    """
    for m in re.finditer(r"(?:^|[-@_/])[vV]?(?=\d)", tag):
        if _TAG_VERSION.fullmatch(tag[m.end() :]):
            return tag[: m.start()], tag[m.end() :]
    first = re.search(r"\d", tag)
    if first and _TAG_VERSION.fullmatch(tag[first.start() :]):
        return tag[: first.start()], tag[first.start() :]
    return None


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sha(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and _SHA.match(value) else None


_DTD = re.compile(r"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def _xml(text: str) -> ET.Element | None:
    """The document's root with namespaces dropped from tag names; None when malformed or when it
    declares a DTD or an entity (refused before parsing, so no entity is ever expanded)."""
    if _DTD.search(text):
        return None
    try:
        root = ET.fromstring(text.lstrip("﻿"))  # noqa: S314 - DTDs and entities are refused above
    except (ET.ParseError, ValueError):
        return None
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _child(el: ET.Element | None, path: str) -> str | None:
    found = el.find(path) if el is not None else None
    return _str(found.text) if found is not None else None


def _literal(value: str | None) -> str | None:
    """A value as written, or None when it is computed by the build (``${x}``, ``$(X)``)."""
    return None if value is None or "$" in value else value


# --------------------------------------------------------------------------- build attestations


@dataclass(frozen=True)
class Provenance:
    """Where a registry's attestation says a version was built."""

    repo: str | None  # ``owner/name`` on GitHub, lower-cased; None for a repository elsewhere
    commit: str | None = None


def _slsa_provenance(doc: object) -> Provenance | None:
    """The source repository and commit in npm's SLSA provenance attestation (v1 or v0.2)."""
    for att in _dict(doc).get("attestations") or []:
        att = _dict(att)
        if not str(att.get("predicateType", "")).startswith("https://slsa.dev/provenance/"):
            continue
        payload = _dict(_dict(att.get("bundle")).get("dsseEnvelope")).get("payload")
        try:
            if not isinstance(payload, str):
                raise TypeError("no payload")
            statement = json.loads(base64.b64decode(payload, validate=True))
        except (TypeError, ValueError) as exc:
            raise RegistryError("npm returned a provenance attestation that could not be read") from exc
        predicate = _dict(_dict(statement).get("predicate"))
        build = _dict(predicate.get("buildDefinition"))
        if build:  # SLSA v1
            workflow = _dict(_dict(build.get("externalParameters")).get("workflow"))
            repo: object = workflow.get("repository")
            deps = build.get("resolvedDependencies")
            digests = [_dict(_dict(d).get("digest")) for d in deps] if isinstance(deps, list) else []
            commit = next((c for d in digests if (c := _sha(d.get("gitCommit")))), None)
        else:  # SLSA v0.2: git+https://github.com/owner/name@refs/tags/v1.2.3
            source = _dict(_dict(predicate.get("invocation")).get("configSource"))
            uri = source.get("uri")
            repo = uri.split("@", 1)[0] if isinstance(uri, str) else None
            commit = _sha(_dict(source.get("digest")).get("sha1"))
        if not repo:
            raise RegistryError("npm returned a provenance attestation without a source repository")
        return Provenance(github_key(repo), commit)
    return None


def _pypi_provenance(doc: object) -> Provenance | None:
    """The Trusted Publisher named by PyPI's provenance object (PEP 740)."""
    bundles = _dict(doc).get("attestation_bundles")
    if not isinstance(bundles, list) or not bundles:
        return None
    publisher = _dict(_dict(bundles[0]).get("publisher"))
    if not publisher:
        raise RegistryError("PyPI returned provenance without a publisher")
    on_github = str(publisher.get("kind", "")).lower() == "github"
    return Provenance(github_key(publisher.get("repository")) if on_github else None)


# Sigstore signing certificates carry the CI identity in Fulcio's X.509 extensions,
# 1.3.6.1.4.1.57264.1.<n>: 12 = source repository URI and 13 = its commit (DER strings), or the
# older 5 = GitHub workflow repository and 3 = its commit (raw strings).
_FULCIO = "1.3.6.1.4.1.57264.1."


def _der(buf: bytes, i: int) -> tuple[int, int, int]:
    """(tag, value start, value end) of the DER element at ``i``."""
    tag, size = buf[i], buf[i + 1]
    i += 2
    if size & 0x80:
        n = size & 0x7F
        if not 0 < n <= 4:
            raise ValueError("unsupported DER length")
        size = int.from_bytes(buf[i : i + n], "big")
        i += n
    if i + size > len(buf):
        raise ValueError("truncated DER")
    return tag, i, i + size


def _der_children(buf: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out = []
    while start < end:
        out.append(_der(buf, start))
        start = out[-1][2]
    return out


def _oid(raw: bytes) -> str:
    parts = [raw[0] // 40, raw[0] % 40]
    n = 0
    for b in raw[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(n)
            n = 0
    return ".".join(map(str, parts))


def fulcio_extensions(cert: bytes) -> dict[str, bytes]:
    """The Fulcio extensions of a DER X.509 certificate, keyed by the last OID arc.

    A small walk over Certificate > tbsCertificate > [3] extensions. It reads what the certificate
    says; it verifies nothing.
    """
    _, s, _e = _der(cert, 0)
    _, ts, te = _der(cert, s)
    for tag, cs, _ce in _der_children(cert, ts, te):
        if tag != 0xA3:
            continue
        _, xs, xe = _der(cert, cs)
        out: dict[str, bytes] = {}
        for _, es, ee in _der_children(cert, xs, xe):
            parts = _der_children(cert, es, ee)
            if len(parts) < 2 or parts[0][0] != 0x06 or parts[-1][0] != 0x04:
                continue
            oid = _oid(cert[parts[0][1] : parts[0][2]])
            if oid.startswith(_FULCIO):
                out[oid[len(_FULCIO) :]] = cert[parts[-1][1] : parts[-1][2]]
        return out
    return {}


def _fulcio_text(ext: Mapping[str, bytes], der_arc: str, raw_arc: str) -> str | None:
    if (value := ext.get(der_arc)) is not None:
        tag, s, e = _der(value, 0)
        return value[s:e].decode("utf-8") if tag == 0x0C else None
    value = ext.get(raw_arc)
    return value.decode("utf-8") if value is not None else None


def _sigstore_provenance(doc: object) -> Provenance | None:
    """The source repository named in the signing certificate of a RubyGems Sigstore attestation."""
    bundles = doc if isinstance(doc, list) else []
    if not bundles:
        return None
    material = _dict(_dict(bundles[0]).get("verificationMaterial"))
    raw = _dict(material.get("certificate")).get("rawBytes")
    chain = _dict(material.get("x509CertificateChain")).get("certificates")
    if raw is None and isinstance(chain, list) and chain:
        raw = _dict(chain[0]).get("rawBytes")
    try:
        if not isinstance(raw, str):
            raise TypeError("no certificate")
        ext = fulcio_extensions(base64.b64decode(raw, validate=True))
        repo, commit = _fulcio_text(ext, "12", "5"), _fulcio_text(ext, "13", "3")
    except (TypeError, ValueError, IndexError) as exc:
        raise RegistryError("RubyGems returned an attestation that could not be read") from exc
    if repo is None:
        raise RegistryError("RubyGems returned an attestation without a source repository")
    return Provenance(github_key(repo), _sha(commit))


# --------------------------------------------------------------------------- registries


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str | None  # a static version the manifest declares; None when it is computed
    file: str  # its path in the repository


def _manifest(name: str | None, version: str | None, file: str) -> Manifest | None:
    """A manifest with a name; a version is kept only when it is a literal version string."""
    if not name:
        return None
    return Manifest(name, version if version and _VERSION.match(version) else None, file)


@dataclass(frozen=True)
class Found:
    """What a registry says about one package at one version."""

    links: tuple[object, ...]  # source and home links in the registry's metadata
    yanked: bool = False
    deprecated: bool = False
    commit: str | None = None  # the commit the registry says the version was built from
    attestation: Provenance | None = None  # what the registry's build attestation names
    attestation_checked: bool = False  # the registry was asked for one (None above: it has none)


@dataclass(frozen=True)
class Registry:
    id: str
    label: str
    files: tuple[str, ...]  # manifest file names, most specific first
    name_rule: re.Pattern[str]
    languages: frozenset[str] = field(default_factory=frozenset)
    withdrawn: str = "yanked"  # what the registry calls a version it withdrew

    def matches(self, base: str) -> bool:
        """Is a file with this name one of this registry's manifests?"""
        return base in self.files

    def present(self, names: Sequence[str], repo: Repo) -> list[str]:
        """The manifests among the repository root's file names, in the order to read them."""
        return [f for f in self.files if f in names]

    def order(self, base: str) -> int:
        """Reading order of manifests in one directory."""
        return self.files.index(base) if base in self.files else len(self.files)

    def parse(self, file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        """The manifest's package. Raises ``_Workspace`` for a workspace root, ``_NotChecked`` when
        the file can say no package's name. ``inherit``: what a workspace root passes down."""
        raise NotImplementedError

    def versions(self, pin: Pin, manifest: Manifest, tagged: str | None) -> list[str]:
        """Versions to ask for, best first: the pinned tag's (when it is this package's tag), then
        the manifest's own."""
        out: list[str] = []
        for v in (tagged, manifest.version):
            if v and _VERSION.match(v) and self.released(v) and v not in out:
                out.append(v)
        return out

    def released(self, version: str) -> bool:
        return True

    async def lookup(self, client: RegistryClient, name: str, version: str) -> Found | None:
        raise NotImplementedError

    def install(self, name: str, version: str) -> str:
        raise NotImplementedError


class PyPI(Registry):
    def parse(self, file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        name: object = None
        version: object = None
        base = posixpath.basename(file)
        if base == "pyproject.toml":
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                return None
            project = _dict(data.get("project"))
            name = project.get("name")
            dynamic = project.get("dynamic")
            version = None if isinstance(dynamic, list) and "version" in dynamic else project.get("version")
            if not _str(name):  # Poetry before PEP 621
                poetry = _dict(_dict(data.get("tool")).get("poetry"))
                name, version = poetry.get("name"), poetry.get("version")
        elif base == "setup.cfg":
            cp = configparser.ConfigParser(interpolation=None)
            try:
                cp.read_string(text)
            except configparser.Error:
                return None
            name = cp.get("metadata", "name", fallback=None)
            version = cp.get("metadata", "version", fallback=None)  # "attr: ..." fails _VERSION below
        else:  # setup.py is code: only literal arguments are read
            n = re.search(r"""\bname\s*=\s*["']([^"']+)["']""", text)
            v = re.search(r"""\bversion\s*=\s*["']([0-9][^"']*)["']""", text)
            name, version = (n.group(1) if n else None), (v.group(1) if v else None)
        return _manifest(_str(name), _str(version), file)

    async def lookup(self, client: RegistryClient, name: str, version: str) -> Found | None:
        project = quote(_pep503(name))
        data = await client.get_json(f"https://pypi.org/pypi/{project}/{quote(version)}/json")
        if not isinstance(data, dict):
            return None
        info = _dict(data.get("info"))
        links = [*_dict(info.get("project_urls")).values(), info.get("home_page"), info.get("download_url")]
        # PEP 740: provenance is kept per file. Every file of a release is uploaded by the same
        # publisher, so the first one answers for the version.
        urls = data.get("urls")
        files = [_dict(u).get("filename") for u in urls] if isinstance(urls, list) else []
        filename = next((f for f in files if isinstance(f, str) and _FILENAME.match(f)), None)
        attestation, checked = None, False
        if filename:
            try:
                attestation = _pypi_provenance(
                    await client.get_json(
                        f"https://pypi.org/integrity/{project}/{quote(version)}/{quote(filename)}/provenance",
                        accept="application/vnd.pypi.integrity.v1+json",
                    )
                )
                checked = True
            except RegistryError as exc:
                log.info("PyPI provenance not read for %s %s: %s", name, version, exc)
        return Found(
            links=tuple(links),
            yanked=bool(info.get("yanked")),
            attestation=attestation,
            attestation_checked=checked,
        )

    def install(self, name: str, version: str) -> str:
        return f"pip install {name}=={version}"


_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,250}$")


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class Npm(Registry):
    def parse(self, file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("private") is True:
            if posixpath.dirname(file):
                raise _NotChecked(f"{file} is private")
            raise _Workspace(f"the root {file} is private (a workspace root)")
        return _manifest(_str(data.get("name")), _str(data.get("version")), file)

    async def lookup(self, client: RegistryClient, name: str, version: str) -> Found | None:
        spec = f"{quote(name, safe='@')}/{quote(version)}"
        data = await client.get_json(f"https://registry.npmjs.org/{spec}")
        if not isinstance(data, dict):
            return None
        repository = data.get("repository")
        links = [
            repository if isinstance(repository, str) else _dict(repository).get("url"),
            data.get("homepage"),
            _dict(data.get("bugs")).get("url"),
        ]
        attestation, checked = None, True  # no dist.attestations: npm holds none for this version
        if _dict(_dict(data.get("dist")).get("attestations")):
            try:
                attestation = _slsa_provenance(
                    await client.get_json(
                        f"https://registry.npmjs.org/-/npm/v1/attestations/{quote(name, safe='@')}@{quote(version)}"
                    )
                )
            except RegistryError as exc:
                log.info("npm provenance not read for %s %s: %s", name, version, exc)
                checked = False
        return Found(
            links=tuple(links),
            deprecated=bool(data.get("deprecated")),
            commit=_str(data.get("gitHead")),
            attestation=attestation,
            attestation_checked=checked,
        )

    def install(self, name: str, version: str) -> str:
        return f"npm install {name}@{version}"


class Crates(Registry):
    def parse(self, file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        package = _dict(data.get("package"))
        workspace = _dict(data.get("workspace"))
        if not package and "workspace" in data:
            shared = _str(_dict(workspace.get("package")).get("version"))
            raise _Workspace(f"{file} is a workspace root", {"version": shared})
        name = _str(package.get("name"))
        version = package.get("version")
        if _dict(version).get("workspace") is True:  # version.workspace = true
            version = inherit.get("version")
        return _manifest(name, _str(version), file)

    async def lookup(self, client: RegistryClient, name: str, version: str) -> Found | None:
        data = await client.get_json(f"https://crates.io/api/v1/crates/{quote(name)}/{quote(version)}")
        v = _dict(_dict(data).get("version"))
        if not v:
            return None
        # Trusted publishing: crates.io records the GitHub repository and commit whose workflow
        # published the version, from the workflow's OIDC token.
        trusted = v.get("trustpub_data")
        attestation = None
        if isinstance(trusted, dict):
            on_github = str(trusted.get("provider", "")).lower() == "github"
            attestation = Provenance(
                github_key(trusted.get("repository")) if on_github else None, _sha(trusted.get("sha"))
            )
        return Found(
            links=(v.get("repository"), v.get("homepage")),
            yanked=bool(v.get("yanked")),
            attestation=attestation,
            attestation_checked="trustpub_data" in v,
        )

    def install(self, name: str, version: str) -> str:
        return f"cargo add {name}@={version}"


_GO_VERSION = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


class Go(Registry):
    def parse(self, file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        m = re.search(r'^\s*module\s+"?([^\s"]+)"?\s*$', text, re.MULTILINE)
        return Manifest(m.group(1), None, file) if m else None

    def versions(self, pin: Pin, manifest: Manifest, tagged: str | None) -> list[str]:
        # A Go module version is a tag: a full semantic version (v1.2.3), prefixed with the module's
        # directory when the module is not at the root (sub/v1.2.3). A major-version directory (v2/)
        # takes no prefix of its own.
        if pin.kind == "branch":
            return []
        d = re.sub(r"(?:^|/)v\d+$", "", posixpath.dirname(manifest.file))
        prefix = f"{d}/" if d else ""
        rest = pin.ref[len(prefix) :]
        return [rest] if pin.ref.startswith(prefix) and _GO_VERSION.match(rest) else []

    async def lookup(self, client: RegistryClient, name: str, version: str) -> Found | None:
        data = await client.get_json(
            f"https://proxy.golang.org/{_go_escape(name)}/@v/{_go_escape(version)}.info"
        )
        if not isinstance(data, dict):
            return None
        origin = _dict(data.get("Origin"))
        # The proxy fetches a module from its path, so the path itself is where it came from; Origin,
        # when the proxy recorded it, also covers vanity paths (go.uber.org/zap) and names the commit.
        return Found(links=(origin.get("URL"), f"https://{name}"), commit=_str(origin.get("Hash")))

    def install(self, name: str, version: str) -> str:
        return f"go get {name}@{version}"


def _go_escape(path: str) -> str:
    """The module proxy's case encoding: each upper-case letter becomes ``!`` and its lower case."""
    return quote("".join(f"!{c.lower()}" if c.isupper() else c for c in path), safe="/!")


class RubyGems(Registry):
    def matches(self, base: str) -> bool:
        return base.endswith(".gemspec")

    def present(self, names: Sequence[str], repo: Repo) -> list[str]:
        specs = sorted(n for n in names if self.matches(n))
        own = f"{repo.name}.gemspec"
        return ([own] if own in specs else []) + [s for s in specs if s != own][:2]

    def parse(self, file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        n = re.search(r"""\.name\s*=\s*["']([^"']+)["']""", text)
        v = re.search(r"""\.version\s*=\s*["']([0-9][^"']*)["']""", text)
        return _manifest(n.group(1) if n else None, v.group(1) if v else None, file)

    async def lookup(self, client: RegistryClient, name: str, version: str) -> Found | None:
        data = await client.get_json(
            f"https://rubygems.org/api/v2/rubygems/{quote(name)}/versions/{quote(version)}.json"
        )
        if not isinstance(data, dict):
            return None
        meta = _dict(data.get("metadata"))
        links = (
            data.get("source_code_uri"),
            data.get("homepage_uri"),
            data.get("bug_tracker_uri"),
            meta.get("source_code_uri"),
            meta.get("homepage_uri"),
        )
        attestation, checked = None, False
        try:
            attestation = _sigstore_provenance(
                await client.get_json(
                    f"https://rubygems.org/api/v1/attestations/{quote(name)}-{quote(version)}.json"
                )
            )
            checked = True
        except RegistryError as exc:
            log.info("RubyGems attestation not read for %s %s: %s", name, version, exc)
        return Found(
            links=links, yanked=bool(data.get("yanked")), attestation=attestation, attestation_checked=checked
        )

    def install(self, name: str, version: str) -> str:
        return f"gem install {name} -v {version}"


class Packagist(Registry):
    def parse(self, file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        name, version = _str(data.get("name")), _str(data.get("version"))
        return _manifest(name, version, file)

    async def lookup(self, client: RegistryClient, name: str, version: str) -> Found | None:
        vendor, package = name.split("/", 1)
        data = await client.get_json(f"https://repo.packagist.org/p2/{quote(vendor)}/{quote(package)}.json")
        entries = _dict(_dict(data).get("packages")).get(name)
        if not isinstance(entries, list):
            return None
        want = version.lstrip("vV")
        current: dict[str, Any] = {}
        for entry in entries:  # "minified" metadata: each entry lists only what changed from the last
            if not isinstance(entry, dict):
                continue
            for k, v in entry.items():
                if v == "__unset":
                    current.pop(k, None)
                else:
                    current[k] = v
            if isinstance(current.get("version"), str) and current["version"].lstrip("vV") == want:
                source = _dict(current.get("source"))
                return Found(links=(source.get("url"),), commit=_str(source.get("reference")))
        return None

    def install(self, name: str, version: str) -> str:
        return f"composer require {name}:{version.lstrip('vV')}"


_MAVEN_GROUP = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
_MAVEN_ARTIFACT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")


class Maven(Registry):
    """Maven Central. The package name is ``groupId:artifactId``."""

    def matches(self, base: str) -> bool:
        return base in ("pom.xml", "gradle.properties")

    def parse(self, file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        base = posixpath.basename(file)
        if base == "pom.xml":
            return self._pom(file, text, inherit)
        if base == "gradle.properties":
            return self._gradle(file, text, inherit)
        raise _NotChecked(
            f"{file} is a Gradle build script: it sets the Maven coordinates in code, which this check "
            "does not run (it reads pom.xml, or GROUP and POM_ARTIFACT_ID in gradle.properties)"
        )

    @staticmethod
    def _pom(file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        root = _xml(text)
        if root is None or root.tag != "project":
            return None
        parent = root.find("parent")
        group = _literal(_child(root, "groupId") or _child(parent, "groupId")) or inherit.get("group")
        artifact = _literal(_child(root, "artifactId"))
        version = _child(root, "version") or _child(parent, "version") or inherit.get("version")
        if _child(root, "packaging") == "pom" and any(_str(m.text) for m in root.iterfind("modules/module")):
            raise _Workspace(f"{file} is a multi-module build", {"group": group, "version": version})
        return _manifest(f"{group}:{artifact}", version, file) if group and artifact else None

    @staticmethod
    def _gradle(file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        props: dict[str, str] = {}
        for line in text.splitlines():
            m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*[=:]\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith(("#", "!")):
                props[m.group(1)] = m.group(2)
        group = _literal(props.get("GROUP")) or inherit.get("group")
        artifact = _literal(props.get("POM_ARTIFACT_ID"))
        version = props.get("VERSION_NAME") or inherit.get("version")
        if group and artifact:
            return _manifest(f"{group}:{artifact}", version, file)
        if group and not posixpath.dirname(file):  # set once for every module of the build
            raise _Workspace(
                f"{file} sets the group of a multi-module build", {"group": group, "version": version}
            )
        return None

    def released(self, version: str) -> bool:
        return not version.upper().endswith("-SNAPSHOT")  # snapshots are never on Central

    async def lookup(self, client: RegistryClient, name: str, version: str) -> Found | None:
        group, artifact = name.split(":", 1)
        links: list[str] = []
        commit = None
        g, a, v = group, artifact, version
        for depth in range(3):  # the POM, then up to two parents that share its group
            text = await client.get_text(_pom_url(g, a, v))
            root = _xml(text) if text is not None else None
            if root is None:
                if depth == 0 and text is not None:
                    raise RegistryError("Maven Central returned a malformed POM")
                if depth == 0:
                    return None
                break
            scm = root.find("scm")
            links += [
                s
                for s in (
                    _child(scm, "url"),
                    _child(scm, "connection"),
                    _child(scm, "developerConnection"),
                    _child(root, "url"),
                )
                if s
            ]
            if depth == 0:
                commit = _sha(_child(scm, "tag"))  # usually a tag name; sometimes the commit
            if any(github_key(link) for link in links):
                break
            # Maven inherits <scm> from the parent POM. Follow a parent only within the same group,
            # never into a shared corporate or foundation parent that names another repository.
            parent = root.find("parent")
            pg, pa, pv = _child(parent, "groupId"), _child(parent, "artifactId"), _child(parent, "version")
            if not (pg and pa and pv) or not (group == pg or group.startswith(pg + ".")):
                break
            if not (_MAVEN_GROUP.match(pg) and _MAVEN_ARTIFACT.match(pa) and _VERSION.match(pv)):
                break
            g, a, v = pg, pa, pv
        return Found(links=tuple(links), commit=commit)

    def install(self, name: str, version: str) -> str:
        return f"{name}:{version}"


def _pom_url(group: str, artifact: str, version: str) -> str:
    a, v = quote(artifact), quote(version)
    return f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{a}/{v}/{a}-{v}.pom"


_NOT_A_NUGET_PACKAGE = re.compile(
    r"[.](?:tests?|unittests?|integrationtests?|specs?|benchmarks?|samples?|examples?|demos?)$",
    re.IGNORECASE,
)


class NuGet(Registry):
    def matches(self, base: str) -> bool:
        stem, ext = posixpath.splitext(base)
        return ext.lower() in self.files and not _NOT_A_NUGET_PACKAGE.search(stem)

    def present(self, names: Sequence[str], repo: Repo) -> list[str]:
        found = sorted(n for n in names if self.matches(n))
        own = [n for n in found if posixpath.splitext(n)[0].lower() == repo.name.lower()]
        return own + [n for n in found if n not in own][:2]

    def order(self, base: str) -> int:
        return 1 if base.lower().endswith(".nuspec") else 0

    def parse(self, file: str, text: str, inherit: Mapping[str, str]) -> Manifest | None:
        root = _xml(text)
        if root is None:
            return None
        if file.lower().endswith(".nuspec"):
            meta = root.find("metadata")
            return _manifest(_literal(_child(meta, "id")), _literal(_child(meta, "version")), file)

        def prop(tag: str) -> str | None:
            return _literal(next((s for el in root.iter(tag) if (s := _str(el.text))), None))

        if (prop("IsPackable") or "").lower() == "false" or (prop("IsTestProject") or "").lower() == "true":
            raise _NotChecked(f"{file} is not packed as a package")
        stem = posixpath.splitext(posixpath.basename(file))[0]
        name = prop("PackageId") or prop("AssemblyName") or stem
        version = prop("PackageVersion") or prop("Version") or prop("VersionPrefix")
        return _manifest(name, version, file)

    async def lookup(self, client: RegistryClient, name: str, version: str) -> Found | None:
        lower, normal = name.lower(), _nuget_version(version)
        if normal is None:
            return None
        text = await client.get_text(
            f"https://api.nuget.org/v3-flatcontainer/{quote(lower)}/{quote(normal)}/{quote(lower)}.nuspec"
        )
        if text is None:
            return None
        root = _xml(text)
        meta = root.find("metadata") if root is not None else None
        if meta is None:
            raise RegistryError("NuGet returned a malformed package spec")
        repository = meta.find("repository")
        links = (repository.get("url") if repository is not None else None, _child(meta, "projectUrl"))
        commit = _sha(repository.get("commit")) if repository is not None else None
        leaf = await client.get_json(
            f"https://api.nuget.org/v3/registration5-gz-semver2/{quote(lower)}/{quote(normal)}.json"
        )
        return Found(links=links, yanked=_dict(leaf).get("listed") is False, commit=commit)

    def install(self, name: str, version: str) -> str:
        return f"dotnet add package {name} --version {version}"


def _nuget_version(version: str) -> str | None:
    """NuGet's normalized form: three or four numbers (a zero fourth dropped), no build metadata."""
    m = re.fullmatch(r"(\d+(?:\.\d+){0,3})(-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version)
    if m is None:
        return None
    numbers = [int(x) for x in m.group(1).split(".")]
    numbers += [0] * (3 - len(numbers))
    if len(numbers) == 4 and numbers[3] == 0:
        numbers.pop()
    return (".".join(map(str, numbers)) + (m.group(2) or "")).lower()


PYPI = PyPI(
    "pypi",
    "PyPI",
    ("pyproject.toml", "setup.cfg", "setup.py"),
    re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$"),
    frozenset({"python", "jupyter notebook", "cython"}),
)
NPM = Npm(
    "npm",
    "npm",
    ("package.json",),
    re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]{0,100}/)?[a-z0-9][a-z0-9._~-]{0,100}$"),
    frozenset({"javascript", "typescript", "vue", "svelte", "coffeescript"}),
)
CRATES = Crates(
    "crates", "crates.io", ("Cargo.toml",), re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"), frozenset({"rust"})
)
GO = Go(
    "go",
    "Go module proxy",
    ("go.mod",),
    re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}(?:/[A-Za-z0-9._~+-]+){0,8}$"),
    frozenset({"go"}),
)
RUBYGEMS = RubyGems(
    "rubygems",
    "RubyGems",
    ("*.gemspec",),
    re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"),
    frozenset({"ruby"}),
)
PACKAGIST = Packagist(
    "packagist",
    "Packagist",
    ("composer.json",),
    re.compile(r"^[a-z0-9](?:[_.-]?[a-z0-9]+)*/[a-z0-9](?:(?:[_.]|-{1,2})?[a-z0-9]+)*$"),
    frozenset({"php"}),
)
MAVEN = Maven(
    "maven",
    "Maven Central",
    ("pom.xml", "gradle.properties", "build.gradle.kts", "build.gradle"),
    re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$"),  # group:artifact
    frozenset({"java", "kotlin", "scala", "groovy"}),
)
NUGET = NuGet(
    "nuget",
    "NuGet",
    (".csproj", ".fsproj", ".vbproj", ".nuspec"),
    re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$"),
    frozenset({"c#", "f#", "visual basic .net"}),
    withdrawn="unlisted",
)
REGISTRIES: tuple[Registry, ...] = (PYPI, NPM, CRATES, GO, RUBYGEMS, PACKAGIST, MAVEN, NUGET)
_LABELS = ", ".join(r.label for r in REGISTRIES)


# --------------------------------------------------------------------------- finding the manifest


class RepoFiles(Protocol):
    """A repository's files at the pinned commit, read through its source host."""

    async def names(self) -> list[str] | None:
        """File names at the repository root, or None when they could not be listed."""
        ...

    async def tree(self) -> list[str] | None:
        """Paths of every file in the repository, or None when they could not be listed."""
        ...

    async def read(self, path: str) -> str | None: ...


def choose_registry(repo: Repo, names: Sequence[str]) -> tuple[Registry | None, list[str]]:
    """The registry for this repository and the root manifests to read for it.

    The language decides when it maps to a registry, even if its manifest is not at the root (then
    the subdirectories are searched). Otherwise the first registry whose manifest is at the root.
    """
    language = (repo.language or "").lower()
    for registry in REGISTRIES:
        if language in registry.languages:
            return registry, registry.present(names, repo)
    for registry in REGISTRIES:
        files = registry.present(names, repo)
        if files:
            return registry, files
    return None, []


# Directories whose manifests are never the published package.
_SKIP_DIRS = frozenset(
    {
        "test", "tests", "testing", "testdata", "fixture", "fixtures", "example", "examples", "sample",
        "samples", "demo", "demos", "doc", "docs", "bench", "benches", "benchmark", "benchmarks", "e2e",
        "node_modules", "vendor", "third_party", "third-party", "build", "dist", "target",
    }
)  # fmt: skip
_MAX_DEPTH = 4  # directory levels below the root
_MAX_READS = 6  # manifests read from subdirectories, per pick
# Tag prefixes that do not name a package: release-1.2.3, version-2.0 ...
_NOT_A_PACKAGE = frozenset({"release", "releases", "rel", "version", "ver", "stable", "tag", "r"})
# Language suffixes repository names carry and package names usually do not: sigstore-js -> sigstore.
_LANGUAGE_SUFFIX = re.compile(
    r"[-_.](?:js|ts|node|py|python|rs|rust|go|golang|rb|ruby|php|java|jvm|kotlin|kt|net|dotnet|csharp)$",
    re.IGNORECASE,
)
_STEM_FILES = (".gemspec", ".csproj", ".fsproj", ".vbproj", ".nuspec")  # named after their package


def _key(s: str) -> str:
    return re.sub(r"[-_.\s]+", "-", s.strip().lower())


def _bare(name: str) -> str:
    """A package name without its scope, vendor, group or module path: ``@scope/core`` -> ``core``."""
    return re.split(r"[/:]", name)[-1]


def _places(path: str) -> set[str]:
    """Names a manifest's location gives it: its directory, that directory's last part, and the file
    name for manifests named after their package."""
    d, base = posixpath.split(path)
    out = {_key(d), _key(posixpath.basename(d))}
    if base.lower().endswith(_STEM_FILES):
        out.add(_key(posixpath.splitext(base)[0]))
    return out - {""}


def _fit(hints: Sequence[str], name: str | None, path: str) -> tuple[int, int] | None:
    """How well a package fits the hints, best first: (hint, 0 by its name / 1 by its location)."""
    if name is not None:
        for i, h in enumerate(hints):
            if _key(name) == _key(h) or _key(_bare(name)) == _key(_bare(h)):
                return i, 0
    places = _places(path)
    for i, h in enumerate(hints):
        if _key(h) in places or _key(_bare(h)) in places:
            return i, 1
    return None


def _tag_package(pin: Pin) -> tuple[str | None, str | None]:
    """(package the pinned tag is for, its version); None for a branch or a tag with no prefix."""
    if pin.kind == "branch":
        return None, None
    split = split_tag(pin.ref)
    if split is None:
        return None, None
    prefix, version = split
    return (prefix if prefix and _key(prefix) not in _NOT_A_PACKAGE else None), version


async def _read_root(files: RepoFiles, path: str) -> str | None:
    try:
        return await files.read(path)
    except RateLimited:
        raise _Unknown(f"rate limited while reading {path}") from None
    except SourceError:
        raise _Unknown(f"{path} could not be read") from None


class PackageChecker:
    def __init__(self, client: RegistryClient) -> None:
        self.client = client

    async def aclose(self) -> None:
        await self.client.aclose()

    async def check(self, repo: Repo, pin: Pin, files: RepoFiles) -> Package:
        try:
            names = await files.names()
        except RateLimited:
            return Package("unknown", "rate limited while listing the repository's files")
        except SourceError:
            names = None
        if names is None:
            return Package("unknown", "the repository's files could not be listed at the pinned commit")

        registry, roots = choose_registry(repo, names)
        if registry is None:
            what = f"{repo.language} project" if repo.language else "project with no primary language"
            return Package("unsupported", f"a {what} with no {_LABELS} manifest at its root")
        label = registry.label
        prefix, tagged = _tag_package(pin)
        hints = [h for h in (prefix, repo.name, _LANGUAGE_SUFFIX.sub("", repo.name)) if h]
        hints = list(dict.fromkeys(hints))
        try:
            manifest = await self._locate(registry, files, roots, hints, prefix)
        except _Unknown as stop:
            return Package("unknown", str(stop), registry=label)
        if not registry.name_rule.match(manifest.name):
            return Package(
                "unknown", f"{manifest.file} names a package {label} does not allow", registry=label
            )
        name = manifest.name
        path = posixpath.dirname(manifest.file) or None

        tag_is_its = prefix is None or _fit([prefix], name, manifest.file) is not None
        versions = registry.versions(pin, manifest, tagged if tag_is_its else None)
        if not versions:
            if pin.kind == "branch":
                why = f"pinned to the {pin.ref} branch and {manifest.file} declares no version to look up"
            elif not tag_is_its:
                why = f"the pinned tag {pin.ref} is another package's and {manifest.file} declares no version"
            else:
                why = f"{pin.ref} is not a version {label} can look up"
            return Package("unknown", why, registry=label, name=name, path=path)

        found: Found | None = None
        version = versions[0]
        try:
            for version in versions:
                found = await registry.lookup(self.client, name, version)
                if found is not None:
                    break
        except RegistryError as exc:
            return Package(
                "unknown", f"{label} could not be checked: {exc}", registry=label, name=name, path=path
            )
        if found is None:
            return Package(
                "not_published",
                f"{label} has no {name} {' or '.join(versions)}",
                registry=label,
                name=name,
                version=versions[0],
                path=path,
            )

        def result(status: PackageStatus, detail: str, **extra: Any) -> Package:
            return Package(status, detail, registry=label, name=name, version=version, path=path, **extra)

        key = repo.full_name.lower()
        attested = found.attestation
        if attested is not None and attested.repo != key:
            return result(
                "not_linked",
                f"{label}'s build attestation for {name} {version} names another repository as its "
                "source: do not install it as this pick",
            )
        if attested is None and key not in {github_key(link) for link in found.links}:
            return result(
                "not_linked",
                f"{label}'s {name} {version} does not name this repository as its source, so it may be "
                "another project: do not install it as this pick",
            )
        if found.yanked:
            return result(
                "yanked", f"{label} withdrew ({registry.withdrawn}) {name} {version}: do not install it"
            )

        commit = (attested.commit if attested else None) or found.commit
        same = None if commit is None else commit.lower() == pin.sha.lower()
        notes = []
        if pin.kind == "branch":
            notes.append(
                f"the pin is the {pin.ref} branch head and {version} is the version its {manifest.file} "
                "declares: the branch may be ahead of that release"
            )
        elif not tag_is_its:
            notes.append(
                f"the pinned tag {pin.ref} is another package's; {version} is the version {manifest.file} "
                "declares at that commit"
            )
        if same is False:
            notes.append(f"{label} records {version} as built from another commit than the pinned revision")
        if found.deprecated:
            notes.append(f"marked deprecated on {label}")
        return result(
            "published",
            "; ".join(notes),
            install=registry.install(name, version),
            same_commit=same,
            attested=True if attested else (False if found.attestation_checked else None),
        )

    async def _locate(
        self,
        registry: Registry,
        files: RepoFiles,
        roots: Sequence[str],
        hints: Sequence[str],
        prefix: str | None,
    ) -> Manifest:
        """The package's manifest: the root one, unless the root is a workspace, has none, or the
        pinned tag names another package; then the best fit in the subdirectories. Raises
        ``_Unknown`` with the reason when there is none."""
        root: Manifest | None = None
        inherit: dict[str, str] = {}
        why = (
            f"no package name in {', '.join(roots)}"
            if roots
            else f"no {' or '.join(registry.files)} at the root"
        )
        for file in roots:
            text = await _read_root(files, file)
            if text is None:
                continue
            try:
                root = registry.parse(file, text, inherit)
            except _NotChecked as skip:
                raise _Unknown(str(skip)) from None
            except _Workspace as workspace:
                why, inherit = str(workspace), workspace.inherit
                break
            if root is not None:
                break
        if root is not None and (prefix is None or _fit([prefix], root.name, root.file)):
            return root

        try:
            found, fit, missing = await self._search(registry, files, hints, inherit)
        except _Unsearched as failed:
            if root is not None:
                why = f"the pinned tag names {prefix}, not the package in the root {root.file}"
            raise _Unknown(f"{why}, and {failed}") from None
        if found is not None and (root is None or (fit is not None and fit[0] == 0)):
            return found
        if root is not None:  # searched, and nothing fits the tag better
            return root
        raise _Unknown(f"{why}, and {missing}")

    async def _search(
        self, registry: Registry, files: RepoFiles, hints: Sequence[str], inherit: Mapping[str, str]
    ) -> tuple[Manifest | None, tuple[int, int] | None, str]:
        """The package in a subdirectory that best fits the hints: (manifest, fit, why none).
        Raises ``_Unsearched`` when the subdirectories could not be listed or read."""
        try:
            paths = await files.tree()
        except RateLimited:
            raise _Unsearched("its subdirectories could not be listed (rate limited)") from None
        except SourceError:
            paths = None
        if paths is None:
            raise _Unsearched("its subdirectories could not be listed")

        by_dir: dict[str, list[str]] = {}
        for p in paths:
            d, _, base = p.rpartition("/")
            parts = d.split("/")
            if not d or len(parts) > _MAX_DEPTH or not registry.matches(base):
                continue
            if any(x.startswith(".") or x.lower() in _SKIP_DIRS for x in parts):
                continue
            by_dir.setdefault(d, []).append(p)
        if not by_dir:
            return None, None, "none in its subdirectories"

        def first(d: str) -> tuple[tuple[int, int], int, str]:  # likely fits first, then shallow ones
            fits = [f for p in by_dir[d] if (f := _fit(hints, None, p))]
            return min(fits, default=(len(hints), 2)), d.count("/"), d

        read: list[Manifest] = []
        reads = 0
        for d in sorted(by_dir, key=first):
            if reads >= _MAX_READS:
                break
            for p in sorted(by_dir[d], key=lambda p: (registry.order(posixpath.basename(p)), p)):
                if reads >= _MAX_READS:
                    break
                reads += 1
                try:
                    text = await files.read(p)
                except RateLimited:
                    raise _Unsearched("its subdirectories could not be read (rate limited)") from None
                except SourceError:
                    continue
                try:
                    manifest = registry.parse(p, text, inherit) if text is not None else None
                except (_NotChecked, _Workspace):
                    break  # this directory holds no package of its own
                if manifest is not None:
                    read.append(manifest)
                    break

        fitting = [(f, m) for m in read if (f := _fit(hints, m.name, m.file))]
        if fitting:
            fit, best = min(fitting, key=lambda x: (x[0], x[1].file.count("/"), x[1].file))
            return best, fit, ""
        if len(by_dir) == 1 and read:  # the only package in the repository
            return read[0], None, ""
        n = len(by_dir)
        if not read:
            return None, None, f"none of the {n} manifests read in its subdirectories names a package"
        return (
            None,
            None,
            (
                f"none of the {n} manifests in its subdirectories is named after the repository or its "
                "pinned tag"
            ),
        )
