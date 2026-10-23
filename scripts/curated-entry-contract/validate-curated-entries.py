#!/usr/bin/env python3
"""Validate the Accelerate Data curated catalog entries against the contract Studio consumes.

Upstream's Build workflow proves manifest well-formedness for every file. This
check is narrower and stricter: it pins the fields Studio's
classifyCatalogEntrySupport() reads, and it fails when a curated entry is
deleted, renamed, or regressed by a fork sync. It also rejects deprecated
configuration shapes across the active catalog, including non-curated entries.

Run locally:  python scripts/curated-entry-contract/validate-curated-entries.py
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

# Each curated entry pins the values Studio depends on. Add a row per curated
# entry; the structural checks below apply to all of them. The runtime selects
# which config block is pinned and which authorization contract is enforced.
CURATED_ENTRIES: dict[str, dict[str, object]] = {
    "fabric-core.yaml": {
        "name": "Fabric Core",
        "entryKey": "obot-fabric-core",
        "runtime": "remote",
        "remoteConfig": {
            "fixedURL": "https://api.fabric.microsoft.com/v1/mcp/core",
            "staticOAuthRequired": True,
        },
    },
    "mailgun.yaml": {
        "name": "Mailgun",
        "entryKey": "obot-mailgun",
        "runtime": "npx",
        "package": "@mailgun/mcp-server@2.1.2",
        "startupTimeoutSeconds": 600,
    },
    "resend.yaml": {
        "name": "Resend",
        "entryKey": "obot-resend",
        "runtime": "remote",
        "remoteConfig": {
            "fixedURL": "https://mcp.resend.com/mcp",
        },
        "headers": [
            {
                "name": "Resend API key",
                "description": "Shared Resend API key for the MCP deployment.",
                "key": "Authorization",
                "required": True,
                "sensitive": True,
                "prefix": "Bearer ",
                "usage": "header",
            }
        ],
    },
    "remotes/exa_search.yaml": {
        "name": "Exa Search",
        "entryKey": "obot-exa-search",
        "runtime": "remote",
        "remoteConfig": {
            "urlTemplate": "https://mcp.exa.ai/mcp?tools=${EXA_TOOLS}",
        },
        "configKeys": ("x-api-key", "EXA_TOOLS"),
        "headers": [
            {
                "name": "Exa API Key",
                "description": (
                    "API key from the Exa dashboard. Sent in the documented x-api-key header."
                ),
                "key": "x-api-key",
                "usage": "header",
                "required": True,
                "sensitive": True,
            }
        ],
    },
}

EXA_TOOL_PROFILES = {
    "Basic Search": "web_search_exa,web_fetch_exa",
    "Advanced Search": "web_search_exa,web_fetch_exa,web_search_advanced_exa",
    "Search + Research Agent": "web_search_exa,web_fetch_exa,web_search_advanced_exa,agent_run",
}

REQUIRED_NON_EMPTY_STRINGS = (
    "name",
    "entryKey",
    "shortDescription",
    "description",
    "runtime",
    "icon",
    "repoURL",
)

# The manifest key that carries each runtime's connection config.
RUNTIME_CONFIG_KEYS = {
    "remote": "remoteConfig",
    "containerized": "containerizedConfig",
    "npx": "npxConfig",
}


def check_required_strings(manifest: dict, fail) -> None:
    for field in REQUIRED_NON_EMPTY_STRINGS:
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            fail(f"{field} must be a non-empty string")

    categories = (manifest.get("metadata") or {}).get("categories")
    if not isinstance(categories, str) or not categories.strip():
        fail("metadata.categories must be a non-empty string")


def check_pinned_values(manifest: dict, expected: dict[str, object], fail) -> None:
    for field in ("name", "entryKey", "runtime"):
        actual = manifest.get(field)
        if actual != expected[field]:
            fail(f"{field} is {actual!r}, expected {expected[field]!r}")

    if expected["runtime"] == "npx":
        npx_config = manifest.get("npxConfig")
        if not isinstance(npx_config, dict):
            fail("npxConfig must be a mapping")
            return

        package = npx_config.get("package")
        if package != expected["package"]:
            fail(f"npxConfig.package is {package!r}, expected {expected['package']!r}")
        startup_timeout = npx_config.get("startupTimeoutSeconds")
        if startup_timeout != expected["startupTimeoutSeconds"]:
            fail(
                "npxConfig.startupTimeoutSeconds is "
                f"{startup_timeout!r}, expected {expected['startupTimeoutSeconds']!r}"
            )
        if "args" in npx_config:
            fail("npxConfig.args must be absent for the curated Mailgun entry")
        if manifest.get("remoteConfig"):
            fail("remoteConfig must be absent for the curated Mailgun entry")
        return

    config_key = RUNTIME_CONFIG_KEYS[str(expected["runtime"])]
    config = manifest.get(config_key)
    if not isinstance(config, dict):
        fail(f"{config_key} must be a mapping")
        return

    for field, want in (expected.get(config_key) or {}).items():  # type: ignore[union-attr]
        got = config.get(field)
        # `is not` for booleans so that 1 does not satisfy True.
        mismatch = got is not want if isinstance(want, bool) else got != want
        if mismatch:
            fail(f"{config_key}.{field} is {got!r}, expected {want!r}")


def config_fields(manifest: dict, usage: str) -> list[dict]:
    return [
        field
        for field in manifest.get("config") or []
        if isinstance(field, dict) and field.get("usage") == usage
    ]


def check_pinned_config_keys(manifest: dict, expected: dict[str, object], fail) -> None:
    want = expected.get("configKeys")
    if want is None:
        return
    got = [
        field.get("key")
        for field in manifest.get("config") or []
        if isinstance(field, dict)
    ]
    if sorted(got) != sorted(want):
        fail(f"config keys are {got!r}, expected {list(want)!r}")


def check_remote_user_scoped_auth(manifest: dict, fail) -> None:
    """Fabric Core uses per-user OAuth, without deployment-owned inputs."""
    if manifest.get("config"):
        fail(
            "config must be absent: authorization is user-scoped, not deployment-configured"
        )


def check_resend_remote_auth(manifest: dict, expected: dict[str, object], fail) -> None:
    """Resend's hosted MCP server uses one shared bearer key at Instance scope.

    Studio must keep the integration on one required deployment-owned secret,
    without adding env-based credentials, static OAuth, or per-user header prompts.
    """
    if any(field.get("usage") != "header" for field in manifest.get("config") or []):
        fail("config must contain only the required shared bearer header")

    remote_config = manifest.get("remoteConfig")
    if not isinstance(remote_config, dict):
        fail("remoteConfig must be a mapping")
        return

    if "staticOAuthRequired" in remote_config:
        fail("remoteConfig.staticOAuthRequired must be absent")

    headers = config_fields(manifest, "header")
    if not isinstance(headers, list) or len(headers) != 1:
        fail(
            "config must contain exactly one Authorization header "
            "for the required shared bearer credential"
        )
        return

    header = headers[0]
    if not isinstance(header, dict):
        fail("config[0] must be a mapping")
        return

    key = header.get("key")
    if key != "Authorization":
        fail(f"config[0].key is {key!r}, expected 'Authorization'")
        return

    label = "config[Authorization]"
    expected_header = (expected.get("headers") or [None])[0]
    if not isinstance(expected_header, dict):
        fail("curated entry pins invalid headers contract")
        return

    allowed_header_fields = {
        "name",
        "description",
        "key",
        "required",
        "sensitive",
        "prefix",
        "usage",
    }
    for field in ("value", "static", "secretBinding", "userAllowed"):
        if field in header:
            fail(
                f"{label}.{field} must be absent: Resend remote auth requires an "
                "owner-supplied bearer credential"
            )

    actual_header_fields = set(header)
    if actual_header_fields != allowed_header_fields:
        field_differences = []
        missing_fields = sorted(allowed_header_fields - actual_header_fields)
        unexpected_fields = sorted(actual_header_fields - allowed_header_fields)
        if missing_fields:
            field_differences.append(f"missing fields: {missing_fields!r}")
        if unexpected_fields:
            field_differences.append(f"unexpected fields: {unexpected_fields!r}")
        fail(
            f"{label} must contain exactly the supported fields "
            f"{sorted(allowed_header_fields)!r}; {'; '.join(field_differences)}"
        )

    for field in ("name", "description", "prefix"):
        got = header.get(field)
        want = expected_header[field]
        if got != want:
            if field == "prefix":
                fail(f"{label}.prefix must be {want!r}")
            else:
                fail(f"{label}.{field} is {got!r}, expected {want!r}")

    if header.get("required") is not True:
        fail(f"{label}.required must be true")
    if header.get("sensitive") is not True:
        fail(f"{label}.sensitive must be true")


def check_exa_remote_auth(manifest: dict, expected: dict[str, object], fail) -> None:
    """Exa's hosted MCP server takes one shared API key in a header at Instance scope.

    The key must stay a required, sensitive, owner-supplied header: never in the
    URL, never a per-user prompt. The only deployment config besides it is the
    tool profile that the urlTemplate interpolates.
    """
    remote_config = manifest.get("remoteConfig")
    if not isinstance(remote_config, dict):
        fail("remoteConfig must be a mapping")
        return

    for field in ("staticOAuthRequired", "fixedURL", "hostname"):
        if field in remote_config:
            fail(f"remoteConfig.{field} must be absent")

    url_template = remote_config.get("urlTemplate")
    if isinstance(url_template, str) and (
        re.findall(r"\$\{(\w+)\}", url_template) != ["EXA_TOOLS"]
        or urlsplit(url_template).query != "tools=${EXA_TOOLS}"
    ):
        fail(
            "remoteConfig.urlTemplate must interpolate only ${EXA_TOOLS}: no credential in the URL"
        )

    headers = config_fields(manifest, "header")
    if (
        not isinstance(headers, list)
        or len(headers) != 1
        or not isinstance(headers[0], dict)
    ):
        fail("config must contain exactly one x-api-key header")
        return

    header = headers[0]
    expected_header = (expected.get("headers") or [None])[0]
    if not isinstance(expected_header, dict):
        fail("curated entry pins invalid headers contract")
        return

    label = "config[x-api-key]"
    for field in ("value", "static", "secretBinding", "userAllowed"):
        if field in header:
            fail(
                f"{label}.{field} must be absent: Exa requires an owner-supplied API key"
            )

    if set(header) != set(expected_header):
        fail(
            f"{label} must contain exactly the supported fields "
            f"{sorted(expected_header)!r}; got {sorted(header)!r}"
        )

    for field, want in expected_header.items():
        got = header.get(field)
        mismatch = got is not want if isinstance(want, bool) else got != want
        if mismatch:
            fail(f"{label}.{field} is {got!r}, expected {want!r}")

    env = config_fields(manifest, "env")
    if len(env) != 1:
        fail("EXA_TOOLS must have usage env")
        return
    tools = env[0]
    if tools.get("required") is not True or tools.get("sensitive") is not False:
        fail("EXA_TOOLS must be required and non-sensitive")
    profiles = {
        option.get("name"): option.get("value")
        for option in tools.get("options") or []
        if isinstance(option, dict)
    }
    if profiles != EXA_TOOL_PROFILES or len(tools.get("options") or []) != len(
        EXA_TOOL_PROFILES
    ):
        fail(f"EXA_TOOLS options are {profiles!r}, expected {EXA_TOOL_PROFILES!r}")


def check_authorization(manifest: dict, expected: dict[str, object], fail) -> None:
    if expected["entryKey"] == "obot-exa-search":
        check_exa_remote_auth(manifest, expected, fail)
    elif expected.get("headers") is not None:
        check_resend_remote_auth(manifest, expected, fail)
    elif expected["runtime"] == "remote":
        check_remote_user_scoped_auth(manifest, fail)
    elif expected["runtime"] == "npx":
        check_mailgun_deployment_config(manifest, fail)


def check_mailgun_deployment_config(manifest: dict, fail) -> None:
    env = manifest.get("config") or []
    if any(field.get("usage") != "env" for field in env):
        fail("Mailgun config must use env: stdio cannot carry request headers")
    for field in env:
        for key in ("value", "static", "secretBinding", "userAllowed"):
            if key in field:
                fail(
                    f"Mailgun config.{key} must be absent: credentials are owner-supplied"
                )

    fields = {
        field.get("key"): field
        for field in env
        if isinstance(field, dict) and isinstance(field.get("key"), str)
    }
    api_key = fields.get("MAILGUN_API_KEY")
    if not isinstance(api_key, dict):
        fail("config must declare MAILGUN_API_KEY")
    elif api_key.get("required") is not True or api_key.get("sensitive") is not True:
        fail("MAILGUN_API_KEY must be required and sensitive")

    if "MAILGUN_API_HOSTNAME" in fields:
        fail("MAILGUN_API_HOSTNAME must not be exposed as deployment configuration")

    expected_keys = {"MAILGUN_API_KEY", "MAILGUN_API_REGION"}
    if set(fields) != expected_keys:
        fail(f"config keys are {sorted(fields)!r}, expected {sorted(expected_keys)!r}")

    region = fields.get("MAILGUN_API_REGION")
    if not isinstance(region, dict):
        fail("config must declare MAILGUN_API_REGION")
    elif region.get("required") is not False or region.get("sensitive") is not False:
        fail("MAILGUN_API_REGION must be optional and non-sensitive")


def check_tool_preview(manifest: dict, fail) -> None:
    tools = manifest.get("toolPreview")
    if not isinstance(tools, list) or not tools:
        fail("toolPreview must be a non-empty list")
        return

    seen: set[str] = set()
    for index, tool in enumerate(tools):
        label = f"toolPreview[{index}]"
        if not isinstance(tool, dict):
            fail(f"{label} must be a mapping")
            continue

        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            fail(f"{label}.name must be a non-empty string")
        elif name in seen:
            fail(f"{label}.name {name!r} is duplicated")
        else:
            seen.add(name)
            label = f"toolPreview[{name}]"

        description = tool.get("description")
        if not isinstance(description, str) or not description.strip():
            fail(f"{label}.description must be a non-empty string")

        params = tool.get("params")
        if params is None:
            continue
        if not isinstance(params, dict):
            fail(f"{label}.params must be a mapping")
            continue
        for param, param_description in params.items():
            if not isinstance(param_description, str) or not param_description.strip():
                fail(f"{label}.params.{param} must be a non-empty string")


def check_native_config(manifest: dict, fail) -> None:
    """Reject legacy source shapes even when Obot's compatibility decoder accepts them."""
    for key in ("serverUserType", "multiUserConfig", "env"):
        if key in manifest:
            fail(f"{key} is deprecated; use native config with usage")
    for runtime in ("remoteConfig", "containerizedConfig"):
        config = manifest.get(runtime)
        if isinstance(config, dict) and "headers" in config:
            fail(f"{runtime}.headers is deprecated; use config with usage header")
    fields = manifest.get("config", [])
    if not isinstance(fields, list):
        fail("config must be a list")
        return
    seen = set()
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            fail(f"config[{index}] must be a mapping")
            continue
        key = field.get("key")
        if not isinstance(key, str) or not key.strip():
            fail(f"config[{index}].key must be a non-empty string")
        elif key in seen:
            fail(f"config key {key!r} is duplicated")
        else:
            seen.add(key)
        if field.get("usage") not in (
            "env",
            "header",
            "file",
            "dynamicFile",
            "interpolated",
        ):
            fail(f"config[{index}].usage must be a native MCPConfig usage")
        for legacy in ("file", "dynamicFile", "interpolated"):
            if legacy in field:
                fail(f"config[{index}].{legacy} is deprecated; use usage")


