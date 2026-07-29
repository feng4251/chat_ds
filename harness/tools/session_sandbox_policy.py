"""Runtime-owned network policy for the single session sandbox.

The model may select an already-authorized Skill operation, but it never
selects an execution environment or grants network access.  Exact Skill
capabilities remain method-and-URL-prefix rules all the way to the policy
proxy; origins are derived only for DNS/connect routing and are never the
request-authority boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from tools.context import ToolContext


SESSION_SANDBOX_RUNTIME_PROFILE = "session-sandbox-v1"
MAX_SESSION_SANDBOX_EGRESS_ORIGINS = 128
MAX_SESSION_SANDBOX_EGRESS_RULES = 256
MAX_SESSION_SANDBOX_URL_PREFIX_CHARS = 8_192
SESSION_SANDBOX_METHOD_ORDER = (
    "GET",
    "HEAD",
    "OPTIONS",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
)
SESSION_SANDBOX_METHODS = frozenset(SESSION_SANDBOX_METHOD_ORDER)

_INVALID_ENCODED_PATH = re.compile(
    r"%(?:2e|2f|5c|25|23|3f|0[0-9a-f]|1[0-9a-f]|7f)",
    re.IGNORECASE,
)
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-F]{2})")


class SessionSandboxPolicyError(ValueError):
    """A malformed runtime-owned sandbox policy."""


@dataclass(frozen=True, slots=True)
class SessionSandboxEgressRule:
    """One canonical, content-derived method-and-URL-prefix capability."""

    url_prefix: str
    methods: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "methods": list(self.methods),
            "url_prefix": self.url_prefix,
        }


@dataclass(frozen=True, slots=True)
class SessionSandboxEgressPolicy:
    """Executor/proxy policy projection for one exact Skill."""

    rules: tuple[SessionSandboxEgressRule, ...] = ()
    private_origins: tuple[str, ...] = ()

    @property
    def origins(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            normalize_http_origin(rule.url_prefix)
            for rule in self.rules
        ))

    def rule_payload(self) -> tuple[dict[str, Any], ...]:
        return tuple(rule.as_payload() for rule in self.rules)


def browser_egress_rule_tuples(
    values: Iterable[
        tuple[str, Iterable[str]]
        | dict[str, Any]
        | SessionSandboxEgressRule
    ],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the canonical tuple representation used by ``ToolContext``."""

    rows: list[dict[str, Any] | SessionSandboxEgressRule] = []
    if isinstance(values, (str, bytes, dict)):
        raise SessionSandboxPolicyError(
            "invalid_browser_egress_rules"
        )
    try:
        raw_values = tuple(values)
    except TypeError as exc:
        raise SessionSandboxPolicyError(
            "invalid_browser_egress_rules"
        ) from exc
    for value in raw_values:
        if isinstance(value, SessionSandboxEgressRule):
            rows.append(value)
            continue
        if isinstance(value, dict):
            rows.append(value)
            continue
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or not isinstance(value[0], str)
        ):
            raise SessionSandboxPolicyError(
                "invalid_browser_egress_rules"
            )
        rows.append({
            "url_prefix": value[0],
            "methods": value[1],
        })
    return tuple(
        (rule.url_prefix, rule.methods)
        for rule in normalize_session_sandbox_egress_rules(rows)
    )


def _canonical_http_host(hostname: str) -> str:
    raw_host = hostname.rstrip(".").casefold()
    if (
        not raw_host
        or any(character in raw_host for character in "*?[]%")
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in raw_host
        )
    ):
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_origin"
        )
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise SessionSandboxPolicyError(
                "invalid_session_sandbox_egress_origin"
            ) from exc
        if (
            len(host) > 253
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                    for character in label
                )
                for label in host.split(".")
            )
        ):
            raise SessionSandboxPolicyError(
                "invalid_session_sandbox_egress_origin"
            )
        return host
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def normalize_http_origin(value: str) -> str:
    """Return one canonical ``scheme://host:port`` HTTP(S) origin."""

    if not isinstance(value, str) or not value or len(value) > 8_192:
        raise SessionSandboxPolicyError("invalid_session_sandbox_egress_origin")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed_port = parsed.port
    except ValueError as exc:
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_origin"
        ) from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not hostname
    ):
        raise SessionSandboxPolicyError("invalid_session_sandbox_egress_origin")
    scheme = parsed.scheme.casefold()
    port = (
        parsed_port
        if parsed_port is not None
        else (443 if scheme == "https" else 80)
    )
    if not 1 <= port <= 65_535:
        raise SessionSandboxPolicyError("invalid_session_sandbox_egress_origin")
    host = _canonical_http_host(hostname)
    return f"{scheme}://{host}:{port}"


