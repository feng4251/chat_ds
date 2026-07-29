"""Invocation-bound current-user URL authority for exact Skill entrypoints.

Schema-v2 ``user_url_egress`` declarations are selectors, not permissions.
This module is the only bridge that may turn one such selector into a
method-and-URL rule.  It does so immediately before dispatch by intersecting:

* the method-free exact URL ledger compiled from bounded user context;
* the binding from the immutable selected entrypoint manifest; and
* the actual, already validated invocation arguments.

The resulting rules are local to that one executor submission and are never
installed in a capability catalog or ``ToolContext`` permission ledger.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

from skills.http_grants import compile_user_sandbox_egress_rules
from tools.context import ToolContext
from tools.session_sandbox_policy import (
    MAX_SESSION_SANDBOX_EGRESS_ORIGINS,
    SessionSandboxEgressPolicy,
    normalize_http_origin,
    normalize_session_sandbox_egress_rules,
    skill_session_sandbox_egress_policy,
)
from tools.skill_runtime_profile import SkillRuntimeSelection


_AST_DEFAULT = object()


def _signature_from_ast_arguments(
    arguments: ast.arguments,
    *,
    drop_receiver: bool,
) -> inspect.Signature | None:
    """Build a non-evaluating Signature with Python's exact call semantics."""

    positional_nodes = [
        *arguments.posonlyargs,
        *arguments.args,
    ]
    positional_kinds = [
        *(
            inspect.Parameter.POSITIONAL_ONLY
            for _node in arguments.posonlyargs
        ),
        *(
            inspect.Parameter.POSITIONAL_OR_KEYWORD
            for _node in arguments.args
        ),
    ]
    default_start = len(positional_nodes) - len(arguments.defaults)
    parameters: list[inspect.Parameter] = []
    for index, (node, kind) in enumerate(
        zip(positional_nodes, positional_kinds)
    ):
        parameters.append(inspect.Parameter(
            node.arg,
            kind,
            default=(
                _AST_DEFAULT
                if index >= default_start
                else inspect.Parameter.empty
            ),
        ))
    if drop_receiver:
        if (
            not parameters
            or parameters[0].kind
            not in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        ):
            return None
        parameters = parameters[1:]
    if arguments.vararg is not None:
        parameters.append(inspect.Parameter(
            arguments.vararg.arg,
            inspect.Parameter.VAR_POSITIONAL,
        ))
    for node, default in zip(
        arguments.kwonlyargs,
        arguments.kw_defaults,
    ):
        parameters.append(inspect.Parameter(
            node.arg,
            inspect.Parameter.KEYWORD_ONLY,
            default=(
                inspect.Parameter.empty
                if default is None
                else _AST_DEFAULT
            ),
        ))
    if arguments.kwarg is not None:
        parameters.append(inspect.Parameter(
            arguments.kwarg.arg,
            inspect.Parameter.VAR_KEYWORD,
        ))
    try:
        return inspect.Signature(parameters=parameters)
    except (TypeError, ValueError):
        return None


def _public_callable_signature(
    source: str,
    callable_name: str,
) -> inspect.Signature | None:
    """Resolve one exact public callable signature without importing code."""

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    parts = callable_name.split(".")
    if (
        len(parts) not in {1, 2}
        or any(not part.isidentifier() or part.startswith("_") for part in parts)
    ):
        return None
    if len(parts) == 1:
        name = parts[0]
        functions = [
            node
            for node in tree.body
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
                and not node.decorator_list
            )
        ]
        if len(functions) == 1:
            return _signature_from_ast_arguments(
                functions[0].args,
                drop_receiver=False,
            )
        # A persistent structured class session may bind constructor
        # parameters by declaring the public class name as its callable.
        classes = [
            node
            for node in tree.body
            if (
                isinstance(node, ast.ClassDef)
                and node.name == name
                and not node.decorator_list
            )
        ]
        if len(classes) != 1:
            return None
        constructor_nodes = [
            node
            for node in classes[0].body
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "__init__"
            )
        ]
        if not constructor_nodes:
            # A class without an explicit constructor inherits object.__init__
            # and accepts no model-supplied constructor arguments.
            return inspect.Signature()
        if (
            len(constructor_nodes) != 1
            # Python never awaits __init__: ``async def __init__`` returns a
            # coroutine and real instantiation raises TypeError. It therefore
            # cannot prove a persistent or one-shot constructor invocation.
            or not isinstance(constructor_nodes[0], ast.FunctionDef)
            or constructor_nodes[0].decorator_list
        ):
            return None
        return _signature_from_ast_arguments(
            constructor_nodes[0].args,
            drop_receiver=True,
        )

    class_name, method_name = parts
    classes = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.ClassDef)
            and node.name == class_name
            and not node.decorator_list
        )
    ]
    if len(classes) != 1:
        return None
    methods = [
        node
        for node in classes[0].body
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
            and not node.decorator_list
        )
    ]
    if len(methods) != 1:
        return None
    return _signature_from_ast_arguments(
        methods[0].args,
        drop_receiver=True,
    )


