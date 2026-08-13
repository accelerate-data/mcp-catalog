#!/usr/bin/env python3
"""Validate the Accelerate Data curated catalog entries against the contract Studio consumes.

Upstream's Build workflow proves manifest well-formedness for every file. This
check is narrower and stricter: it pins the fields Studio's
classifyCatalogEntrySupport() reads, and it fails when a curated entry is
deleted, renamed, or regressed by a fork sync.

Run locally:  python scripts/curated-entry-contract/validate-curated-entries.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Each curated entry pins the values Studio depends on. Add a row per curated
# entry; the structural checks below apply to all of them. The runtime selects
# which config block is pinned and which authorization contract is enforced.
CURATED_ENTRIES: dict[str, dict[str, object]] = {
    "fabric-core.yaml": {
        "name": "Fabric Core",
        "entryKey": "obot-fabric-core",
        "serverUserType": "multiUser",
        "runtime": "remote",
        "remoteConfig": {
            "fixedURL": "https://api.fabric.microsoft.com/v1/mcp/core",
            "staticOAuthRequired": True,
        },
    },
    "fabric-pro-dev.yaml": {
        "name": "Fabric Pro-Dev",
        "entryKey": "obot-fabric-pro-dev",
        "serverUserType": "multiUser",
        "runtime": "containerized",
        "containerizedConfig": {
            "image": "mcr.microsoft.com/fabric/fabric-mcp:1.2.0",
            "port": 8080,
            "path": "/",
            "healthzPath": "/health",
            "oauth": {
                "provider": "microsoftEntra",
                "authorityEnv": "AzureAd__Instance",
                "tenantIDEnv": "AzureAd__TenantId",
                "clientIDEnv": "AzureAd__ClientId",
                "clientSecretEnv": "AzureAd__ClientCredentials__0__ClientSecret",
                "scopes": ["api://${AzureAd__ClientId}/Mcp.Tools.ReadWrite"],
            },
        },
        "envKeys": (
            # No command-line flag sets the listen address. Without this key the
            # server binds the container's loopback on port 5000, nothing outside
            # the container can reach it, and readiness never passes.
            "ASPNETCORE_URLS",
            "AzureAd__TenantId",
            "AzureAd__ClientId",
            "AzureAd__Instance",
            "AzureAd__ClientCredentials__0__SourceType",
            "AzureAd__ClientCredentials__0__ClientSecret",
        ),
    },
    "mailgun.yaml": {
        "name": "Mailgun",
        "entryKey": "obot-mailgun",
        "serverUserType": "multiUser",
        "runtime": "npx",
        "package": "@mailgun/mcp-server@2.1.2",
    },
    "resend.yaml": {
        "name": "Resend",
        "entryKey": "obot-resend",
        "serverUserType": "multiUser",
        "runtime": "remote",
        "remoteConfig": {
            "fixedURL": "https://mcp.resend.com/mcp",
        },
        "remoteHeaders": [
            {
                "name": "Resend API key",
                "description": "Shared Resend API key for the MCP deployment.",
                "key": "Authorization",
                "required": True,
                "sensitive": True,
                "prefix": "Bearer ",
            }
        ],
    },
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
    for field in ("name", "entryKey", "serverUserType", "runtime"):
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


def check_pinned_env_keys(manifest: dict, expected: dict[str, object], fail) -> None:
    """An entry whose image needs a fixed env value pins the whole key list.

    Obot builds a containerized server's environment solely from the operator's
    answers to `env` — ContainerizedRuntimeConfig carries no env field — so an
    image that requires a non-secret variable with no command-line equivalent
    depends on that key staying declared. Dropping one leaves every other check
    green while the deployment can no longer start.
    """
    want = expected.get("envKeys")
    if want is None:
        return

    got = [field.get("key") for field in manifest.get("env") or [] if isinstance(field, dict)]
    if got != list(want):  # type: ignore[arg-type]
        fail(f"env keys are {got!r}, expected {list(want)!r}")  # type: ignore[arg-type]


def check_remote_user_scoped_auth(manifest: dict, fail) -> None:
    """A user-authorized remote entry must configure no deployment-wide credential.

    An env block or a remoteConfig header would make an operator-supplied secret
    part of the entry, which is exactly what user-scoped OAuth must not require.
    """
    if manifest.get("env"):
        fail("env must be absent: authorization is user-scoped, not deployment-configured")

    headers = (manifest.get("remoteConfig") or {}).get("headers")
    if headers:
        fail("remoteConfig.headers must be absent: no static credential is sent to the server")


def check_resend_remote_auth(manifest: dict, expected: dict[str, object], fail) -> None:
    """Resend's hosted MCP server uses one shared bearer key at Instance scope.

    Studio must keep the integration on one required deployment-owned secret,
    without adding env-based credentials, static OAuth, or per-user header prompts.
    """
    if "env" in manifest:
        fail(
            "env must be absent: Resend remote auth must stay on the required shared bearer header"
        )

    multi_user_config = manifest.get("multiUserConfig")
    if isinstance(multi_user_config, dict) and "userDefinedHeaders" in multi_user_config:
        fail(
            "multiUserConfig.userDefinedHeaders must be absent: "
            "Resend remote auth is instance-owned, not per-user"
        )

    remote_config = manifest.get("remoteConfig")
    if not isinstance(remote_config, dict):
        fail("remoteConfig must be a mapping")
        return

    if "staticOAuthRequired" in remote_config:
        fail("remoteConfig.staticOAuthRequired must be absent")

    headers = remote_config.get("headers")
    if not isinstance(headers, list) or len(headers) != 1:
        fail(
            "remoteConfig.headers must contain exactly one Authorization header "
            "for the required shared bearer credential"
        )
        return

    header = headers[0]
    if not isinstance(header, dict):
        fail("remoteConfig.headers[0] must be a mapping")
        return

    key = header.get("key")
    if key != "Authorization":
        fail(f"remoteConfig.headers[0].key is {key!r}, expected 'Authorization'")
        return

    label = "remoteConfig.headers[Authorization]"
    expected_header = (expected.get("remoteHeaders") or [None])[0]
    if not isinstance(expected_header, dict):
        fail("curated entry pins invalid remoteHeaders contract")
        return

    allowed_header_fields = {
        "name",
        "description",
        "key",
        "required",
        "sensitive",
        "prefix",
    }
    for field in ("value", "secretBinding"):
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


def check_containerized_auth(manifest: dict, fail) -> None:
    """A containerized entry uses deployment config for its OAuth application.

    Obot owns every user's authorization grant and injects the refreshed bearer
    into that user's request. The catalog must not fall back to pasted headers.
    """
    for index, field in enumerate(manifest.get("env") or []):
        label = f"env[{index}]"
        if not isinstance(field, dict):
            fail(f"{label} must be a mapping")
            continue
        key = field.get("key")
        if not isinstance(key, str) or not key.strip():
            fail(f"{label}.key must be a non-empty string")
            continue
        # Obot's catalog path reads only credEnv[key] for a non-system server, so
        # a static value is silently dropped and a required field lands unset.
        if field.get("value"):
            fail(f"env[{key}].value must be absent: Obot ignores it on a catalog entry")

    headers = (manifest.get("multiUserConfig") or {}).get("userDefinedHeaders")
    if headers:
        fail("multiUserConfig.userDefinedHeaders must be absent: Obot owns the per-user OAuth grant")

    oauth = (manifest.get("containerizedConfig") or {}).get("oauth")
    if not isinstance(oauth, dict):
        fail("containerizedConfig.oauth must declare the deployment-owned OAuth application")
        return
    expected = CURATED_ENTRIES["fabric-pro-dev.yaml"]["containerizedConfig"]["oauth"]
    if oauth != expected:
        fail(f"containerizedConfig.oauth is {oauth!r}, expected {expected!r}")


def check_authorization(manifest: dict, expected: dict[str, object], fail) -> None:
    if expected.get("remoteHeaders") is not None:
        check_resend_remote_auth(manifest, expected, fail)
    elif expected["runtime"] == "remote":
        check_remote_user_scoped_auth(manifest, fail)
    elif expected["runtime"] == "containerized":
        check_containerized_auth(manifest, fail)
    else:
        check_mailgun_deployment_config(manifest, fail)


def check_containerized_readiness(manifest: dict, fail) -> None:
    """Obot's readiness probe must reach an anonymous endpoint.

    With no healthzPath, Obot POSTs an MCP `initialize` body to the container
    path and requires a 200. A server that authenticates its MCP endpoint
    answers 401 there, so the deployment never becomes ready.
    """
    healthz = (manifest.get("containerizedConfig") or {}).get("healthzPath")
    if not isinstance(healthz, str) or not healthz.strip():
        fail(
            "containerizedConfig.healthzPath must be a non-empty string: "
            "Obot's fallback readiness probe hits the authenticated MCP path"
        )


def check_mailgun_deployment_config(manifest: dict, fail) -> None:
    multi_user_config = manifest.get("multiUserConfig")
    if multi_user_config is not None and not isinstance(multi_user_config, dict):
        fail("multiUserConfig must be a mapping when present")
        user_defined_headers = None
    else:
        user_defined_headers = (multi_user_config or {}).get("userDefinedHeaders")
    if user_defined_headers:
        fail("multiUserConfig.userDefinedHeaders must be absent for an npx stdio server")

    env = manifest.get("env")
    if not isinstance(env, list):
        fail("env must be a list")
        return

    fields = {
        field.get("key"): field
        for field in env
        if isinstance(field, dict) and isinstance(field.get("key"), str)
    }
    api_key = fields.get("MAILGUN_API_KEY")
    if not isinstance(api_key, dict):
        fail("env must declare MAILGUN_API_KEY")
    elif api_key.get("required") is not True or api_key.get("sensitive") is not True:
        fail("MAILGUN_API_KEY must be required and sensitive")

    if "MAILGUN_API_HOSTNAME" in fields:
        fail("MAILGUN_API_HOSTNAME must not be exposed as deployment configuration")

    expected_keys = {"MAILGUN_API_KEY", "MAILGUN_API_REGION"}
    if set(fields) != expected_keys:
        fail(f"env keys are {sorted(fields)!r}, expected {sorted(expected_keys)!r}")

    region = fields.get("MAILGUN_API_REGION")
    if not isinstance(region, dict):
        fail("env must declare MAILGUN_API_REGION")
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


def validate(root: Path) -> list[str]:
    errors: list[str] = []

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

        check_required_strings(manifest, fail)
        check_pinned_values(manifest, expected, fail)
        check_pinned_env_keys(manifest, expected, fail)
        check_authorization(manifest, expected, fail)
        if runtime == "containerized":
            check_containerized_readiness(manifest, fail)
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
