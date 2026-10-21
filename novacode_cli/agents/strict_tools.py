"""Provider-aware constrained tool schemas and non-coercing execution validation."""

from __future__ import annotations

import copy
import json
import os
import re
from functools import lru_cache
from urllib.parse import urlparse

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

# Audited against the actual routes in config/model_manager.py. "conditional"
# requires explicit on mode; "local" does not assert provider-side sampling.
PROVIDER_STRICT_MODES = {
    "openai": "openai",
    "azure": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "bedrock": "bedrock",
    "cerebras": "openai",
    "xai": "implicit",
    "openrouter": "conditional",
    "huggingface": "conditional",
    "ollama": "local",
    "opencode": "local",
    "opencode_zen": "local",
    "commandcode": "local",
    "nvidia": "local",
    "mistral": "local",
    "groq": "local",
    "kimi_coding": "local",
    "minimax": "local",
}


def strict_mode() -> str:
    """Read the switch per request so model/provider changes need no restart."""
    value = os.environ.get("NOVA_STRICT_TOOLS")
    if value is None:
        from novacode_cli.config.nova_config import NovaConfig

        value = NovaConfig().get("strict_tools", "auto")
    value = str(value).lower()
    value = {"true": "on", "false": "off", "1": "on", "0": "off"}.get(value, value)
    if value not in {"auto", "on", "off"}:
        raise ValueError("strict_tools must be auto, on or off.")
    return value


def _claude_supported(name: str) -> bool:
    match = re.search(r"claude-(opus|sonnet|haiku)-(\d+)(?:[-.](\d+))?", name)
    if not match:
        return False
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    return major >= 5 or major == 4 and (minor >= 5 or family == "opus" and minor >= 1)


def provider_route(model, mode: str) -> str:
    """Use Nova's recorded provider identity, never a gateway's SDK class name."""
    metadata = getattr(model, "metadata", None) or {}
    provider = metadata.get("nova_provider", "")
    name = str(metadata.get("nova_model") or getattr(model, "model_name", "")).lower()
    route = PROVIDER_STRICT_MODES.get(provider, "local")
    if provider == "openai":
        endpoint = getattr(model, "openai_api_base", None)
        if endpoint and urlparse(str(endpoint)).hostname != "api.openai.com":
            return "local"
    if provider == "anthropic" and not _claude_supported(name):
        return "local"
    if provider == "anthropic":
        endpoint = getattr(model, "anthropic_api_url", None)
        if endpoint and urlparse(str(endpoint)).hostname != "api.anthropic.com":
            return "local"
    if provider == "azure":
        endpoint = str(getattr(model, "openai_api_base", "") or "")
        host = urlparse(endpoint).hostname or ""
        if not host.endswith(
            (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")
        ):
            return "local"
        if mode == "auto" and not re.match(r"(?:gpt-|o[134])", name):
            return "local"  # An arbitrary Azure deployment alias has unknown capabilities.
    if provider == "cerebras" and name not in {"gpt-oss-120b", "qwen-3.8-27b", "gemma-4-31b"}:
        return "local"
    if route == "bedrock":
        if not _claude_supported(name) or name.startswith("in.anthropic.claude-haiku-4-5"):
            return "local"
        try:
            fields = model.client.meta.service_model.shape_for("ToolSpecification").members
            if "strict" not in fields:
                return "local"
        except (AttributeError, KeyError):
            return "local"
    if route == "conditional":
        return "openai" if mode == "on" else "local"
    return route


class IncompatibleStrictSchema(ValueError):
    """The schema cannot be constrained without changing its value semantics."""


class _WireDefinition(dict):
    """Keep accounting tags out of serialized provider request fields."""

    def __init__(self, definition: dict, metadata: dict | None):
        super().__init__(definition)
        self.metadata = metadata


def strict_schema(original: dict) -> dict:
    """Close named objects, require existing properties, preserve their value types.

    Open dictionaries and unsupported constraints are never silently rewritten.
    Optional fields become required on the wire; their declared defaults remain
    documented. No nullable values are invented for non-nullable parameters.
    """
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "description",
        "title",
        "$defs",
        "$ref",
        "anyOf",
        "default",
    }

    def visit(node):
        if not isinstance(node, dict) or not node:
            raise IncompatibleStrictSchema("unconstrained values are not strict-compatible")
        unknown = set(node) - allowed
        if unknown:
            raise IncompatibleStrictSchema(
                f"unsupported schema keywords: {', '.join(sorted(unknown))}"
            )
        result = copy.deepcopy(node)
        if "$ref" in result and not str(result["$ref"]).startswith("#"):
            raise IncompatibleStrictSchema("external references are not strict-compatible")
        if "default" in result:
            default = json.dumps(result.pop("default"), ensure_ascii=False, default=str)
            result["description"] = (
                result.get("description", "") + f" Default: {default}."
            ).strip()
        if result.get("type") == "object" or "properties" in result:
            additional = result.get("additionalProperties")
            if additional is not None and additional is not False:
                raise IncompatibleStrictSchema("open dictionaries cannot be closed safely")
            if "properties" not in result:
                if additional is not False:
                    raise IncompatibleStrictSchema("unconstrained object")
                result["properties"] = {}
            result["properties"] = {
                key: visit(value) for key, value in result["properties"].items()
            }
            if set(result.get("required", [])) - set(result["properties"]):
                raise IncompatibleStrictSchema("required fields must be declared properties")
            result["required"] = list(result["properties"])
            result["additionalProperties"] = False
        if "items" in result:
            result["items"] = visit(result["items"])
        elif result.get("type") == "array":
            raise IncompatibleStrictSchema("array items must have a schema")
        if "anyOf" in result:
            result["anyOf"] = [visit(part) for part in result["anyOf"]]
        if "$defs" in result:
            result["$defs"] = {key: visit(value) for key, value in result["$defs"].items()}
        return result

    return visit(original)