def bind_python_invocation_parameters(
    source: str,
    *,
    callable_name: str,
    positional: list[Any] | tuple[Any, ...] | None,
    keywords: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Map actual Python call arguments to explicit parameter names.

    The Skill runner performs the full invocation validation.  This bounded
    binder has the narrower security job of proving where a manifest-selected
    URL parameter came from. It nevertheless validates the *complete* call
    through ``Signature.bind`` before returning any mapping: positional-only,
    keyword-only, defaults, varargs, varkwargs, duplicate values, missing
    required parameters, and surplus arguments all follow Python semantics.
    Defaults and values absorbed only by ``*args``/``**kwargs`` never mint
    authority because they are not explicit values for the selected named
    parameter.
    """

    if (
        not isinstance(source, str)
        or not isinstance(callable_name, str)
        or positional is not None
        and not isinstance(positional, (list, tuple))
        or keywords is not None
        and not isinstance(keywords, dict)
    ):
        return None
    signature = _public_callable_signature(source, callable_name)
    if signature is None:
        return None
    actual_positional = tuple(positional or ())
    if any(not isinstance(name, str) for name in (keywords or {})):
        return None
    try:
        bound = signature.bind(
            *actual_positional,
            **dict(keywords or {}),
        )
    except TypeError:
        return None
    # ``inspect.Signature.bind`` intentionally lets a positional-only
    # spelling flow into **kwargs, but on supported Python versions it can
    # then omit the still-required positional-only parameter instead of
    # raising like a real call would. Reassert all required non-variadic
    # parameters so the proof matches interpreter call semantics exactly.
    if any(
        parameter.default is inspect.Parameter.empty
        and parameter.kind not in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }
        and name not in bound.arguments
        for name, parameter in signature.parameters.items()
    ):
        return None
    return dict(bound.arguments)


def invocation_bound_skill_egress_policy(
    context: ToolContext | None,
    skill_name: str,
    selection: SkillRuntimeSelection,
    *,
    invocation: dict[str, Any],
) -> SessionSandboxEgressPolicy:
    """Merge static Skill rules with one proven dynamic invocation."""

    return invocation_bound_skill_egress_policy_for_invocations(
        context,
        skill_name,
        selection,
        invocations=(invocation,),
    )


def invocation_bound_skill_egress_policy_for_invocations(
    context: ToolContext | None,
    skill_name: str,
    selection: SkillRuntimeSelection,
    *,
    invocations: tuple[dict[str, Any], ...],
) -> SessionSandboxEgressPolicy:
    """Merge rules proven by all fully validated calls in one dispatch."""

    static_policy = skill_session_sandbox_egress_policy(
        context,
        skill_name,
    )
    if (
        context is None
        or not invocations
        or any(not isinstance(item, dict) for item in invocations)
        or not context.user_url_authorization_urls
        or not selection.user_url_egress
        or (
            skill_name,
            selection.entrypoint,
            selection.script_sha256,
        ) not in context.allowed_skill_scripts
        or (
            skill_name,
            selection.package_sha256,
        ) not in context.allowed_skill_package_digests
    ):
        return static_policy

    bindings = tuple(
        binding.as_payload()
        for binding in selection.user_url_egress
    )
    dynamic: list[tuple[str, tuple[str, ...]]] = []
    for invocation in invocations:
        dynamic.extend(compile_user_sandbox_egress_rules(
            context.user_url_authorization_urls,
            bindings,
            invocation=invocation,
        ))
    if not dynamic:
        return static_policy
    rules = normalize_session_sandbox_egress_rules([
        *static_policy.rules,
        *(
            {
                "url_prefix": prefix,
                "methods": methods,
            }
            for prefix, methods in dynamic
        ),
    ])
    origins = tuple(dict.fromkeys(
        normalize_http_origin(rule.url_prefix)
        for rule in rules
    ))
    if len(origins) > MAX_SESSION_SANDBOX_EGRESS_ORIGINS:
        # The normalizer and user-ledger bounds make this unreachable under
        # valid runtime state; retain fail-closed behavior if invariants drift.
        return static_policy
    allowed_private = {
        normalize_http_origin(origin)
        for origin in context.allowed_browser_private_origins
    }
    return SessionSandboxEgressPolicy(
        rules=rules,
        private_origins=tuple(
            origin for origin in origins if origin in allowed_private
        ),
    )
