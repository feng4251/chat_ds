"""Compile bounded HTTPS GET authority from literal URLs in a Skill package.

The compiler treats package text as authority only for the exact HTTPS URL
prefixes it literally contains.  It does not infer hosts from prose, user
messages, capability names, or model output.  Supporting files are read only
from the already compiled resource closure and through the canonical Skill
resource validator.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from native_security.path_safety import (
    validate_skill_resource,
    validate_skill_root,
)


MAX_HTTP_GRANTS_PER_SKILL = 32
MAX_HTTP_GRANT_TEXT_BYTES = 2_000_000
MAX_HTTP_GRANT_RESOURCES = 512
MAX_HTTP_METHOD_DECLARATION_CHARS = 2_048
_TEXT_RESOURCE_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".json", ".toml",
})
_URL_RE = re.compile(r"https://[^\s<>\"'`]+", re.IGNORECASE)
_SANDBOX_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
# A closing brace is deliberately not treated as surrounding Markdown: it can
# be the meaningful end of a URI-template path segment.  Complete template
# segments are compiled below; unmatched/partial braces are rejected rather
# than silently trimmed into literal authority.
_TRAILING_MARKDOWN = ").,;:!?]，。；：！？"
_URI_TEMPLATE_PATH_SEGMENT = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_NEGATED_HTTP_POST_RE = re.compile(
    r"(?:"
    r"\b(?:do\s+not|don['’]?t|never|must\s+not|should\s+not|"
    r"cannot|can['’]?t|no)\b.{0,32}\bPOST\b|"
    r"\bPOST\b.{0,24}\b(?:is\s+not|not\s+allowed|"
    r"forbidden|prohibited)\b|"
    r"(?:不要|不得|禁止|切勿|不能|不可|无需).{0,16}POST"
    r")",
    re.IGNORECASE,
)
_GRAPHQL_ENDPOINT_DECLARATION_RE = re.compile(
    r"(?:\bGraphQL\b.{0,32}\b(?:API|endpoint)\b|"
    r"\b(?:API|endpoint)\b.{0,32}\bGraphQL\b)",
    re.IGNORECASE,
)
_GRAPHQL_NON_ENDPOINT_RE = re.compile(
    r"\b(?:browser|playground|explorer|docs?|documentation|schema)\b",
    re.IGNORECASE,
)
_SANDBOX_HTTP_METHOD_ORDER = (
    "GET",
    "HEAD",
    "OPTIONS",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
)
_EXAMPLE_HOST_SUFFIXES = (
    ".example", ".invalid", ".localhost", "example.com", "example.org", "example.net",
)
# Reject both direct and double-encoded path separators/traversal syntax.  A
# literal percent (``%25``) is deliberately excluded from this narrow bridge:
# otherwise an upstream proxy that decodes twice can reinterpret a path only
# after the grant comparison has already succeeded.
_ENCODED_PATH_SEPARATORS = re.compile(
    r"%(?:2e|2f|5c|25|23|3f|3b|0[0-9a-f]|1[0-9a-f]|7f)",
    re.IGNORECASE,
)


def _canonical_netloc(host: str) -> str:
    """Render a canonical hostname or IP literal for ``urlunsplit``."""

    return f"[{host}]" if ":" in host else host


def _canonical_https_parts(
    value: Any,
    *,
    max_length: int,
    allow_query: bool,
) -> tuple[str, str, str] | None:
    """Return canonical ``(host, path, query)`` for one strict HTTPS URL.

    This validator is shared by grant compilation and runtime requests.  The
    path is intentionally conservative because comparisons occur before a
    remote server/proxy performs its own URL decoding or normalization.
    """

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or candidate != value
        or len(candidate) > max_length
        or "{" in candidate
        or "}" in candidate
        or any(ch.isspace() for ch in candidate)
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate)
    ):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        return None
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    except (UnicodeError, ValueError):
        return None
    if not host:
        return None
    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        return None

    path = parsed.path or "/"
    # Invalid percent escapes, raw/encoded backslashes, repeated separators,
    # encoded dots/slashes, and literal dot segments are rejected before both
    # grant matching and aiohttp/yarl canonicalization.
    percent_without_escape = re.search(r"%(?![0-9a-fA-F]{2})", path)
    if (
        "\\" in path
        or "//" in path
        or " " in path
        or "{" in path
        or "}" in path
        or percent_without_escape
        or _ENCODED_PATH_SEPARATORS.search(path)
        or any(
            re.fullmatch(r"\.{1,2}(?:;.*)?", part) is not None
            for part in path.split("/")
        )
    ):
        return None
    query = parsed.query
    if query and (
        ";" in query
        or "{" in query
        or "}" in query
        or re.search(r"%(?![0-9a-fA-F]{2})", query)
        # A literal/double-encoding percent escape can change a key only after
        # the one canonical parse used by the credential policy. Reject it
        # rather than trying to predict each upstream framework's decode count.
        or re.search(r"%25", query, re.IGNORECASE)
        or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", query, re.IGNORECASE)
    ):
        return None
    return host, path, query


def _compile_uri_template_path_prefix(candidate: str) -> str | None:
    """Compile a strict path-segment URI template to one literal prefix.

    Only a complete ``{identifier}`` occupying an entire path segment is
    recognized.  The prefix ends at the literal segment boundary immediately
    before the first placeholder, so ``/studies/{nct_id}`` grants
    ``/studies/``.  Braces anywhere else are invalid instead of being treated
    as literal URL characters.
    """

    if "{" not in candidate and "}" not in candidate:
        return candidate
    try:
        parsed = urlsplit(candidate)
    except (TypeError, ValueError):
        return None
    if any(
        "{" in component or "}" in component
        for component in (
            parsed.scheme,
            parsed.netloc,
            parsed.query,
            parsed.fragment,
        )
    ):
        return None

    segments = parsed.path.split("/")
    first_placeholder: int | None = None
    validation_segments = list(segments)
    for index, segment in enumerate(segments):
        if "{" not in segment and "}" not in segment:
            continue
        if _URI_TEMPLATE_PATH_SEGMENT.fullmatch(segment) is None:
            return None
        # Validate the *whole* template path, including literal suffixes after
        # the first placeholder, before truncating it to an authority prefix.
        # A benign stand-in lets the common canonicalizer retain traversal,
        # repeated-separator, encoding, host, and query safety checks.
        validation_segments[index] = "template-value"
        if first_placeholder is None:
            first_placeholder = index
    if first_placeholder is None:
        return None

    validation_candidate = urlunsplit((
        parsed.scheme,
        parsed.netloc,
        "/".join(validation_segments),
        parsed.query,
        parsed.fragment,
    ))
    if _canonical_https_parts(
        validation_candidate,
        max_length=2_048,
        allow_query=True,
    ) is None:
        return None

    prefix_path = "/".join(segments[:first_placeholder])
    if not prefix_path.endswith("/"):
        prefix_path += "/"
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        prefix_path or "/",
        parsed.query,
        parsed.fragment,
    ))


def canonical_https_request_url(value: Any) -> str | None:
    """Return one strict credential-free-capable HTTPS request URL.

    Query credential policy is enforced by the runtime tool because it needs
    to report a specific diagnostic; this shared boundary keeps host/path
    canonicalization identical to grant compilation.
    """

    parts = _canonical_https_parts(
        value,
        max_length=8_192,
        allow_query=True,
    )
    if parts is None:
        return None
    host, path, query = parts
    return urlunsplit(("https", _canonical_netloc(host), path, query, ""))


def canonical_https_prefix(value: Any) -> str | None:
    """Return one conservative query-free HTTPS prefix, or ``None``."""

    if not isinstance(value, str):
        return None
    candidate = value.strip().rstrip(_TRAILING_MARKDOWN)
    candidate = _compile_uri_template_path_prefix(candidate)
    if candidate is None:
        return None
    parts = _canonical_https_parts(
        candidate,
        max_length=2_048,
        allow_query=True,
    )
    if parts is None:
        return None
    host, path, _query = parts
    if (
        host == "localhost"
        or any(
            host == suffix.lstrip(".")
            or host.endswith("." + suffix.lstrip("."))
            for suffix in _EXAMPLE_HOST_SUFFIXES
        )
    ):
        return None
    return urlunsplit(("https", _canonical_netloc(host), path, "", ""))


def canonical_sandbox_http_prefix(value: Any) -> str | None:
    """Return one exact canonical HTTP(S) path/query prefix for a sandbox.

    Unlike the direct ``skill_http_*`` bridge this compiler may retain HTTP,
    private addresses, non-default ports, and a literal query prefix.  Those
    coordinates still confer no ambient private-network access: the runtime
    separately intersects their derived origins with the deployment/user-turn
    private grant before signing the executor policy.
    """

    if not isinstance(value, str):
        return None
    candidate = value.strip().rstrip(_TRAILING_MARKDOWN)
    if not candidate:
        return None
    if "{" in candidate or "}" in candidate:
        try:
            parsed = urlsplit(candidate)
        except (TypeError, ValueError):
            return None
        if any(
            "{" in component or "}" in component
            for component in (
                parsed.scheme,
                parsed.netloc,
                parsed.query,
                parsed.fragment,
            )
        ):
            return None
        segments = parsed.path.split("/")
        first_placeholder: int | None = None
        for index, segment in enumerate(segments):
            if "{" not in segment and "}" not in segment:
                continue
            if _URI_TEMPLATE_PATH_SEGMENT.fullmatch(segment) is None:
                return None
            if first_placeholder is None:
                first_placeholder = index
        if first_placeholder is None:
            return None
        prefix_path = "/".join(segments[:first_placeholder])
        if not prefix_path.endswith("/"):
            prefix_path += "/"
        candidate = urlunsplit((
            parsed.scheme,
            parsed.netloc,
            prefix_path or "/",
            parsed.query,
            "",
        ))
    try:
        from native_security.session_sandbox_policy import (
            normalize_http_url_prefix,
        )

        canonical = normalize_http_url_prefix(candidate)
    except (ImportError, ValueError):
        return None
    try:
        host = (urlsplit(canonical).hostname or "").casefold()
    except ValueError:
        return None
    if (
        host == "localhost"
        or any(
            host == suffix.lstrip(".")
            or host.endswith("." + suffix.lstrip("."))
            for suffix in _EXAMPLE_HOST_SUFFIXES
        )
    ):
        return None
    return canonical


def extract_literal_https_prefixes(texts: Iterable[str]) -> tuple[str, ...]:
    """Extract a stable, bounded set of literal HTTPS prefixes."""

    prefixes: list[str] = []
    total_bytes = 0
    for raw_text in texts:
        if not isinstance(raw_text, str) or not raw_text:
            continue
        encoded = raw_text.encode("utf-8", errors="replace")
        remaining = MAX_HTTP_GRANT_TEXT_BYTES - total_bytes
        if remaining <= 0:
            break
        if len(encoded) > remaining:
            encoded = encoded[:remaining]
            raw_text = encoded.decode("utf-8", errors="ignore")
        total_bytes += len(encoded)
        for match in _URL_RE.finditer(raw_text):
            prefix = canonical_https_prefix(match.group(0))
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)
                if len(prefixes) >= MAX_HTTP_GRANTS_PER_SKILL:
                    return tuple(prefixes)
    return tuple(prefixes)


def _url_has_explicit_json_post_declaration(
    text: str,
    match: re.Match[str],
) -> bool:
    """Bind POST authority to one unambiguous line or paragraph declaration.

    HTTP method names and GraphQL are protocol literals rather than a
    language-specific verb dictionary. A context containing multiple URLs is
    deliberately ambiguous and cannot confer POST authority on any of them.
    """

    window_start = max(
        0, match.start() - MAX_HTTP_METHOD_DECLARATION_CHARS - 2
    )
    window_end = min(
        len(text), match.end() + MAX_HTTP_METHOD_DECLARATION_CHARS + 2
    )
    before = text[window_start:match.start()]
    after = text[match.end():window_end]

    if _NEGATED_HTTP_POST_RE.search(text[window_start:window_end]):
        return False

    contexts: list[tuple[int, int]] = []
    line_break = before.rfind("\n")
    next_line_break = after.find("\n")
    if line_break >= 0 or window_start == 0:
        if next_line_break >= 0 or window_end == len(text):
            contexts.append((
                window_start + line_break + 1,
                (
                    match.end() + next_line_break
                    if next_line_break >= 0 else len(text)
                ),
            ))

    paragraph_breaks = tuple(re.finditer(r"\n[ \t]*\n", before))
    previous_paragraph = paragraph_breaks[-1] if paragraph_breaks else None
    next_paragraph = re.search(r"\n[ \t]*\n", after)
    if previous_paragraph is not None or window_start == 0:
        if next_paragraph is not None or window_end == len(text):
            contexts.append((
                (
                    window_start + previous_paragraph.end()
                    if previous_paragraph is not None else 0
                ),
                (
                    match.end() + next_paragraph.start()
                    if next_paragraph is not None else len(text)
                ),
            ))

    # A canonical endpoint whose final path segment is exactly ``graphql`` is
    # self-declaring. Do not extend this to adjacent documentation/browser
    # paths such as ``/graphql/browser``.
    canonical = (
        canonical_https_prefix(match.group(0))
        or canonical_sandbox_http_prefix(match.group(0))
    )
    if canonical is not None:
        endpoint_path = (urlsplit(canonical).path or "/").rstrip("/")
        if endpoint_path.rsplit("/", 1)[-1].casefold() == "graphql":
            return True

    for start, end in contexts:
        context = text[start:end]
        if not context or len(context) > MAX_HTTP_METHOD_DECLARATION_CHARS:
            continue
        if len(tuple(_URL_RE.finditer(context))) != 1:
            continue
        if re.search(r"\bPOST\b", context, re.IGNORECASE):
            return True
        if (
            _GRAPHQL_ENDPOINT_DECLARATION_RE.search(context)
            and not _GRAPHQL_NON_ENDPOINT_RE.search(context)
        ):
            return True
    return False


def _bounded_single_url_contexts(
    text: str,
    match: re.Match[str],
) -> tuple[tuple[str, int, int], ...]:
    """Return bounded line/paragraph contexts that bind one URL occurrence.

    A method token in prose must never leak onto a neighbouring endpoint.
    Contexts larger than the compiler bound or containing another literal
    HTTP(S) URL are therefore ineligible for method authority.
    """

    spans: list[tuple[int, int]] = []
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    if line_end < 0:
        line_end = len(text)
    spans.append((line_start, line_end))

    preceding = tuple(re.finditer(r"\n[ \t]*\n", text[:match.start()]))
    paragraph_start = preceding[-1].end() if preceding else 0
    following = re.search(r"\n[ \t]*\n", text[match.end():])
    paragraph_end = (
        match.end() + following.start()
        if following is not None else len(text)
    )
    spans.append((paragraph_start, paragraph_end))

    contexts: list[tuple[str, int, int]] = []
    for start, end in dict.fromkeys(spans):
        if (
            start < 0
            or end < match.end()
            or end - start > MAX_HTTP_METHOD_DECLARATION_CHARS
        ):
            continue
        context = text[start:end]
        occurrences = tuple(_SANDBOX_URL_RE.finditer(context))
        relative_start = match.start() - start
        relative_end = match.end() - start
        if (
            len(occurrences) != 1
            or occurrences[0].start() != relative_start
        ):
            continue
        contexts.append((context, relative_start, relative_end))
    return tuple(contexts)


def _context_negates_http_method(context: str, method: str) -> bool:
    """Return whether a bounded declaration explicitly denies one method."""

    token = re.escape(method)
    return bool(
        re.search(
            rf"(?:"
            rf"\b(?:do\s+not|don['’]?t|never|must\s+not|should\s+not|"
            rf"cannot|can['’]?t|without|no)\b.{{0,40}}\b{token}\b|"
            rf"\b{token}\b.{{0,32}}\b(?:is\s+not|not\s+allowed|"
            rf"forbidden|prohibited|unsupported|disabled)\b|"
            rf"(?:不要|不得|禁止|切勿|不能|不可|无需).{{0,20}}{token}"
            rf")",
            context,
            re.IGNORECASE,
        )
    )


def _context_explicitly_binds_http_method(
    context: str,
    relative_start: int,
    relative_end: int,
    method: str,
) -> bool:
    """Recognize deterministic method declarations for one literal URL."""

    token = re.escape(method)
    declaration_context = (
        context[:relative_start]
        + (" " * (relative_end - relative_start))
        + context[relative_end:]
    )
    if _context_negates_http_method(declaration_context, method):
        return False

    # Exact command/code shapes.  The single-URL context invariant above
    # makes the method-to-URL binding unambiguous.
    if re.search(r"\bcurl(?:\.exe)?\b", context, re.IGNORECASE) and re.search(
        rf"(?:^|\s)(?:-X|--request(?:=|\s))\s*{token}\b",
        context,
        re.IGNORECASE,
    ):
        return True
    before = context[:relative_start]
    if re.search(
        rf"\b(?:requests|httpx|session|client)\s*\.\s*{token}\s*"
        rf"\(\s*(?:url\s*=\s*)?(?:[rubf]{{0,2}})?[\"'`]\s*$",
        before,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"\b(?:method|http_method|httpMethod)\s*[:=]\s*"
        rf"[\"'`]?\s*{token}\b",
        context,
        re.IGNORECASE,
    ):
        return True

    # Protocol notation immediately adjacent to the URL, including table
    # rows such as ``| PATCH | https://... |`` and concise instructions such
    # as ``use DELETE request at https://...``.  This is intentionally much
    # narrower than inferring a method from arbitrary prose in the paragraph.
    left = before[-128:]
    right = context[relative_end:relative_end + 128]
    if re.search(
        rf"(?:^|[\s|`(])(?:use\s+|using\s+|via\s+)?{token}"
        rf"(?:\s+(?:HTTP\s+)?(?:request|method|operation|endpoint))?"
        rf"(?:\s+(?:to|at|via|for|调用|请求|方法|到))?"
        rf"[\s:|=`'\"—–-]*$",
        left,
        re.IGNORECASE,
    ):
        return True
    if re.match(
        rf"^[\s:|=`'\"),.;—–-]*(?:using\s+|via\s+|method\s*[:=]\s*)?"
        rf"{token}\b",
        right,
        re.IGNORECASE,
    ):
        return True
    return False


def _explicit_sandbox_http_methods_for_url(
    text: str,
    match: re.Match[str],
) -> set[str]:
    """Compile non-default methods from exact code/protocol declarations."""

    methods: set[str] = set()
    contexts = _bounded_single_url_contexts(text, match)
    for method in _SANDBOX_HTTP_METHOD_ORDER:
        if any(
            _context_explicitly_binds_http_method(
                context,
                relative_start,
                relative_end,
                method,
            )
            for context, relative_start, relative_end in contexts
        ):
            methods.add(method)
    # Preserve the existing conservative POST/GraphQL classifier.  It also
    # recognizes a canonical ``.../graphql`` endpoint whose path is
    # self-declaring, while explicitly rejecting POST negations.
    if _url_has_explicit_json_post_declaration(text, match):
        methods.add("POST")
    return methods


def _negated_sandbox_http_methods_for_url(
    text: str,
    match: re.Match[str],
) -> set[str]:
    """Return methods explicitly denied for this single URL occurrence."""

    contexts = _bounded_single_url_contexts(text, match)
    return {
        method
        for method in _SANDBOX_HTTP_METHOD_ORDER
        if any(
            _context_negates_http_method(
                (
                    context[:relative_start]
                    + (" " * (relative_end - relative_start))
                    + context[relative_end:]
                ),
                method,
            )
            for context, relative_start, relative_end in contexts
        )
    }


def extract_literal_https_post_prefixes(
    texts: Iterable[str],
) -> tuple[str, ...]:
    """Extract only literal prefixes with an explicit JSON-POST declaration."""

    prefixes: list[str] = []
    total_bytes = 0
    for raw_text in texts:
        if not isinstance(raw_text, str) or not raw_text:
            continue
        encoded = raw_text.encode("utf-8", errors="replace")
        remaining = MAX_HTTP_GRANT_TEXT_BYTES - total_bytes
        if remaining <= 0:
            break
        if len(encoded) > remaining:
            encoded = encoded[:remaining]
            raw_text = encoded.decode("utf-8", errors="ignore")
        total_bytes += len(encoded)
        for match in _URL_RE.finditer(raw_text):
            if not _url_has_explicit_json_post_declaration(raw_text, match):
                continue
            prefix = canonical_https_prefix(match.group(0))
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)
                if len(prefixes) >= MAX_HTTP_GRANTS_PER_SKILL:
                    return tuple(prefixes)
    return tuple(prefixes)


def extract_literal_sandbox_egress_rules(
    texts: Iterable[str],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Compile exact sandbox URL prefixes with explicit method sets.

    Every literal URL grants retrieval only (GET/HEAD). POST is added solely
    for the same URL occurrence when the existing bounded POST/GraphQL
    declaration classifier proves it. Multiple occurrences are aggregated by
    canonical prefix without widening path or query scope.
    """

    methods_by_prefix: dict[str, set[str]] = {}
    total_bytes = 0
    for raw_text in texts:
        if not isinstance(raw_text, str) or not raw_text:
            continue
        encoded = raw_text.encode("utf-8", errors="replace")
        remaining = MAX_HTTP_GRANT_TEXT_BYTES - total_bytes
        if remaining <= 0:
            break
        if len(encoded) > remaining:
            encoded = encoded[:remaining]
            raw_text = encoded.decode("utf-8", errors="ignore")
        total_bytes += len(encoded)
        for match in _SANDBOX_URL_RE.finditer(raw_text):
            prefix = canonical_sandbox_http_prefix(match.group(0))
            if prefix is None:
                continue
            occurrence_methods = {"GET", "HEAD"}.difference(
                _negated_sandbox_http_methods_for_url(raw_text, match)
            )
            occurrence_methods.update(
                _explicit_sandbox_http_methods_for_url(raw_text, match)
            )
            if occurrence_methods:
                methods_by_prefix.setdefault(prefix, set()).update(
                    occurrence_methods
                )
            if len(methods_by_prefix) > MAX_HTTP_GRANTS_PER_SKILL:
                # Authority compilation is atomic. Never return a
                # hash/order-dependent partial set after an overflow.
                return ()
    return tuple(
        (
            prefix,
            tuple(
                method
                for method in _SANDBOX_HTTP_METHOD_ORDER
                if method in methods
            ),
        )
        for prefix, methods in sorted(methods_by_prefix.items())
    )