def normalize_http_url_prefix(value: str) -> str:
    """Return a canonical exact HTTP(S) path/query prefix.

    The canonical wire form uses an explicit port, a non-empty absolute path,
    no credentials/fragment, and raw (not decoded) path/query bytes.  Encoded
    separators, traversal syntax, ambiguous percent escapes, and repeated path
    separators are rejected so a downstream HTTP stack cannot reinterpret the
    request after the proxy comparison.
    """

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_SESSION_SANDBOX_URL_PREFIX_CHARS
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in value
        )
    ):
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_url_prefix"
        )
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_url_prefix"
        ) from exc
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_url_prefix"
        )
    port = (
        parsed_port
        if parsed_port is not None
        else (443 if scheme == "https" else 80)
    )
    if not 1 <= port <= 65_535:
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_url_prefix"
        )
    host = _canonical_http_host(hostname)
    path = parsed.path or "/"
    if (
        not path.startswith("/")
        or "\\" in path
        or "//" in path
        or "{" in path
        or "}" in path
        or _INVALID_PERCENT_ESCAPE.search(path)
        or _INVALID_ENCODED_PATH.search(path)
        or any(
            re.fullmatch(r"\.{1,2}(?:;.*)?", component) is not None
            for component in path.split("/")
        )
    ):
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_url_prefix"
        )
    query = parsed.query
    if (
        "{" in query
        or "}" in query
        or ";" in query
        or _INVALID_PERCENT_ESCAPE.search(query)
        or re.search(
            r"%(?:25|23|0[0-9A-F]|1[0-9A-F]|7F)",
            query,
            re.IGNORECASE,
        )
    ):
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_url_prefix"
        )
    return urlunsplit((
        scheme,
        f"{host}:{port}",
        path,
        query,
        "",
    ))


def normalize_session_sandbox_methods(
    methods: Iterable[str],
) -> tuple[str, ...]:
    """Return a bounded canonical method set."""

    if isinstance(methods, (str, bytes)):
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_methods"
        )
    try:
        values = tuple(methods)
    except TypeError as exc:
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_methods"
        ) from exc
    if (
        not values
        or len(values) > len(SESSION_SANDBOX_METHOD_ORDER)
        or any(
            not isinstance(method, str)
            or method not in SESSION_SANDBOX_METHODS
            for method in values
        )
        or len(set(values)) != len(values)
    ):
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_methods"
        )
    value_set = set(values)
    return tuple(
        method
        for method in SESSION_SANDBOX_METHOD_ORDER
        if method in value_set
    )


