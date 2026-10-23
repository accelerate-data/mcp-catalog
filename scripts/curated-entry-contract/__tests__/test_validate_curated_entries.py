from __future__ import annotations

import copy
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

MODULE_PATH = Path(__file__).parents[1] / "validate-curated-entries.py"
SPEC = importlib.util.spec_from_file_location("curated_entry_contract", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
curated_entries = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(curated_entries)
REPO_ROOT = MODULE_PATH.parents[2]


def load(filename: str) -> dict:
    return yaml.safe_load((REPO_ROOT / filename).read_text())


def field(manifest: dict, key: str) -> dict:
    return next(item for item in manifest["config"] if item["key"] == key)


class CuratedEntryContractTest(unittest.TestCase):
    maxDiff = None

    def validate_with(self, filename: str, manifest: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(REPO_ROOT / ".ignoreobotcatalogs", root / ".ignoreobotcatalogs")
            for curated in curated_entries.CURATED_ENTRIES:
                target = root / curated
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(REPO_ROOT / curated, target)
            target = root / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(yaml.safe_dump(manifest, sort_keys=False))
            return curated_entries.validate(root)

    def assert_regressions(self, filename: str, cases: list) -> None:
        for name, change, expected in cases:
            with self.subTest(name=name):
                manifest = copy.deepcopy(load(filename))
                change(manifest)
                errors = self.validate_with(filename, manifest)
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_accepts_native_catalog(self) -> None:
        self.assertEqual(curated_entries.validate(REPO_ROOT), [])

    def test_rejects_legacy_shapes_in_non_curated_entries(self) -> None:
        # A legacy field must fail even when empty or coexisting with valid config.
        cases = [
            ({"env": []}, "env is deprecated"),
            ({"serverUserType": "multiUser"}, "serverUserType is deprecated"),
            ({"multiUserConfig": {}}, "multiUserConfig is deprecated"),
            ({"remoteConfig": {"headers": []}}, "remoteConfig.headers is deprecated"),
            (
                {"containerizedConfig": {"headers": []}},
                "containerizedConfig.headers is deprecated",
            ),
        ]
        for filename in (
            "unreviewed.yaml",
            "remotes/unreviewed.yaml",
            "obot-images/unreviewed.yaml",
            "obot-remotes/unreviewed.yaml",
        ):
            for legacy, expected in cases:
                with self.subTest(filename=filename, legacy=legacy):
                    manifest = {
                        "config": [{"key": "TOKEN", "usage": "header"}],
                        **legacy,
                    }
                    self.assertTrue(
                        any(
                            expected in e
                            for e in self.validate_with(filename, manifest)
                        )
                    )

    def test_ignores_hidden_and_non_catalog_yaml(self) -> None:
        for filename in (
            ".github/workflows/test.yaml",
            ".hidden.yaml",
            "remotes/.hidden/test.yaml",
            "scripts/fixture.yaml",
        ):
            with self.subTest(filename=filename):
                self.assertEqual(self.validate_with(filename, {"env": []}), [])

    def test_rejects_malformed_native_config_in_non_curated_entries(self) -> None:
        cases = [
            ({}, "config must be a list"),
            ([None], "must be a mapping"),
            ([{"key": "KEY"}], ".usage must be a native"),
            ([{"key": "KEY", "usage": "headers"}], ".usage must be a native"),
            ([{"key": "", "usage": "env"}], ".key must be a non-empty"),
            ([{"key": "KEY", "usage": "env"}] * 2, "is duplicated"),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                self.assertTrue(
                    any(
                        expected in e
                        for e in self.validate_with("other.yaml", {"config": config})
                    )
                )
        for legacy in ("file", "dynamicFile", "interpolated"):
            config = [{"key": "KEY", "usage": "env", legacy: True}]
            self.assertTrue(
                any(
                    "is deprecated" in e
                    for e in self.validate_with("other.yaml", {"config": config})
                )
            )

    def test_accepts_all_native_usages(self) -> None:
        config = [
            {"key": usage, "usage": usage}
            for usage in ("env", "header", "file", "dynamicFile", "interpolated")
        ]
        self.assertEqual(self.validate_with("other.yaml", {"config": config}), [])

    def test_resend_requires_owner_supplied_bearer_header(self) -> None:
        cases = [
            (
                "optional",
                lambda m: field(m, "Authorization").update(required=False),
                ".required must be true",
            ),
            (
                "not secret",
                lambda m: field(m, "Authorization").update(sensitive=False),
                ".sensitive must be true",
            ),
            (
                "prefix",
                lambda m: field(m, "Authorization").pop("prefix"),
                ".prefix must be 'Bearer '",
            ),
            (
                "wrong endpoint",
                lambda m: m["remoteConfig"].update(fixedURL="https://example.invalid"),
                "remoteConfig.fixedURL is",
            ),
            (
                "oauth",
                lambda m: m["remoteConfig"].update(staticOAuthRequired=True),
                "staticOAuthRequired must be absent",
            ),
            (
                "wrong usage",
                lambda m: field(m, "Authorization").update(usage="env"),
                "config must contain only",
            ),
            (
                "extra config",
                lambda m: m["config"].append({"key": "ENV", "usage": "env"}),
                "config must contain only",
            ),
            (
                "extra header",
                lambda m: m["config"].append({"key": "EXTRA", "usage": "header"}),
                "exactly one Authorization",
            ),
            (
                "wrong key",
                lambda m: field(m, "Authorization").update(key="OTHER"),
                "expected 'Authorization'",
            ),
        ]
        for key in ("value", "static", "secretBinding", "userAllowed"):
            cases.append(
                (
                    key,
                    lambda m, key=key: field(m, "Authorization").update({key: True}),
                    f".{key} must be absent",
                )
            )
        self.assert_regressions("resend.yaml", cases)

    def test_exa_preserves_profile_options_and_secret_header(self) -> None:
        cases = [
            (
                "optional key",
                lambda m: field(m, "x-api-key").update(required=False),
                ".required is False",
            ),
            (
                "public key",
                lambda m: field(m, "x-api-key").update(sensitive=False),
                ".sensitive is False",
            ),
            (
                "wrong header",
                lambda m: field(m, "x-api-key").update(key="Authorization"),
                "config keys are",
            ),
            (
                "wrong usage",
                lambda m: field(m, "x-api-key").update(usage="env"),
                "exactly one x-api-key",
            ),
            (
                "header prefix",
                lambda m: field(m, "x-api-key").update(prefix="Bearer "),
                "exactly the supported fields",
            ),
            (
                "missing header",
                lambda m: m["config"].remove(field(m, "x-api-key")),
                "exactly one x-api-key",
            ),
            (
                "profile usage",
                lambda m: field(m, "EXA_TOOLS").update(usage="interpolated"),
                "EXA_TOOLS must have usage env",
            ),
            (
                "profile optional",
                lambda m: field(m, "EXA_TOOLS").update(required=False),
                "required and non-sensitive",
            ),
            (
                "profile secret",
                lambda m: field(m, "EXA_TOOLS").update(sensitive=True),
                "required and non-sensitive",
            ),
            (
                "profile removed",
                lambda m: field(m, "EXA_TOOLS")["options"].pop(),
                "EXA_TOOLS options are",
            ),
            (
                "profile changed",
                lambda m: field(m, "EXA_TOOLS")["options"][0].update(value="agent_run"),
                "EXA_TOOLS options are",
            ),
            (
                "extra config",
                lambda m: m["config"].append({"key": "EXTRA", "usage": "env"}),
                "config keys are",
            ),
            (
                "wrong endpoint",
                lambda m: m["remoteConfig"].update(
                    urlTemplate="https://example.invalid/?tools=${EXA_TOOLS}"
                ),
                "remoteConfig.urlTemplate is",
            ),
            (
                "malformed remote",
                lambda m: m.update(remoteConfig="not a map"),
                "remoteConfig must be a mapping",
            ),
        ]
        for key in ("value", "static", "secretBinding", "userAllowed"):
            cases.append(
                (
                    key,
                    lambda m, key=key: field(m, "x-api-key").update({key: True}),
                    f".{key} must be absent",
                )
            )
        for key in ("fixedURL", "hostname", "staticOAuthRequired"):
            cases.append(
                (
                    key,
                    lambda m, key=key: m["remoteConfig"].update({key: True}),
                    f"remoteConfig.{key} must be absent",
                )
            )
        for url in (
            "https://mcp.exa.ai/mcp?tools=${EXA_TOOLS}&exaApiKey=${EXA_API_KEY}",
            "https://mcp.exa.ai/mcp?tools=${EXA_TOOLS}&exaApiKey=literal",
            "https://mcp.exa.ai/mcp?tools=${EXA_API_KEY}",
        ):
            cases.append(
                (
                    url,
                    lambda m, url=url: m["remoteConfig"].update(urlTemplate=url),
                    "must interpolate only ${EXA_TOOLS}",
                )
            )
        self.assert_regressions("remotes/exa_search.yaml", cases)

    def test_mailgun_preserves_env_only_package_contract(self) -> None:
        self.assert_regressions(
            "mailgun.yaml",
            [
                (
                    "package",
                    lambda m: m["npxConfig"].update(
                        package="@mailgun/mcp-server@latest"
                    ),
                    "npxConfig.package is",
                ),
                (
                    "args",
                    lambda m: m["npxConfig"].update(args=[]),
                    "npxConfig.args must be absent",
                ),
                (
                    "header",
                    lambda m: field(m, "MAILGUN_API_KEY").update(usage="header"),
                    "stdio cannot carry request headers",
                ),
                (
                    "optional key",
                    lambda m: field(m, "MAILGUN_API_KEY").update(required=False),
                    "required and sensitive",
                ),
                (
                    "public key",
                    lambda m: field(m, "MAILGUN_API_KEY").update(sensitive=False),
                    "required and sensitive",
                ),
                (
                    "required region",
                    lambda m: field(m, "MAILGUN_API_REGION").update(required=True),
                    "optional and non-sensitive",
                ),
                (
                    "static key",
                    lambda m: field(m, "MAILGUN_API_KEY").update(value="secret"),
                    "config.value must be absent",
                ),
                (
                    "extra env",
                    lambda m: m["config"].append(
                        {"key": "MAILGUN_API_HOSTNAME", "usage": "env"}
                    ),
                    "must not be exposed",
                ),
            ],
        )

    def test_fabric_core_keeps_oauth_without_deployment_credentials(self) -> None:
        self.assert_regressions(
            "fabric-core.yaml",
            [
                (
                    "oauth disabled",
                    lambda m: m["remoteConfig"].update(staticOAuthRequired=False),
                    "expected True",
                ),
                (
                    "endpoint changed",
                    lambda m: m["remoteConfig"].update(
                        fixedURL="https://example.invalid"
                    ),
                    "remoteConfig.fixedURL is",
                ),
                (
                    "shared credential",
                    lambda m: m.update(
                        config=[{"key": "Authorization", "usage": "header"}]
                    ),
                    "authorization is user-scoped",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
