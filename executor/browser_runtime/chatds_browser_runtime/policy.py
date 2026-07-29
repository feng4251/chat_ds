"""Validate the runtime-injected egress proxy used by browser workers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
from typing import Mapping
from urllib.parse import urlsplit


PROXY_ENVIRONMENT_VARIABLE = "SKILL_EGRESS_PROXY_URL"
CA_CERTIFICATE_ENVIRONMENT_VARIABLE = "SKILL_EGRESS_CA_CERT_PATH"
LEAF_SPKI_ENVIRONMENT_VARIABLE = "SKILL_EGRESS_LEAF_SPKI_PATH"
TRUST_GENERATION_MANIFEST_ENVIRONMENT_VARIABLE = (
    "SKILL_EGRESS_TRUST_GENERATION_MANIFEST_PATH"
)
TRUST_GENERATION_ENVIRONMENT_VARIABLE = (
    "SKILL_EGRESS_TRUST_GENERATION"
)
_TRUST_DIRECTORY_NAME = ".chatds-egress-trust"
_CONTROLLER_UID = 0
_SPKI_SHA256_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
_MAX_CA_CERTIFICATE_BYTES = 64 * 1024
_MAX_SPKI_FILE_BYTES = 256
_MAX_TRUST_GENERATION_MANIFEST_BYTES = 4 * 1024
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class PolicyError(RuntimeError):
    """The runtime proxy environment is absent or malformed."""


@dataclass(frozen=True)
class ProxyPolicy:
    policy_id: str
    proxy_url: str
    ca_cert_path: str
    leaf_spki_path: str
    leaf_spki_sha256: str
    trust_generation_manifest_path: str
    trust_generation: str


def _validate_proxy_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyError("proxy_url must be a non-empty string")
    if len(value) > 1024:
        raise PolicyError("proxy_url is too long")
    if any(char.isspace() or ord(char) < 0x21 for char in value):
        raise PolicyError("proxy_url contains whitespace or control characters")

    parsed = urlsplit(value)
    if parsed.scheme != "http":
        raise PolicyError("proxy_url must use the internal HTTP CONNECT proxy")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PolicyError("proxy_url has an invalid port") from exc
    if not parsed.hostname or port is None:
        raise PolicyError("proxy_url must include a host and explicit port")
    if not 1 <= port <= 65_535:
        raise PolicyError("proxy_url has an invalid port")
    if parsed.username is not None or parsed.password is not None:
        raise PolicyError("proxy credentials are forbidden in the worker policy")
    if (
        parsed.path
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise PolicyError("proxy_url may not contain a path, query, or fragment")
    return f"http://{parsed.netloc}"


def _read_runtime_trust_file(
    value: object,
    *,
    expected_name: str,
    maximum_bytes: int,
) -> tuple[str, bytes]:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise PolicyError("runtime trust path is absent or malformed")
    if "\x00" in value:
        raise PolicyError("runtime trust path is absent or malformed")
    path = Path(value)
    if (
        not path.is_absolute()
        or path.name != expected_name
        or path.parent.name != _TRUST_DIRECTORY_NAME
    ):
        raise PolicyError("runtime trust path is not lease-scoped")
    try:
        parent_info = path.parent.lstat()
        resolved_path = path.resolve(strict=True)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise PolicyError("runtime trust material is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            path.parent.is_symlink()
            or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != _CONTROLLER_UID
            or parent_info.st_gid != os.getegid()
            or stat.S_IMODE(parent_info.st_mode) != 0o550
            or resolved_path != path
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != _CONTROLLER_UID
            or before.st_gid != os.getegid()
            or stat.S_IMODE(before.st_mode) != 0o440
            or not 1 <= before.st_size <= maximum_bytes
        ):
            raise PolicyError("runtime trust material has unsafe metadata")
        content = bytearray()
        while len(content) <= maximum_bytes:
            chunk = os.read(
                descriptor,
                min(64 * 1024, maximum_bytes + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(content) != before.st_size
            or len(content) > maximum_bytes
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
        ):
            raise PolicyError(
                "runtime trust material changed during validation"
            )
        return str(path), bytes(content)
    finally:
        os.close(descriptor)


def _load_runtime_trust(
    source: Mapping[str, str],
) -> tuple[str, str, str, str, str]:
    ca_path, ca_content = _read_runtime_trust_file(
        source.get(CA_CERTIFICATE_ENVIRONMENT_VARIABLE),
        expected_name="ca.pem",
        maximum_bytes=_MAX_CA_CERTIFICATE_BYTES,
    )
    spki_path, spki_content = _read_runtime_trust_file(
        source.get(LEAF_SPKI_ENVIRONMENT_VARIABLE),
        expected_name="leaf.spki",
        maximum_bytes=_MAX_SPKI_FILE_BYTES,
    )
    manifest_path, manifest_content = _read_runtime_trust_file(
        source.get(
            TRUST_GENERATION_MANIFEST_ENVIRONMENT_VARIABLE
        ),
        expected_name="generation.json",
        maximum_bytes=_MAX_TRUST_GENERATION_MANIFEST_BYTES,
    )
    if len({
        Path(ca_path).parent,
        Path(spki_path).parent,
        Path(manifest_path).parent,
    }) != 1:
        raise PolicyError("runtime trust files do not share one lease root")
    try:
        decoded_ca = ca_content.decode("ascii", errors="strict")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=decoded_ca)
        spki = spki_content.decode("ascii", errors="strict").strip()
    except (UnicodeError, ssl.SSLError) as exc:
        raise PolicyError("runtime trust material is invalid") from exc
    if (
        _SPKI_SHA256_RE.fullmatch(spki) is None
        or spki_content != (spki + "\n").encode("ascii")
    ):
        raise PolicyError("runtime trust material is invalid")
    try:
        manifest = json.loads(
            manifest_content.decode("ascii", errors="strict")
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyError("runtime trust generation is invalid") from exc
    expected_fields = {
        "version",
        "generation_id",
        "ca_file_sha256",
        "leaf_spki_file_sha256",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected_fields
        or manifest.get("version") != 1
        or isinstance(manifest.get("version"), bool)
        or any(
            not isinstance(manifest.get(name), str)
            or _SHA256_HEX_RE.fullmatch(manifest[name]) is None
            for name in (
                "generation_id",
                "ca_file_sha256",
                "leaf_spki_file_sha256",
            )
        )
    ):
        raise PolicyError("runtime trust generation is invalid")
    canonical_manifest = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    digest = hashlib.sha256()
    digest.update(b"chatds-egress-trust-generation-v1\x00")
    digest.update(ca_content)
    digest.update(b"\x00")
    digest.update(spki_content)
    generation = digest.hexdigest()
    if (
        manifest_content != canonical_manifest
        or manifest["generation_id"] != generation
        or manifest["ca_file_sha256"]
        != hashlib.sha256(ca_content).hexdigest()
        or manifest["leaf_spki_file_sha256"]
        != hashlib.sha256(spki_content).hexdigest()
        or source.get(TRUST_GENERATION_ENVIRONMENT_VARIABLE)
        != generation
    ):
        raise PolicyError("runtime trust generation is invalid")
    return (
        ca_path,
        spki_path,
        spki,
        manifest_path,
        generation,
    )


def load_proxy_environment(
    environment: Mapping[str, str] | None = None,
) -> ProxyPolicy:
    """Read the fixed runtime-owned environment key.

    This launch control is defense in depth. The worker's ``network_mode:
    none`` namespace remains the authority that makes direct egress
    impossible if untrusted code changes its own child environment.
    """

    source = os.environ if environment is None else environment
    (
        ca_path,
        spki_path,
        spki,
        manifest_path,
        trust_generation,
    ) = _load_runtime_trust(source)
    return ProxyPolicy(
        policy_id="runtime-egress-proxy",
        proxy_url=_validate_proxy_url(source.get(PROXY_ENVIRONMENT_VARIABLE)),
        ca_cert_path=ca_path,
        leaf_spki_path=spki_path,
        leaf_spki_sha256=spki,
        trust_generation_manifest_path=manifest_path,
        trust_generation=trust_generation,
    )


def proxy_environment(policy: ProxyPolicy) -> dict[str, str]:
    """Return proxy variables while keeping only local driver control direct."""

    local_only = "localhost,127.0.0.1,[::1]"
    return {
        "HTTP_PROXY": policy.proxy_url,
        "HTTPS_PROXY": policy.proxy_url,
        "ALL_PROXY": policy.proxy_url,
        "http_proxy": policy.proxy_url,
        "https_proxy": policy.proxy_url,
        "all_proxy": policy.proxy_url,
        "NO_PROXY": local_only,
        "no_proxy": local_only,
        "CHATDS_EGRESS_POLICY_ID": policy.policy_id,
        PROXY_ENVIRONMENT_VARIABLE: policy.proxy_url,
        "SSL_CERT_FILE": policy.ca_cert_path,
        "REQUESTS_CA_BUNDLE": policy.ca_cert_path,
        "CURL_CA_BUNDLE": policy.ca_cert_path,
        "NODE_EXTRA_CA_CERTS": policy.ca_cert_path,
        "GIT_SSL_CAINFO": policy.ca_cert_path,
        "AWS_CA_BUNDLE": policy.ca_cert_path,
        # Node 22.21+ enables its built-in fetch and core http/https proxy
        # agents only when this runtime-owned startup switch is present.
        "NODE_USE_ENV_PROXY": "1",
        CA_CERTIFICATE_ENVIRONMENT_VARIABLE: policy.ca_cert_path,
        LEAF_SPKI_ENVIRONMENT_VARIABLE: policy.leaf_spki_path,
        TRUST_GENERATION_MANIFEST_ENVIRONMENT_VARIABLE: (
            policy.trust_generation_manifest_path
        ),
        TRUST_GENERATION_ENVIRONMENT_VARIABLE: (
            policy.trust_generation
        ),
    }