def normalize_session_sandbox_egress_rules(
    values: Iterable[dict[str, Any] | SessionSandboxEgressRule],
) -> tuple[SessionSandboxEgressRule, ...]:
    """Validate, canonicalize, aggregate, and bound exact egress rules."""

    if isinstance(values, (str, bytes, dict)):
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_rules"
        )
    try:
        rows = tuple(values)
    except TypeError as exc:
        raise SessionSandboxPolicyError(
            "invalid_session_sandbox_egress_rules"
        ) from exc
    if len(rows) > MAX_SESSION_SANDBOX_EGRESS_RULES:
        raise SessionSandboxPolicyError(
            "session_sandbox_egress_rule_limit_exceeded"
        )
    aggregated: dict[str, set[str]] = {}
    for row in rows:
        if isinstance(row, SessionSandboxEgressRule):
            raw_prefix = row.url_prefix
            raw_methods: Iterable[str] = row.methods
        elif isinstance(row, dict) and set(row) == {
            "methods", "url_prefix",
        }:
            raw_prefix = row.get("url_prefix")
            raw_methods = row.get("methods")  # type: ignore[assignment]
        else:
            raise SessionSandboxPolicyError(
                "invalid_session_sandbox_egress_rules"
            )
        if not isinstance(raw_prefix, str):
            raise SessionSandboxPolicyError(
                "invalid_session_sandbox_egress_rules"
            )
        prefix = normalize_http_url_prefix(raw_prefix)
        if prefix != raw_prefix:
            raise SessionSandboxPolicyError(
                "noncanonical_session_sandbox_egress_rule"
            )
        methods = normalize_session_sandbox_methods(raw_methods)
        aggregated.setdefault(prefix, set()).update(methods)
    return tuple(
        SessionSandboxEgressRule(
            url_prefix=prefix,
            methods=normalize_session_sandbox_methods(methods),
        )
        for prefix, methods in aggregated.items()
    )


def _canonical_request_coordinate(
    url: str,
) -> tuple[str, str, str] | None:
    """Return the canonical origin/path/query used by the egress proxy."""

    try:
        parsed_input = urlsplit(url)
        # URL fragments are browser-local state and never cross the network
        # request boundary.  Drop only that component before applying the
        # proxy-compatible canonicalizer; credentials, malformed authority,
        # path ambiguity, and every other component remain fail-closed.
        without_fragment = urlunsplit((
            parsed_input.scheme,
            parsed_input.netloc,
            parsed_input.path,
            parsed_input.query,
            "",
        ))
        canonical = normalize_http_url_prefix(without_fragment)
        parsed = urlsplit(canonical)
        origin = normalize_http_origin(canonical)
    except (SessionSandboxPolicyError, TypeError, ValueError):
        return None
    return origin, parsed.path or "/", parsed.query


def browser_egress_request_allowed(
    url: str,
    method: str,
    rules: Iterable[
        tuple[str, Iterable[str]]
        | dict[str, Any]
        | SessionSandboxEgressRule
    ],
) -> bool:
    """Match one browser request with the proxy's exact rule semantics."""

    if not isinstance(method, str) or method not in SESSION_SANDBOX_METHODS:
        return False
    coordinate = _canonical_request_coordinate(url)
    if coordinate is None:
        return False
    request_origin, request_path, request_query = coordinate
    try:
        canonical_rules = browser_egress_rule_tuples(rules)
    except SessionSandboxPolicyError:
        return False
    for prefix, methods in canonical_rules:
        if method not in methods:
            continue
        try:
            parsed_prefix = urlsplit(prefix)
            prefix_origin = normalize_http_origin(prefix)
        except (SessionSandboxPolicyError, ValueError):
            continue
        if request_origin != prefix_origin:
            continue
        prefix_path = parsed_prefix.path or "/"
        path_matches = (
            request_path.startswith(prefix_path)
            if prefix_path.endswith("/")
            else request_path == prefix_path
        )
        if not path_matches:
            continue
        prefix_query = parsed_prefix.query
        if prefix_query and not request_query.startswith(prefix_query):
            continue
        return True
    return False


def browser_egress_rule_subset(
    child_rule: tuple[str, Iterable[str]],
    parent_rules: Iterable[
        tuple[str, Iterable[str]]
        | dict[str, Any]
        | SessionSandboxEgressRule
    ],
) -> bool:
    """Prove that every method in one child rule is within a parent rule."""

    try:
        child = browser_egress_rule_tuples((child_rule,))
    except SessionSandboxPolicyError:
        return False
    if len(child) != 1:
        return False
    prefix, methods = child[0]
    return all(
        browser_egress_request_allowed(prefix, method, parent_rules)
        for method in methods
    )


