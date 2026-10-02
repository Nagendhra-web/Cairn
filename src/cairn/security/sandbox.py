"""Filesystem and network boundaries for built-in tools."""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import socket
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

from cairn.core.errors import SandboxViolation


class PathSandbox:
    """Confines file access to a set of root directories.

    Paths are fully resolved (symlinks included) before the containment check,
    so ``../`` traversal and symlinks pointing outside a root are rejected.
    """

    DENY_NAMES = frozenset({".env", ".git", "id_rsa", "id_ed25519", ".netrc", ".pypirc"})

    def __init__(self, roots: Iterable[str | Path], *, read_only: bool = False) -> None:
        self.roots = [Path(r).resolve() for r in roots]
        if not self.roots:
            raise ValueError("PathSandbox needs at least one root")
        self.read_only = read_only

    def resolve(self, path: str | Path, *, write: bool = False) -> Path:
        if write and self.read_only:
            raise SandboxViolation("sandbox is read-only", path=str(path))
        raw = Path(path)
        candidate = raw if raw.is_absolute() else self.roots[0] / raw
        resolved = candidate.resolve()
        if not any(resolved == r or r in resolved.parents for r in self.roots):
            raise SandboxViolation(f"path '{path}' escapes the sandbox", path=str(path))
        if any(part in self.DENY_NAMES for part in resolved.parts):
            raise SandboxViolation(f"path '{path}' touches a protected file", path=str(path))
        return resolved


class NetworkPolicy:
    """Domain allowlist with private-address protection (SSRF defense).

    Hostnames are resolved and every resulting address is checked, so a public
    name that resolves to ``127.0.0.1`` or a cloud metadata address is refused
    unless ``allow_private`` is set.
    """

    def __init__(
        self,
        allow_domains: Iterable[str] = (),
        *,
        allow_private: bool = False,
        schemes: Iterable[str] = ("https", "http"),
    ) -> None:
        self.allow_domains = [d.lower() for d in allow_domains]
        self.allow_private = allow_private
        self.schemes = frozenset(schemes)

    def check(self, url: str, *, resolve: bool = True) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in self.schemes:
            raise SandboxViolation(f"scheme '{parsed.scheme}' is not allowed", url=url)
        host = (parsed.hostname or "").lower()
        if not host:
            raise SandboxViolation("URL has no host", url=url)
        if not any(fnmatch.fnmatchcase(host, pattern) for pattern in self.allow_domains):
            raise SandboxViolation(
                f"host '{host}' is not on the network allowlist", url=url, allow=self.allow_domains
            )
        if not self.allow_private and resolve:
            for addr in _addresses(host):
                if _is_private(addr):
                    raise SandboxViolation(
                        f"host '{host}' resolves to non-public address {addr}", url=url
                    )
        return url


def _addresses(host: str) -> list[str]:
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    return sorted({str(info[4][0]) for info in infos})


def _is_private(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for subprocesses: no inherited credentials."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"}
    env.update(extra or {})
    return env
