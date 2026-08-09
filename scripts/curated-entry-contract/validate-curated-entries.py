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


def check_containerized_auth(manifest: dict, fail) -> None:
    """A containerized entry splits its credentials across two configuration tiers.

    A containerized entry has no endpoint to configure, so it cannot use static
    OAuth; an operator-supplied `env` block is legitimate there and carries the
    deployment's own registration. The per-user credential must still come from
    `multiUserConfig.userDefinedHeaders`, which is the only surface Studio
    projects into User Settings.
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
    if not isinstance(headers, list) or not headers:
        fail(
            "multiUserConfig.userDefinedHeaders must be a non-empty list: "
            "the per-user credential has no other configuration surface"
        )
        return

    for index, header in enumerate(headers):
        label = f"userDefinedHeaders[{index}]"
        if not isinstance(header, dict):
            fail(f"{label} must be a mapping")
            continue
        key = header.get("key")
        if not isinstance(key, str) or not key.strip():
            fail(f"{label}.key must be a non-empty string")
            continue
        label = f"userDefinedHeaders[{key}]"
        # Studio drops any header carrying its own value, so the user is never
        # asked for it and the server receives nothing.
        if header.get("value") or "secretBinding" in header:
            fail(f"{label} must not set value or secretBinding: Studio drops such a field")

    if not any(isinstance(h, dict) and h.get("required") is True for h in headers):
        fail("at least one userDefinedHeaders entry must be required")


def check_authorization(manifest: dict, expected: dict[str, object], fail) -> None:
    if expected["runtime"] == "remote":
        check_remote_user_scoped_auth(manifest, fail)
    else:
        check_containerized_auth(manifest, fail)


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