def intersect_browser_egress_rules(
    parent_rules: Iterable[
        tuple[str, Iterable[str]]
        | dict[str, Any]
        | SessionSandboxEgressRule
    ],
    child_rules: Iterable[
        tuple[str, Iterable[str]]
        | dict[str, Any]
        | SessionSandboxEgressRule
    ],
    *,
    require_complete_child: bool = True,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return a bounded child projection without widening parent authority."""

    parent = browser_egress_rule_tuples(parent_rules)
    child = browser_egress_rule_tuples(child_rules)
    retained: list[tuple[str, tuple[str, ...]]] = []
    for prefix, methods in child:
        allowed_methods = tuple(
            method
            for method in methods
            if browser_egress_request_allowed(prefix, method, parent)
        )
        if require_complete_child and allowed_methods != methods:
            raise SessionSandboxPolicyError(
                "browser_egress_child_rule_outside_parent"
            )
        if allowed_methods:
            retained.append((prefix, allowed_methods))
    return browser_egress_rule_tuples(retained)


def browser_context_egress_rules(
    context: ToolContext | None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Project the native browser's complete immutable run authority."""

    if context is None:
        return ()
    rows: list[tuple[str, Iterable[str]]] = [
        (prefix, methods)
        for prefix, methods in context.allowed_browser_egress_rules
    ]
    rows.extend(
        (prefix, methods)
        for _skill_name, prefix, methods in (
            context.allowed_skill_sandbox_egress_rules
        )
    )
    try:
        return browser_egress_rule_tuples(rows)
    except SessionSandboxPolicyError:
        # Runtime-owned malformed state is never repaired into a wider rule.
        return ()


def skill_session_sandbox_egress_policy(
    context: ToolContext | None,
    skill_name: str,
) -> SessionSandboxEgressPolicy:
    """Project exact current-run rules and private subset for one Skill.

    Dynamic browser-private grants are *not* ambient sandbox authority.  An
    origin enters the signed private subset only when an exact URL rule for
    this Skill already targets that origin and the deployment/user-turn
    intersection grants the same origin.
    """

    if context is None:
        return SessionSandboxEgressPolicy()
    rows: list[SessionSandboxEgressRule] = []
    explicit_prefixes: set[str] = set()
    for granted_skill, prefix, methods in (
        context.allowed_skill_sandbox_egress_rules
    ):
        if granted_skill != skill_name:
            continue
        canonical_prefix = normalize_http_url_prefix(prefix)
        if canonical_prefix != prefix:
            raise SessionSandboxPolicyError(
                "noncanonical_session_sandbox_egress_rule"
            )
        rows.append(SessionSandboxEgressRule(
            canonical_prefix,
            normalize_session_sandbox_methods(methods),
        ))
        explicit_prefixes.add(canonical_prefix)

    # Compatibility for older in-process contexts and persisted plans: a
    # legacy exact prefix can grant only safe retrieval methods.  It is never
    # converted to an origin-wide capability and never grants POST.
    for granted_skill, prefix in (
        context.allowed_skill_sandbox_egress_prefixes
    ):
        if granted_skill != skill_name:
            continue
        canonical_prefix = normalize_http_url_prefix(prefix)
        if canonical_prefix in explicit_prefixes:
            continue
        rows.append(SessionSandboxEgressRule(
            canonical_prefix,
            ("GET", "HEAD"),
        ))
    rules = normalize_session_sandbox_egress_rules(rows)
    origins = tuple(dict.fromkeys(
        normalize_http_origin(rule.url_prefix)
        for rule in rules
    ))
    if len(origins) > MAX_SESSION_SANDBOX_EGRESS_ORIGINS:
        raise SessionSandboxPolicyError(
            "session_sandbox_egress_origin_limit_exceeded"
        )
    allowed_private = {
        normalize_http_origin(origin)
        for origin in context.allowed_browser_private_origins
    }
    private_origins = tuple(
        origin for origin in origins if origin in allowed_private
    )
    return SessionSandboxEgressPolicy(
        rules=rules,
        private_origins=private_origins,
    )


def skill_session_sandbox_egress_origins(
    context: ToolContext | None,
    skill_name: str,
) -> tuple[str, ...]:
    """Compatibility projection; exact authorization remains rule-based."""

    return skill_session_sandbox_egress_policy(
        context,
        skill_name,
    ).origins