@lru_cache(maxsize=256)
def _validator(encoded: str, close_named: bool):
    from jsonschema.validators import validator_for

    schema = json.loads(encoded)

    def guard(node):
        if isinstance(node, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in node and not str(node[key]).startswith("#"):
                    raise ValueError("External tool schema references are not permitted.")
            if close_named and "properties" in node and "additionalProperties" not in node:
                node["additionalProperties"] = False
            for value in node.values():
                guard(value)
        elif isinstance(node, list):
            for value in node:
                guard(value)

    guard(schema)
    cls = validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


class StrictToolMiddleware(AgentMiddleware):
    """Prepare provider-specific wire definitions without changing registered tools."""

    def _prepare(self, request):
        mode = strict_mode()
        if mode == "off" or not request.tools:
            return request
        route = provider_route(request.model, mode)
        if route == "local":
            if mode == "on":
                raise ValueError(
                    "Native strict tool use is not verified for this provider/model/endpoint."
                )
            return request
        if route == "implicit":
            return request  # xAI documents schema-constrained tool calls by default.
        if route == "google":
            if request.tool_choice is not None:
                if mode == "on":
                    raise ValueError("Explicit tool_choice conflicts with Gemini VALIDATED mode.")
                return request
            for tool in request.tools:
                function = convert_to_openai_tool(tool).get("function")
                try:
                    if function is None:
                        raise IncompatibleStrictSchema("not a function schema")
                    strict_schema(function.get("parameters", {}))
                except IncompatibleStrictSchema as exc:
                    if mode == "on":
                        raise ValueError(
                            f"Gemini VALIDATED mode cannot preserve this tool schema: {exc}"
                        ) from exc
                    return request
            settings = dict(request.model_settings)
            config = copy.deepcopy(settings.get("tool_config") or {})
            if hasattr(config, "model_dump"):
                config = config.model_dump(exclude_none=True)
            if mode == "on" and "function_calling_config" in config:
                if config["function_calling_config"].get("mode") not in {"VALIDATED", "ANY"}:
                    raise ValueError(
                        "Existing Gemini tool configuration does not constrain tool schemas."
                    )
            if "function_calling_config" not in config:
                config["function_calling_config"] = {"mode": "VALIDATED"}
                settings["tool_config"] = config
            return request.override(model_settings=settings)
        prepared = []
        for tool in request.tools:
            definition = convert_to_openai_tool(tool)
            function = definition.get("function")
            if function is None or function.get("strict") is False:
                if mode == "on":
                    raise ValueError(
                        "This tool explicitly disables or does not support strict function schemas."
                    )
                prepared.append(tool)
                continue
            try:
                schema = strict_schema(function.get("parameters", {}))
                if schema.get("type") != "object":
                    raise IncompatibleStrictSchema("function parameters must be an object")
            except IncompatibleStrictSchema as exc:
                if mode == "on":
                    raise ValueError(
                        f"Tool {function['name']} cannot use native strict mode: {exc}"
                    ) from exc
                prepared.append(tool)
                continue
            if route == "anthropic":
                prepared.append(
                    {
                        "name": function["name"],
                        "description": function.get("description", ""),
                        "input_schema": schema,
                        "strict": True,
                    }
                )
            elif route == "bedrock":
                prepared.append(
                    {
                        "toolSpec": {
                            "name": function["name"],
                            "description": function.get("description", ""),
                            "inputSchema": {"json": schema},
                            "strict": True,
                        }
                    }
                )
            else:
                prepared.append(
                    {
                        "type": "function",
                        "function": {**function, "parameters": schema, "strict": True},
                    }
                )
        settings = dict(request.model_settings)
        prepared = [
            _WireDefinition(definition, getattr(original, "metadata", None))
            if isinstance(definition, dict) and definition is not original
            else definition
            for original, definition in zip(request.tools, prepared, strict=True)
        ]
        if not any(
            isinstance(t, dict)
            and (
                t.get("strict")
                or t.get("function", {}).get("strict")
                or t.get("toolSpec", {}).get("strict")
            )
            for t in prepared
        ):
            return request
        provider = (getattr(request.model, "metadata", None) or {}).get("nova_provider")
        if provider == "azure":
            settings["parallel_tool_calls"] = False
        if provider == "openrouter":
            body = copy.deepcopy(settings.get("extra_body") or {})
            body["provider"] = {**body.get("provider", {}), "require_parameters": True}
            settings["extra_body"] = body
        return request.override(tools=prepared, model_settings=settings)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))

    def _invalid_arguments(self, request):
        if strict_mode() == "off" or not isinstance(request.tool, BaseTool):
            return None
        from jsonschema.exceptions import SchemaError, ValidationError
        from referencing.exceptions import Unresolvable

        schema = request.tool.tool_call_schema
        close_named = not isinstance(schema, dict)
        if not isinstance(schema, dict):
            schema = schema.model_json_schema()
        try:
            _validator(json.dumps(schema, sort_keys=True), close_named).validate(
                request.tool_call.get("args") or {}
            )
        except (ValidationError, SchemaError, ValueError, Unresolvable) as exc:
            path = ".".join(str(part) for part in getattr(exc, "absolute_path", [])) or "arguments"
            detail = exc.message if isinstance(exc, (ValidationError, SchemaError)) else str(exc)
            return ToolMessage(
                content=f"Invalid tool arguments at {path}: {detail}",
                status="error",
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
            )
        return None

    def wrap_tool_call(self, request, handler):
        denial = self._invalid_arguments(request)
        return denial if denial is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        denial = self._invalid_arguments(request)
        return denial if denial is not None else await handler(request)