def compile_user_sandbox_egress_urls(
    user_text: str,
) -> tuple[str, ...]:
    """Return method-free exact URL identities from bounded user-authored text.

    These values are deliberately *not* egress rules.  A URL becomes network
    authority only immediately before an exact content-addressed Skill
    entrypoint is dispatched, after one manifest binding is proven to select
    that same URL from the actual invocation arguments.
    """

    from native_security.session_sandbox_policy import (
        SessionSandboxPolicyError,
        normalize_http_url_prefix,
    )

    if not isinstance(user_text, str):
        return ()
    encoded = user_text.encode("utf-8", errors="replace")
    if len(encoded) > MAX_HTTP_GRANT_TEXT_BYTES:
        user_text = encoded[:MAX_HTTP_GRANT_TEXT_BYTES].decode(
            "utf-8",
            errors="ignore",
        )
    urls: list[str] = []
    for match in _SANDBOX_URL_RE.finditer(user_text):
        raw = match.group(0).rstrip(_TRAILING_MARKDOWN + "}》】）")
        try:
            parsed = urlsplit(raw)
            # Fragments are browser-local and never appear on the HTTP wire.
            canonical = normalize_http_url_prefix(urlunsplit((
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.query,
                "",
            )))
        except (TypeError, ValueError, SessionSandboxPolicyError):
            continue
        if canonical not in urls:
            urls.append(canonical)
        if len(urls) >= MAX_HTTP_GRANTS_PER_SKILL:
            break
    return tuple(urls)


