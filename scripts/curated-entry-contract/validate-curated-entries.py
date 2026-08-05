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
# entry; the structural checks below apply to all of them.
CURATED_ENTRIES: dict[str, dict[str, object]] = {
    "fabric-core.yaml": {
        "name": "Fabric Core",
        "entryKey": "obot-fabric-core",
        "serverUserType": "multiUser",
        "runtime": "remote",
        "fixedURL": "https://api.fabric.microsoft.com/v1/mcp/core",
        "staticOAuthRequired": True,
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

    remote_config = manifest.get("remoteConfig")
    if not isinstance(remote_config, dict):
        fail("remoteConfig must be a mapping")
        return

    fixed_url = remote_config.get("fixedURL")
    if fixed_url != expected["fixedURL"]:
        fail(f"remoteConfig.fixedURL is {fixed_url!r}, expected {expected['fixedURL']!r}")

    static_oauth = remote_config.get("staticOAuthRequired")
    if static_oauth is not expected["staticOAuthRequired"]:
        fail(
            f"remoteConfig.staticOAuthRequired is {static_oauth!r}, "
            f"expected {expected['staticOAuthRequired']!r}"
        )


def check_user_scoped_auth(manifest: dict, fail) -> None:
    """A user-authorized remote entry must configure no deployment-wide credential.

    An env block or a remoteConfig header would make an operator-supplied secret
    part of the entry, which is exactly what user-scoped OAuth must not require.
    """
    if manifest.get("env"):
        fail("env must be absent: authorization is user-scoped, not deployment-configured")

    headers = (manifest.get("remoteConfig") or {}).get("headers")
    if headers:
        fail("remoteConfig.headers must be absent: no static credential is sent to the server")


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
        check_user_scoped_auth(manifest, fail)
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
