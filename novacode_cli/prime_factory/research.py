"""Version-aware research receipts from host-controlled document retrieval."""

from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import json
import re
import socket
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http import HTTPStatus
from itertools import islice
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    from pathlib import Path

from novacode_cli.prime_factory.knowledge import MAX_BYTES, KnowledgeIndex, Record
from novacode_cli.prime_memory import DEPENDENCIES, MAX_FILE_BYTES, safe_text


@dataclass(frozen=True)
class FetchResult:
    """A transport's observed response; model summaries are not fetch receipts."""

    url: str
    status: int
    content: str
    version: str
    official: bool = False


Fetcher = Callable[[str], FetchResult]
MAX_REDIRECTS = 5


def make_fetcher(
    version: str, *, authorize: Callable[[str], bool], dependency: str = ""
) -> Fetcher:
    """Create a bounded transport after the originating host grants each URL.

    A configured URL policy is still enforced even when the host grants a URL.
    Version identity must appear in the fetched page or URL; absent evidence
    returns an empty version and cannot satisfy the research gate.
    """
    import httpx

    from novacode_cli.security.policy import get_policy
    from novacode_cli.security.unicode_security import check_url_safety
    from novacode_cli.tools.web_tools import _DOCS_SITES

    def fetch_document(url: str) -> FetchResult:
        target = url
        with httpx.Client(
            timeout=15,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                ResearchLedger._url(target)
                parsed = urlsplit(target)
                if (
                    not check_url_safety(target).safe
                    or get_policy().evaluate("fetch_url", {"url": target}).tier == "deny"
                    or not authorize(target)
                ):
                    msg = "Documentation URL requires host authorization"
                    raise ValueError(msg)
                addresses = socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
                if not addresses or any(
                    not ipaddress.ip_address(address[4][0]).is_global for address in addresses
                ):
                    msg = "Documentation URL must resolve to public network addresses"
                    raise ValueError(msg)
                # Pin the checked address while keeping Host, SNI and certificate
                # verification on the authorized origin. A second DNS lookup can rebind.
                connection_url = httpx.URL(target).copy_with(host=addresses[0][4][0])
                with client.stream(
                    "GET",
                    connection_url,
                    headers={"Host": parsed.netloc},
                    extensions={"sni_hostname": parsed.hostname},
                ) as response:
                    if response.is_redirect:
                        target = urljoin(target, response.headers.get("location", ""))
                        continue
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_BYTES:
                            msg = "Documentation exceeds content bound"
                            raise ValueError(msg)
                    content = chunks.decode(response.encoding or "utf-8", errors="replace")
                    marker = re.compile(r"(?<![\w.])v?" + re.escape(version) + r"(?![\w.])")
                    observed_version = (
                        version if version and marker.search(target + "\n" + content) else ""
                    )
                    official_sites = _DOCS_SITES.get(dependency.lower(), [])[:1]
                    official = any(parsed.hostname == site.split("/")[0] for site in official_sites)
                    return FetchResult(
                        target, response.status_code, content, observed_version, official
                    )
        msg = "Documentation exceeded redirect limit"
        raise ValueError(msg)

    return fetch_document


class ResearchLedger:
    """Persist sources locally and block dependent work without current documentation."""

    def __init__(self, root: Path, directory: Path | None = None) -> None:
        """Use the repository's versioned knowledge storage."""
        self.index = KnowledgeIndex(root, directory)
        self._validated: set[str] = set()
        self._exempt = False
        self.environment = self.inspect_environment()

    def inspect_environment(
        self,
        dependencies: Iterable[str] = (),
        *,
        environment_path: Path | None = None,
    ) -> Record:
        """Inspect lockfiles before installed metadata, without importing dependencies."""
        versions: dict[str, str] = {}
        lock = self.index.root / "uv.lock"
        if lock.is_file() and lock.stat().st_size <= MAX_FILE_BYTES:
            parsed = tomllib.loads(lock.read_text(encoding="utf-8"))
            versions.update(
                (entry["name"], entry["version"])
                for entry in parsed.get("package", [])
                if isinstance(entry.get("name"), str) and isinstance(entry.get("version"), str)
            )
        package_lock = self.index.root / "package-lock.json"
        if package_lock.is_file() and package_lock.stat().st_size <= MAX_FILE_BYTES:
            parsed = json.loads(package_lock.read_text(encoding="utf-8"))
            versions.update(
                (name.removeprefix("node_modules/"), entry["version"])
                for name, entry in parsed.get("packages", {}).items()
                if name.startswith("node_modules/") and isinstance(entry.get("version"), str)
            )
        installed: dict[str, str | None] = {}
        isolated: dict[str, str] = {}
        if environment_path is not None:
            search = [
                environment_path / "Lib" / "site-packages",
                *sorted(environment_path.glob("lib/python*/site-packages")),
            ]
            distributions = importlib.metadata.distributions(path=[str(path) for path in search])
            for number, distribution in enumerate(islice(distributions, 1025)):
                if number >= 1024:  # noqa: PLR2004 - Bound execution-environment metadata enumeration.
                    message = "Execution environment exceeds package metadata bound"
                    raise ValueError(message)
                package = distribution.metadata["Name"]
                if package:
                    isolated[re.sub(r"[-_.]+", "-", package).lower()] = distribution.version
        for name in sorted(set(dependencies)):
            if environment_path is not None:
                installed[name] = isolated.get(re.sub(r"[-_.]+", "-", name).lower())
                continue
            try:
                installed[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                installed[name] = None
        fingerprints = {
            name: self.index.fingerprint(name)
            for name in DEPENDENCIES
            if (self.index.root / name).is_file()
        }
        return {
            "locked_versions": versions,
            "installed_versions": installed,
            "fingerprints": fingerprints,
        }

    @staticmethod
    def _url(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            msg = "Research requires a credential-free HTTP documentation URL"
            raise ValueError(msg)
        if safe_text(url, len(url)) != url:
            msg = "Research URL contains excluded credentials"
            raise ValueError(msg)

    def fetch(
        self,
        url: str,
        *,
        dependency: str,
        version: str,
        excerpt: str,
        decisions: list[str],
        fetcher: Fetcher,
    ) -> Record:
        """Accept a real successful response only through the authorized host fetcher.

        The caller supplies a transport that already enforces Nova's network policy.
        This layer never opens sockets or treats retrieved instructions as authority.
        """
        self._url(url)
        result = fetcher(url)
        if not isinstance(result, FetchResult):
            msg = "Research needs a host-observed fetch receipt"
            raise TypeError(msg)
        self._url(result.url)
        if (
            result.status != HTTPStatus.OK
            or not result.content
            or len(result.content.encode()) > MAX_BYTES
        ):
            msg = "Documentation fetch unavailable or exceeds content bound"
            raise ValueError(msg)
        if not dependency.strip() or not version.strip() or result.version != version:
            msg = "Documentation version mismatch"
            raise ValueError(msg)
        if (
            not excerpt.strip()
            or excerpt not in result.content
            or not decisions
            or not all(decisions)
        ):
            msg = "Documentation requires a fetched excerpt and supported decisions"
            raise ValueError(msg)
        environment = self.inspect_environment([dependency])
        locked = environment["locked_versions"].get(dependency)
        installed = environment["installed_versions"].get(dependency)
        if (locked is not None and locked != version) or (
            installed is not None and installed != version
        ):
            msg = "Documentation does not match locked or installed dependency version"
            raise ValueError(msg)
        identity = hashlib.sha256(f"{dependency}\0{version}\0{url}".encode()).hexdigest()
        record = self.index.put(
            "source",
            identity,
            {
                "requested_url": url,
                "url": result.url,
                "status": result.status,
                "dependency": dependency,
                "dependency_version": version,
                "retrieved_at": time.time(),
                "content_digest": hashlib.sha256(result.content.encode()).hexdigest(),
                "excerpt": safe_text(excerpt),
                "decisions": [safe_text(decision) for decision in decisions],
                "official": result.official,
                "untrusted": True,
            },
            fingerprints=environment["fingerprints"],
        )
        self._validated.add(identity)
        return record

    def exempt(self, reason: str) -> Record:
        """Explicitly record why a repository-only task needs no external API research."""
        if not reason.strip():
            msg = "Repository-only research exemption needs a reason"
            raise ValueError(msg)
        record = self.index.put("finding", "research_exemption", {"reason": safe_text(reason)})
        self._exempt = True
        return record

    def validate(self, requirements: dict[str, str]) -> list[str]:
        """Return blockers; search snippets and stale/version-mismatched sources cannot pass."""
        sources = self.index.records("source")
        blockers = []
        environment = self.inspect_environment(requirements)
        for dependency, version in sorted(requirements.items()):
            if any(
                versions.get(dependency) not in {None, version}
                for versions in (environment["locked_versions"], environment["installed_versions"])
            ):
                blockers.append(f"Documentation version mismatch: {dependency} {version}")
                continue
            matching = [
                source
                for source in sources
                if source["content"].get("dependency") == dependency
                and source["id"] in self._validated
                and source["content"].get("dependency_version") == version
                and source["content"].get("status") == HTTPStatus.OK
                and source["content"].get("excerpt")
                and source["content"].get("decisions")
                and self.index.current(source)
                and source["fingerprints"] == environment["fingerprints"]
            ]
            if not matching:
                blockers.append(f"Required documentation unavailable: {dependency} {version}")
        if not requirements and not self._exempt:
            blockers.append("Record a repository-only research exemption or required dependencies")
        return blockers

    def revalidate_source(self, source_record: Record, fetcher: Fetcher) -> bool:
        """Revalidate one approved source without changing its original provenance.

        Changed content needs a new research and approval decision. In particular,
        a fresh retrieval time cannot silently alter the approved plan identity.
        """
        identity = source_record["id"]
        self._validated.discard(identity)
        content = source_record["content"]
        expected_identity = hashlib.sha256(
            f"{content['dependency']}\0{content['dependency_version']}\0{content['requested_url']}".encode()
        ).hexdigest()
        if (
            source_record.get("version") != 1
            or source_record.get("kind") != "source"
            or identity != expected_identity
        ):
            msg = "Invalid documentation source identity"
            raise ValueError(msg)
        self._url(content["requested_url"])
        result = fetcher(content["requested_url"])
        if isinstance(result, FetchResult):
            self._url(result.url)
        environment = self.inspect_environment([content["dependency"]])
        if (
            not isinstance(result, FetchResult)
            or result.status != HTTPStatus.OK
            or result.url != content["url"]
            or result.version != content["dependency_version"]
            or not result.content
            or len(result.content.encode()) > MAX_BYTES
            or hashlib.sha256(result.content.encode()).hexdigest() != content["content_digest"]
            or not self.index.current(source_record)
            or source_record["fingerprints"] != environment["fingerprints"]
            or any(
                versions.get(content["dependency"]) not in {None, content["dependency_version"]}
                for versions in (environment["locked_versions"], environment["installed_versions"])
            )
        ):
            msg = f"Documentation changed or unavailable: {content['url']}"
            raise ValueError(msg)
        self.index.put(
            "source",
            identity,
            content,
            sources=tuple(source_record["sources"]),
            links=tuple((link[0], link[1]) for link in source_record["links"]),
            fingerprints=source_record["fingerprints"],
        )
        self._validated.add(identity)
        return True

    def revalidate(self, fetcher: Fetcher) -> list[str]:
        """Fetch reused sources again; changed/unavailable content blocks dependent work."""
        blockers = []
        for source in self.index.records("source"):
            try:
                self.revalidate_source(source, fetcher)
            except (OSError, ValueError) as exc:
                blockers.append(f"Documentation revalidation unavailable: {source['id']}: {exc}")
        return blockers