def _argv_user_url_value(
    arguments: tuple[str, ...],
    selector: str | int,
) -> str | None:
    """Resolve one exact argv selector without guessing or last-value wins."""

    if type(selector) is int:
        return arguments[selector] if selector < len(arguments) else None
    if not isinstance(selector, str) or not selector.startswith("--"):
        return None
    matches: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == selector:
            if index + 1 >= len(arguments):
                return None
            matches.append(arguments[index + 1])
            index += 2
            continue
        prefix = selector + "="
        if value.startswith(prefix):
            matches.append(value[len(prefix):])
        index += 1
    # Duplicate flags are ambiguous even when both values happen to match.
    return matches[0] if len(matches) == 1 else None


def compile_user_sandbox_egress_rules(
    authorized_urls: Iterable[str],
    bindings: Iterable[dict[str, Any]],
    *,
    invocation: dict[str, Any],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Compile only rules proven by one exact, actual Skill invocation.

    ``authorized_urls`` is the runtime-owned method-free ledger compiled from
    the bounded current user context.  ``bindings`` comes from the selected
    immutable entrypoint manifest.  ``invocation`` is built by the runner from
    already validated actual argv/callable/payload data.  No candidate rule is
    safe to preinstall before all three identities intersect.
    """

    from native_security.session_sandbox_policy import (
        SessionSandboxPolicyError,
        normalize_http_origin,
        normalize_http_url_prefix,
        normalize_session_sandbox_methods,
    )

    if (
        isinstance(authorized_urls, (str, bytes, dict))
        or not isinstance(invocation, dict)
        or set(invocation).difference({
            "source",
            "args",
            "callable",
            "parameters",
            "command",
            "payload",
        })
    ):
        return ()
    try:
        raw_urls = tuple(authorized_urls)
        binding_rows = tuple(bindings)
    except TypeError:
        return ()
    if (
        not raw_urls
        or len(raw_urls) > MAX_HTTP_GRANTS_PER_SKILL
        or any(not isinstance(value, str) for value in raw_urls)
        or not binding_rows
        or len(binding_rows) > 8
        or any(not isinstance(binding, dict) for binding in binding_rows)
    ):
        return ()
    canonical_urls: set[str] = set()
    try:
        for value in raw_urls:
            canonical = normalize_http_url_prefix(value)
            if canonical != value:
                return ()
            canonical_urls.add(canonical)
    except SessionSandboxPolicyError:
        return ()
    if len(canonical_urls) != len(raw_urls):
        return ()

    invocation_source = invocation.get("source")
    if invocation_source not in {"argv", "python", "stdin_json"}:
        return ()
    argv: tuple[str, ...] = ()
    parameters: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    invocation_callable: str | None = None
    invocation_command: str | None = None
    if invocation_source == "argv":
        raw_args = invocation.get("args")
        if (
            set(invocation) != {"source", "args"}
            or not isinstance(raw_args, (list, tuple))
            or len(raw_args) > 64
            or any(not isinstance(value, str) for value in raw_args)
        ):
            return ()
        argv = tuple(raw_args)
    elif invocation_source == "python":
        raw_parameters = invocation.get("parameters")
        invocation_callable = invocation.get("callable")
        if (
            set(invocation) != {"source", "callable", "parameters"}
            or not isinstance(invocation_callable, str)
            or not isinstance(raw_parameters, dict)
            or len(raw_parameters) > 128
            or any(not isinstance(key, str) for key in raw_parameters)
        ):
            return ()
        parameters = raw_parameters
    else:
        raw_payload = invocation.get("payload")
        invocation_command = invocation.get("command")
        if (
            set(invocation) != {"source", "command", "payload"}
            or not isinstance(invocation_command, str)
            or not isinstance(raw_payload, dict)
            or len(raw_payload) > 128
            or any(not isinstance(key, str) for key in raw_payload)
        ):
            return ()
        payload = raw_payload

    methods_by_prefix: dict[str, set[str]] = {}
    for binding in binding_rows:
        if set(binding).difference({
            "source",
            "selector",
            "methods",
            "scope",
            "callable",
            "command",
        }):
            return ()
        source = binding.get("source")
        selector = binding.get("selector")
        scope = binding.get("scope")
        callable_name = binding.get("callable")
        command = binding.get("command")
        if (
            source not in {"argv", "python", "stdin_json"}
            or scope not in {"url", "origin"}
            or isinstance(selector, bool)
            or not isinstance(selector, (str, int))
            or (source == "argv" and not (
                (
                    type(selector) is int
                    and 0 <= selector < 64
                )
                or (
                    isinstance(selector, str)
                    and re.fullmatch(
                        r"--[A-Za-z0-9][A-Za-z0-9_-]{0,126}",
                        selector,
                    )
                    is not None
                )
            ))
            or (source == "argv" and (
                callable_name is not None or command is not None
            ))
            or (source == "python" and (
                not isinstance(selector, str)
                or re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_]{0,127}",
                    selector,
                )
                is None
                or not isinstance(callable_name, str)
                or re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_]{0,127}"
                    r"(?:\.[A-Za-z][A-Za-z0-9_]{0,127})?",
                    callable_name,
                )
                is None
                or command is not None
            ))
            or (source == "stdin_json" and (
                not isinstance(selector, str)
                or re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_]{0,127}",
                    selector,
                )
                is None
                or not isinstance(command, str)
                or re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_]{0,127}",
                    command,
                )
                is None
                or callable_name is not None
            ))
        ):
            return ()
        try:
            methods = normalize_session_sandbox_methods(
                binding.get("methods")
            )
        except SessionSandboxPolicyError:
            return ()

        # Validate every binding atomically, but only the actual invocation's
        # source/callable/command may contribute a destination.
        if source != invocation_source:
            continue
        value: Any = None
        if source == "argv":
            value = _argv_user_url_value(argv, selector)
        elif source == "python" and callable_name == invocation_callable:
            value = parameters.get(str(selector))
        elif source == "stdin_json" and command == invocation_command:
            value = payload.get(str(selector))
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value)
            canonical = normalize_http_url_prefix(urlunsplit((
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.query,
                "",
            )))
        except (TypeError, ValueError, SessionSandboxPolicyError):
            continue
        # Exact equality prevents a different path, query, origin, or scheme
        # in model-authored invocation arguments from borrowing user intent.
        if canonical not in canonical_urls:
            continue
        try:
            prefix = (
                normalize_http_url_prefix(
                    normalize_http_origin(canonical) + "/"
                )
                if scope == "origin"
                else canonical
            )
        except SessionSandboxPolicyError:
            continue
        methods_by_prefix.setdefault(prefix, set()).update(methods)
        if len(methods_by_prefix) > MAX_HTTP_GRANTS_PER_SKILL:
            return ()
    return tuple(
        (
            prefix,
            tuple(
                method
                for method in _SANDBOX_HTTP_METHOD_ORDER
                if method in methods
            ),
        )
        for prefix, methods in sorted(methods_by_prefix.items())
    )


def _text_explicitly_references_resource(text: str, relative_path: str) -> bool:
    """Whether package text names one exact allowed relative resource path."""

    if not text or not relative_path:
        return False
    # The boundaries prevent ``references/api.md.bak`` or a longer directory
    # path from authorizing ``references/api.md``.  ``./`` is the only lexical
    # alias accepted; basenames and directory inference are never used.
    for candidate in (relative_path, f"./{relative_path}"):
        pattern = (
            r"(?<![A-Za-z0-9_.\-/])"
            + re.escape(candidate)
            + r"(?![A-Za-z0-9_.\-/])"
        )
        if re.search(pattern, text):
            return True
    return False


def _compile_loaded_skill_http_grants(
    skill_name: str,
    loaded_package: dict[str, Any],
    allowed_resource_paths: Iterable[str] = (),
    *,
    post_only: bool = False,
    sandbox_rules: bool = False,
) -> tuple[tuple[Any, ...], ...]:
    """Compile ``(skill_name, prefix)`` grants from a loaded session Skill.

    The caller must already have selected the package and compiled its exact
    resource closure.  A package outside the session scope never creates
    network authority.
    """

    if (
        not isinstance(skill_name, str)
        or not skill_name
        or not isinstance(loaded_package, dict)
        or loaded_package.get("_chatds_scope") != "session"
    ):
        return ()
    # Only the loaded SKILL.md body is the root of network authority. Catalog
    # descriptions, UI summaries, and other derived metadata are not package
    # instructions and cannot seed either a URL grant or a resource traversal.
    texts: list[str] = []
    content = loaded_package.get("content")
    if isinstance(content, str):
        texts.append(content)

    # Establish a deterministic, bounded resource list before opening any
    # package files.  Returning no grants on overflow avoids minting a
    # hash-order-dependent partial authority set.
    resource_paths: set[str] = set()
    for path in allowed_resource_paths:
        resource_paths.add(str(path))
        if len(resource_paths) > MAX_HTTP_GRANT_RESOURCES:
            return ()

    graph = loaded_package.get("resource_graph")
    root_value = graph.get("skill_root") if isinstance(graph, dict) else None
    root_check = validate_skill_root(Path(str(root_value))) if root_value else None
    package_root = root_check.path if root_check is not None and root_check.valid else None
    if package_root is not None:
        # Include main content/description in the same cumulative byte budget
        # as supporting files.  Read only the remaining bytes from disk rather
        # than accumulating every <=2MB resource before the extractor clips.
        consumed = sum(
            min(len(text.encode("utf-8", errors="replace")), MAX_HTTP_GRANT_TEXT_BYTES)
            for text in texts
        )
        remaining = max(0, MAX_HTTP_GRANT_TEXT_BYTES - consumed)
        candidates = [
            raw_path
            for raw_path in sorted(
                resource_paths,
                key=lambda item: (item.casefold(), item),
            )
            if raw_path not in {"", "SKILL.md", "__manifest__"}
            and PurePosixPath(raw_path).suffix.casefold()
            in _TEXT_RESOURCE_SUFFIXES
        ]
        scanned_resources: set[str] = set()
        searchable_text = "\n".join(texts)
        # Follow only exact literal references from SKILL.md/metadata and then
        # from each already-authorized supporting text.  ``allowed`` remains a
        # necessary outer boundary, but an inventory entry alone never mints
        # network authority.
        while remaining > 0:
            referenced = [
                raw_path
                for raw_path in candidates
                if raw_path not in scanned_resources
                and _text_explicitly_references_resource(
                    searchable_text,
                    raw_path,
                )
            ]
            if not referenced:
                break
            newly_loaded: list[str] = []
            for raw_path in referenced:
                scanned_resources.add(raw_path)
                if remaining <= 0:
                    break
                checked = validate_skill_resource(
                    package_root,
                    raw_path,
                    expected_kind="file",
                    require_relative=True,
                )
                if not checked.valid or checked.path is None:
                    continue
                try:
                    size = checked.path.stat().st_size
                    if size > MAX_HTTP_GRANT_TEXT_BYTES:
                        continue
                    with checked.path.open("rb") as stream:
                        encoded = stream.read(remaining)
                    # Count bytes even when an ostensibly textual resource is
                    # not valid UTF-8, so malformed earlier files cannot induce
                    # an unbounded scan of later resources.
                    remaining -= len(encoded)
                    decoded = encoded.decode("utf-8")
                except (OSError, UnicodeError):
                    continue
                texts.append(decoded)
                newly_loaded.append(decoded)
            if not newly_loaded:
                continue
            searchable_text = "\n".join(newly_loaded)

    if sandbox_rules:
        return tuple(
            (skill_name, prefix, methods)
            for prefix, methods in extract_literal_sandbox_egress_rules(texts)
        )
    extractor = (
        extract_literal_https_post_prefixes
        if post_only else extract_literal_https_prefixes
    )
    return tuple((skill_name, prefix) for prefix in extractor(texts))


def compile_loaded_skill_http_grants(
    skill_name: str,
    loaded_package: dict[str, Any],
    allowed_resource_paths: Iterable[str] = (),
) -> tuple[tuple[str, str], ...]:
    """Compile credential-free HTTPS GET prefixes from one exact Skill closure."""

    return _compile_loaded_skill_http_grants(
        skill_name,
        loaded_package,
        allowed_resource_paths,
    )


def compile_loaded_skill_http_post_grants(
    skill_name: str,
    loaded_package: dict[str, Any],
    allowed_resource_paths: Iterable[str] = (),
) -> tuple[tuple[str, str], ...]:
    """Compile only exact prefixes explicitly declared for JSON POST/GraphQL."""

    return _compile_loaded_skill_http_grants(
        skill_name,
        loaded_package,
        allowed_resource_paths,
        post_only=True,
    )


def compile_loaded_skill_sandbox_egress_rules(
    skill_name: str,
    loaded_package: dict[str, Any],
    allowed_resource_paths: Iterable[str] = (),
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Compile exact method-and-prefix rules for Skill sandbox execution."""

    rows = _compile_loaded_skill_http_grants(
        skill_name,
        loaded_package,
        allowed_resource_paths,
        sandbox_rules=True,
    )
    return tuple(
        (str(row[0]), str(row[1]), tuple(row[2]))
        for row in rows
        if len(row) == 3
    )
