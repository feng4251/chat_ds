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

from skills.path_safety import validate_skill_resource, validate_skill_root


MAX_HTTP_GRANTS_PER_SKILL = 32
MAX_HTTP_GRANT_TEXT_BYTES = 2_000_000
MAX_HTTP_GRANT_RESOURCES = 512
MAX_HTTP_METHOD_DECLARATION_CHARS = 2_048
_TEXT_RESOURCE_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".json", ".toml",
})
_URL_RE = re.compile(r"https://[^\s<>\"'`]+", re.IGNORECASE)
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
    canonical = canonical_https_prefix(match.group(0))
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
) -> tuple[tuple[str, str], ...]:
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
