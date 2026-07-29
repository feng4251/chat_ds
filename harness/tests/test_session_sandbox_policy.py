from __future__ import annotations

import unittest

from tools.context import ToolContext
from tools.session_sandbox_policy import (
    MAX_SESSION_SANDBOX_EGRESS_ORIGINS,
    SessionSandboxEgressRule,
    SessionSandboxPolicyError,
    normalize_http_origin,
    normalize_http_url_prefix,
    normalize_session_sandbox_egress_rules,
    skill_session_sandbox_egress_policy,
    skill_session_sandbox_egress_origins,
)
from tools.registry import get_schemas
from tools.skill_process import RUN_SKILL_PROCESS_SCHEMA
from tools.skill_python import RUN_SKILL_PYTHON_SCHEMA
from tools.skill_script import RUN_SKILL_SCRIPT_SCHEMA


class SessionSandboxPolicyTests(unittest.TestCase):
    def test_http_origins_are_canonicalized_from_exact_url_prefixes(self) -> None:
        cases = {
            "HTTPS://Example.COM./v1/items?q=one#fragment": (
                "https://example.com:443"
            ),
            "http://example.com": "http://example.com:80",
            "https://example.com:8443/path": "https://example.com:8443",
            "http://bücher.example/path": (
                "http://xn--bcher-kva.example:80"
            ),
            "https://[2001:0db8:0:0::1]/resource": (
                "https://[2001:db8::1]:443"
            ),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, normalize_http_origin(value))

    def test_invalid_and_wildcard_origins_fail_closed(self) -> None:
        invalid = (
            "",
            "ftp://example.com/resource",
            "https://",
            "https://user@example.com/",
            "https://user:secret@example.com/",
            "https://*.example.com/",
            "https://example.*:443/",
            "https://bad_host.example/",
            "https://-bad.example/",
            "https://bad-.example/",
            "https://bad..example/",
            "https://exa mple.com/",
            "https://[fe80::1%25eth0]/",
            "https://[bad/",
            "https://[::gg]/",
            "https://example.com:0/",
            "https://example.com:65536/",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(SessionSandboxPolicyError):
                    normalize_http_origin(value)

    def test_no_context_or_no_grants_defaults_to_no_egress(self) -> None:
        self.assertEqual(
            (),
            skill_session_sandbox_egress_origins(None, "alpha"),
        )
        self.assertEqual(
            (),
            skill_session_sandbox_egress_origins(ToolContext(), "alpha"),
        )

    def test_only_exact_skill_grants_are_projected_and_deduplicated(self) -> None:
        context = ToolContext(
            allowed_skill_sandbox_egress_prefixes=(
                ("alpha", "HTTPS://API.Example.COM/v1/"),
                ("beta", "https://beta.example.test/data"),
                ("alpha", "https://api.example.com:443/v2/"),
                ("alpha", "http://submit.example.test/jobs"),
                ("beta", "https://beta-post.example.test/jobs"),
            ),
            allowed_browser_private_origins=(
                "https://10.10.132.126:18443/private/path",
            ),
        )

        self.assertEqual(
            (
                "https://api.example.com:443",
                "http://submit.example.test:80",
            ),
            skill_session_sandbox_egress_origins(context, "alpha"),
        )
        self.assertEqual(
            (
                "https://beta.example.test:443",
                "https://beta-post.example.test:443",
            ),
            skill_session_sandbox_egress_origins(context, "beta"),
        )
        self.assertEqual(
            (),
            skill_session_sandbox_egress_origins(context, "ungranted"),
        )

    def test_exact_rules_retain_methods_query_and_skill_scoped_private_subset(
        self,
    ) -> None:
        context = ToolContext(
            allowed_skill_sandbox_egress_rules=(
                (
                    "alpha",
                    "https://10.10.132.126:18443/api/v1/?tenant=a",
                    ("GET", "HEAD", "POST"),
                ),
                (
                    "beta",
                    "https://172.30.100.126:18443/api/v2/",
                    ("GET", "HEAD"),
                ),
            ),
            allowed_browser_private_origins=(
                "https://10.10.132.126:18443",
                "https://172.30.100.126:18443",
            ),
        )

        alpha = skill_session_sandbox_egress_policy(context, "alpha")
        self.assertEqual(
            (
                SessionSandboxEgressRule(
                    "https://10.10.132.126:18443/api/v1/?tenant=a",
                    ("GET", "HEAD", "POST"),
                ),
            ),
            alpha.rules,
        )
        self.assertEqual(
            ("https://10.10.132.126:18443",),
            alpha.private_origins,
        )
        self.assertEqual(
            ("https://172.30.100.126:18443",),
            skill_session_sandbox_egress_policy(
                context,
                "beta",
            ).private_origins,
        )
        self.assertEqual(
            (),
            skill_session_sandbox_egress_policy(
                context,
                "ungranted",
            ).private_origins,
        )

    def test_exact_rule_normalization_is_bounded_and_method_complete(self) -> None:
        normalized = normalize_session_sandbox_egress_rules((
            {
                "methods": ["DELETE", "PUT", "GET", "OPTIONS"],
                "url_prefix": "http://api.vendor.test:80/v1/items?scope=a",
            },
            {
                "methods": ["HEAD", "PATCH", "POST"],
                "url_prefix": "http://api.vendor.test:80/v1/items?scope=a",
            },
        ))

        self.assertEqual(
            (
                SessionSandboxEgressRule(
                    "http://api.vendor.test:80/v1/items?scope=a",
                    (
                        "GET",
                        "HEAD",
                        "OPTIONS",
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                    ),
                ),
            ),
            normalized,
        )

    def test_exact_prefix_rejects_ambiguous_authority_and_path_forms(self) -> None:
        self.assertEqual(
            "https://api.vendor.test:443/v1/items?tenant=a",
            normalize_http_url_prefix(
                "HTTPS://API.VENDOR.TEST/v1/items?tenant=a"
            ),
        )
        invalid = (
            "https://user@api.vendor.test/v1/",
            "https://api.vendor.test/v1/#fragment",
            "https://api.vendor.test/v1//admin",
            "https://api.vendor.test/v1/%2fadmin",
            "https://api.vendor.test/v1/../admin",
            "https://api.vendor.test/v1/?bad%escape",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(SessionSandboxPolicyError):
                    normalize_http_url_prefix(value)

    def test_origin_projection_has_a_hard_unique_origin_bound(self) -> None:
        maximum = tuple(
            (
                "alpha",
                f"https://host-{index}.example.test/resource",
            )
            for index in range(MAX_SESSION_SANDBOX_EGRESS_ORIGINS)
        )
        self.assertEqual(
            MAX_SESSION_SANDBOX_EGRESS_ORIGINS,
            len(
                skill_session_sandbox_egress_origins(
                    ToolContext(
                        allowed_skill_sandbox_egress_prefixes=maximum
                    ),
                    "alpha",
                )
            ),
        )

        over_limit = maximum + (
            ("alpha", "https://overflow.example.test/resource"),
        )
        with self.assertRaises(SessionSandboxPolicyError) as caught:
            skill_session_sandbox_egress_origins(
                ToolContext(
                    allowed_skill_sandbox_egress_prefixes=over_limit
                ),
                "alpha",
            )
        self.assertEqual(
            "session_sandbox_egress_origin_limit_exceeded",
            str(caught.exception),
        )

    def test_direct_http_ledgers_do_not_expand_sandbox_egress(self) -> None:
        context = ToolContext(
            allowed_skill_http_prefixes=(
                ("alpha", "https://get.example.test/v1/"),
            ),
            allowed_skill_http_post_prefixes=(
                ("alpha", "https://post.example.test/v1/"),
            ),
        )

        self.assertEqual(
            (),
            skill_session_sandbox_egress_origins(context, "alpha"),
        )

    def test_model_facing_tools_cannot_supply_egress_authority(self) -> None:
        for schema in (
            RUN_SKILL_SCRIPT_SCHEMA,
            RUN_SKILL_PYTHON_SCHEMA,
            RUN_SKILL_PROCESS_SCHEMA,
        ):
            with self.subTest(tool=schema["name"]):
                properties = schema["parameters"]["properties"]
                self.assertNotIn("egress_origins", properties)
                self.assertNotIn("network_policy", properties)
                self.assertNotIn("runtime_profile", properties)
                definition = get_schemas([schema["name"]])[0]["function"]
                self.assertIs(
                    False,
                    definition["parameters"]["additionalProperties"],
                )


if __name__ == "__main__":
    unittest.main()