def active_catalog_files(root: Path) -> list[Path]:
    # The repository uses Obot's default *.yaml selection and its ignore file.
    # Recurse so upstream additions outside remotes remain covered as well.
    ignore_file = root / ".ignoreobotcatalogs"
    ignores = (
        [
            line.strip()
            for line in ignore_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if ignore_file.exists()
        else []
    )
    return sorted(
        path
        for path in root.rglob("*.yaml")
        if not path.is_symlink()
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
        and not any(
            fnmatch(path.relative_to(root).as_posix(), pattern) for pattern in ignores
        )
    )


def validate(root: Path) -> list[str]:
    errors: list[str] = []

    for path in active_catalog_files(root):
        try:
            manifest = yaml.safe_load(path.read_text())
        except yaml.YAMLError as error:
            errors.append(f"{path.name}: invalid YAML: {error}")
            continue
        if not isinstance(manifest, dict):
            errors.append(f"{path.name}: manifest must be a mapping")
            continue
        check_native_config(
            manifest,
            lambda message, path=path: errors.append(f"{path.name}: {message}"),
        )

    for filename, expected in sorted(CURATED_ENTRIES.items()):
        path = root / filename

        def fail(message: str, path=path) -> None:
            errors.append(f"{path.name}: {message}")

        runtime = expected.get("runtime")
        if runtime not in RUNTIME_CONFIG_KEYS:
            fail(f"curated entry pins unsupported runtime {runtime!r}")
            continue

        if not path.is_file():
            fail("curated entry is missing from the catalog")
            continue

        try:
            manifest = yaml.safe_load(path.read_text())
        except yaml.YAMLError as error:
            fail(f"invalid YAML: {error}")
            continue

        if not isinstance(manifest, dict):
            fail("manifest must be a mapping")
            continue

        native_errors = []
        check_native_config(manifest, native_errors.append)
        if native_errors:
            continue
        check_required_strings(manifest, fail)
        check_pinned_values(manifest, expected, fail)
        check_pinned_config_keys(manifest, expected, fail)
        check_authorization(manifest, expected, fail)
        check_tool_preview(manifest, fail)

    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    errors = validate(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    print(f"Curated catalog entries are valid ({len(CURATED_ENTRIES)} checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
