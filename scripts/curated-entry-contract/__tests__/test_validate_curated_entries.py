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

with (REPO_ROOT / "resend.yaml").open() as manifest_file:
    VALID_RESEND_MANIFEST = yaml.safe_load(manifest_file)

with (REPO_ROOT / "fabric-pro-dev.yaml").open() as manifest_file:
    VALID_FABRIC_MANIFEST = yaml.safe_load(manifest_file)

with (REPO_ROOT / "remotes" / "exa_search.yaml").open() as manifest_file:
    VALID_EXA_MANIFEST = yaml.safe_load(manifest_file)

EXPECTED_RESEND_ENTRY = {
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
            "description": (
                "Shared Resend API key for the MCP deployment."
            ),
            "key": "Authorization",
            "required": True,
            "sensitive": True,
            "prefix": "Bearer ",
        }
    ],
}

class CuratedEntryContractTest(unittest.TestCase):
    maxDiff = None

    def write_catalog(self, directory: Path, overrides: dict[str, dict]) -> None:
        for filename in curated_entries.CURATED_ENTRIES:
            target = directory / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if filename in overrides:
                target.write_text(yaml.safe_dump(overrides[filename], sort_keys=False))
                continue
            shutil.copy(REPO_ROOT / filename, target)

        for filename, manifest in overrides.items():
            if filename not in curated_entries.CURATED_ENTRIES:
                (directory / filename).write_text(
                    yaml.safe_dump(manifest, sort_keys=False)
                )

    def validate_with(self, **overrides: dict) -> list[str]:
        """Validate the real catalog with the named manifests swapped out."""
        by_filename = {
            filename.replace("_", "-") + ".yaml": manifest
            for filename, manifest in overrides.items()
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_catalog(root, by_filename)
            return curated_entries.validate(root)

    def validate_resend_manifest(self, resend_manifest: dict) -> list[str]:
        return self.validate_with(resend=resend_manifest)

    def validate_exa_manifest(self, exa_manifest: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_catalog(root, {"remotes/exa_search.yaml": exa_manifest})
            return curated_entries.validate(root)

    def validate_fabric_manifest(self, fabric_manifest: dict) -> list[str]:
        return self.validate_with(fabric_pro_dev=fabric_manifest)

    def test_curated_entries_pin_resend_remote_contract(self) -> None:
        self.assertEqual(
            curated_entries.CURATED_ENTRIES["resend.yaml"],
            EXPECTED_RESEND_ENTRY,
        )

    def test_accepts_curated_resend_manifest(self) -> None:
        self.assertEqual(self.validate_resend_manifest(VALID_RESEND_MANIFEST), [])

    def test_rejects_resend_contract_regressions(self) -> None:
        cases = [
            (
                "optional header",
                lambda manifest: manifest["remoteConfig"]["headers"][0].__setitem__("required", False),
                "resend.yaml: remoteConfig.headers[Authorization].required must be true",
            ),
            (
                "non-sensitive header",
                lambda manifest: manifest["remoteConfig"]["headers"][0].__setitem__("sensitive", False),
                "resend.yaml: remoteConfig.headers[Authorization].sensitive must be true",
            ),
            (
                "missing bearer prefix",
                lambda manifest: manifest["remoteConfig"]["headers"][0].pop("prefix"),
                "resend.yaml: remoteConfig.headers[Authorization].prefix must be 'Bearer '",
            ),
            (
                "changed endpoint",
                lambda manifest: manifest["remoteConfig"].__setitem__(
                    "fixedURL", "https://example.invalid/mcp"
                ),
                "resend.yaml: remoteConfig.fixedURL is 'https://example.invalid/mcp', expected 'https://mcp.resend.com/mcp'",
            ),
            (
                "static oauth required",
                lambda manifest: manifest["remoteConfig"].__setitem__("staticOAuthRequired", True),
                "resend.yaml: remoteConfig.staticOAuthRequired must be absent",
            ),
            (
                "env contract",
                lambda manifest: manifest.__setitem__(
                    "env",
                    [
                        {
                            "key": "RESEND_API_KEY",
                            "name": "Resend API key",
                            "description": "Deployment-scoped token",
                            "required": False,
                            "sensitive": True,
                        }
                    ],
                ),
                "resend.yaml: env must be absent: Resend remote auth must stay on the required shared bearer header",
            ),
            (
                "empty env declaration",
                lambda manifest: manifest.__setitem__("env", []),
                "resend.yaml: env must be absent: Resend remote auth must stay on the required shared bearer header",
            ),
            (
                "per-user headers",
                lambda manifest: manifest.__setitem__(
                    "multiUserConfig",
                    {
                        "userDefinedHeaders": [
                            {
                                "key": "Authorization",
                                "name": "Resend API key",
                                "required": True,
                            }
                        ]
                    },
                ),
                "resend.yaml: multiUserConfig.userDefinedHeaders must be absent: Resend remote auth is instance-owned, not per-user",
            ),
            (
                "empty per-user header declaration",
                lambda manifest: manifest.__setitem__(
                    "multiUserConfig", {"userDefinedHeaders": []}
                ),
                "resend.yaml: multiUserConfig.userDefinedHeaders must be absent: Resend remote auth is instance-owned, not per-user",
            ),
            (
                "header static value",
                lambda manifest: manifest["remoteConfig"]["headers"][0].__setitem__(
                    "value", "shared-resend-key"
                ),
                "resend.yaml: remoteConfig.headers[Authorization].value must be absent: Resend remote auth requires an owner-supplied bearer credential",
            ),
            (
                "header secret binding",
                lambda manifest: manifest["remoteConfig"]["headers"][0].__setitem__(
                    "secretBinding", "resend-api-key"
                ),
                "resend.yaml: remoteConfig.headers[Authorization].secretBinding must be absent: Resend remote auth requires an owner-supplied bearer credential",
            ),
            (
                "unexpected header field",
                lambda manifest: manifest["remoteConfig"]["headers"][0].__setitem__(
                    "scope", "instance"
                ),
                "resend.yaml: remoteConfig.headers[Authorization] must contain exactly the supported fields ['description', 'key', 'name', 'prefix', 'required', 'sensitive']; unexpected fields: ['scope']",
            ),
        ]

        for name, mutate, expected_error in cases:
            with self.subTest(name=name):
                manifest = copy.deepcopy(VALID_RESEND_MANIFEST)
                mutate(manifest)
                self.assertIn(expected_error, self.validate_resend_manifest(manifest))

    def test_accepts_curated_exa_manifest(self) -> None:
        self.assertEqual(self.validate_exa_manifest(VALID_EXA_MANIFEST), [])

    def test_rejects_exa_contract_regressions(self) -> None:
        def mutate(change) -> dict:
            manifest = copy.deepcopy(VALID_EXA_MANIFEST)
            change(manifest)
            return manifest

        header = "remoteConfig.headers[x-api-key]"
        cases = [
            (
                "single user",
                mutate(lambda m: m.update(serverUserType="singleUser")),
                "exa_search.yaml: serverUserType is 'singleUser', expected 'multiUser'",
            ),
            (
                "optional key",
                mutate(lambda m: m["remoteConfig"]["headers"][0].update(required=False)),
                f"exa_search.yaml: {header}.required is False, expected True",
            ),
            (
                "non-sensitive key",
                mutate(lambda m: m["remoteConfig"]["headers"][0].update(sensitive=False)),
                f"exa_search.yaml: {header}.sensitive is False, expected True",
            ),
            (
                "wrong header",
                mutate(lambda m: m["remoteConfig"]["headers"][0].update(key="Authorization")),
                f"exa_search.yaml: {header}.key is 'Authorization', expected 'x-api-key'",
            ),
            (
                "static key value",
                mutate(lambda m: m["remoteConfig"]["headers"][0].update(value="shared-key")),
                f"exa_search.yaml: {header}.value must be absent: "
                "Exa requires an owner-supplied API key",
            ),
            (
                "credential in url",
                mutate(
                    lambda m: m["remoteConfig"].update(
                        urlTemplate="https://mcp.exa.ai/mcp?tools=${EXA_TOOLS}&exaApiKey=${EXA_API_KEY}"
                    )
                ),
                "exa_search.yaml: remoteConfig.urlTemplate must interpolate only ${EXA_TOOLS}: "
                "no credential in the URL",
            ),
            (
                "wrong endpoint",
                mutate(
                    lambda m: m["remoteConfig"].update(
                        urlTemplate="https://example.invalid/mcp?tools=${EXA_TOOLS}"
                    )
                ),
                "exa_search.yaml: remoteConfig.urlTemplate is "
                "'https://example.invalid/mcp?tools=${EXA_TOOLS}', expected "
                "'https://mcp.exa.ai/mcp?tools=${EXA_TOOLS}'",
            ),
            (
                "static oauth",
                mutate(lambda m: m["remoteConfig"].update(staticOAuthRequired=True)),
                "exa_search.yaml: remoteConfig.staticOAuthRequired must be absent",
            ),
            (
                "per-user header prompt",
                mutate(
                    lambda m: m.update(
                        multiUserConfig={"userDefinedHeaders": [{"name": "k", "key": "x-api-key"}]}
                    )
                ),
                "exa_search.yaml: multiUserConfig.userDefinedHeaders must be absent: "
                "Exa API key is instance-owned, not per-user",
            ),
            (
                "profile dropped",
                mutate(lambda m: m["env"][0]["options"].pop(0)),
                "exa_search.yaml: EXA_TOOLS options are",
            ),
            (
                "profile optional",
                mutate(lambda m: m["env"][0].update(required=False)),
                "exa_search.yaml: EXA_TOOLS must be required and non-sensitive",
            ),
            (
                "hostname",
                mutate(lambda m: m["remoteConfig"].update(hostname="mcp.exa.ai")),
                "exa_search.yaml: remoteConfig.hostname must be absent",
            ),
            (
                "secret binding",
                mutate(lambda m: m["remoteConfig"]["headers"][0].update(secretBinding="exa")),
                f"exa_search.yaml: {header}.secretBinding must be absent: "
                "Exa requires an owner-supplied API key",
            ),
            (
                "remote config not a mapping",
                mutate(lambda m: m.update(remoteConfig="https://mcp.exa.ai/mcp")),
                "exa_search.yaml: remoteConfig must be a mapping",
            ),
            (
                "literal credential in url",
                mutate(
                    lambda m: m["remoteConfig"].update(
                        urlTemplate="https://mcp.exa.ai/mcp?tools=${EXA_TOOLS}&exaApiKey=abc123"
                    )
                ),
                "exa_search.yaml: remoteConfig.urlTemplate must interpolate only ${EXA_TOOLS}: "
                "no credential in the URL",
            ),
            (
                "key interpolated instead of profile",
                mutate(
                    lambda m: m["remoteConfig"].update(
                        urlTemplate="https://mcp.exa.ai/mcp?tools=${EXA_API_KEY}"
                    )
                ),
                "exa_search.yaml: remoteConfig.urlTemplate must interpolate only ${EXA_TOOLS}: "
                "no credential in the URL",
            ),
            (
                "fixed url",
                mutate(lambda m: m["remoteConfig"].update(fixedURL="https://mcp.exa.ai/mcp")),
                "exa_search.yaml: remoteConfig.fixedURL must be absent",
            ),
            (
                "header prefix added",
                mutate(lambda m: m["remoteConfig"]["headers"][0].update(prefix="Bearer ")),
                f"exa_search.yaml: {header} must contain exactly the supported fields",
            ),
            (
                "header missing",
                mutate(lambda m: m["remoteConfig"].pop("headers")),
                "exa_search.yaml: remoteConfig.headers must contain exactly one x-api-key header",
            ),
            (
                "profile value changed",
                mutate(lambda m: m["env"][0]["options"][0].update(value="agent_run")),
                "exa_search.yaml: EXA_TOOLS options are",
            ),
            (
                "profile sensitive",
                mutate(lambda m: m["env"][0].update(sensitive=True)),
                "exa_search.yaml: EXA_TOOLS must be required and non-sensitive",
            ),
            (
                "extra env key",
                mutate(lambda m: m["env"].append({"name": "x", "key": "EXA_API_KEY"})),
                "exa_search.yaml: env keys are ['EXA_TOOLS', 'EXA_API_KEY'], expected ['EXA_TOOLS']",
            ),
        ]
        for name, manifest, expected_error in cases:
            with self.subTest(name):
                errors = self.validate_exa_manifest(manifest)
                self.assertTrue(
                    any(error.startswith(expected_error) for error in errors),
                    f"{expected_error!r} not in {errors!r}",
                )

    def test_accepts_curated_fabric_manifest(self) -> None:
        self.assertEqual(self.validate_fabric_manifest(VALID_FABRIC_MANIFEST), [])

    def test_fabric_oauth_env_keys_survive_obot_normalization(self) -> None:
        """Obot uppercases declared env keys, then matches oauth refs verbatim.

        A mixed-case spelling therefore never matches its own declaration, so
        the entry is dropped from the synced catalog (VD-4783) and the contract
        pins the normalized form.
        """
        oauth = curated_entries.CURATED_ENTRIES["fabric-pro-dev.yaml"][
            "containerizedConfig"
        ]["oauth"]
        declared = set(curated_entries.CURATED_ENTRIES["fabric-pro-dev.yaml"]["envKeys"])
        for field in ("authorityEnv", "tenantIDEnv", "clientIDEnv", "clientSecretEnv"):
            key = oauth[field]
            self.assertEqual(key, curated_entries.normalize_env_key(key))
            self.assertIn(key, declared)
        for key in declared:
            self.assertEqual(key, curated_entries.normalize_env_key(key))

    def test_rejects_fabric_oauth_env_regressions(self) -> None:
        def set_env_key(manifest: dict, old: str, new: str) -> None:
            for field in manifest["env"]:
                if field["key"] == old:
                    field["key"] = new
                    return
            raise AssertionError(f"env key {old!r} not present in the manifest")

        cases = [
            (
                "documented mixed-case env key",
                lambda manifest: set_env_key(
                    manifest, "AZUREAD__TENANTID", "AzureAd__TenantId"
                ),
                "fabric-pro-dev.yaml: env[1].key is 'AzureAd__TenantId', expected "
                "'AZUREAD__TENANTID': Obot normalizes declared env keys before matching them",
            ),
            (
                "hyphenated env key",
                lambda manifest: set_env_key(
                    manifest, "AZUREAD__TENANTID", "AZUREAD-TENANTID"
                ),
                "fabric-pro-dev.yaml: env[1].key is 'AZUREAD-TENANTID', expected "
                "'AZUREAD_TENANTID': Obot normalizes declared env keys before matching them",
            ),
            (
                "dotted env key",
                lambda manifest: set_env_key(
                    manifest, "AZUREAD__TENANTID", "AZUREAD.TENANTID"
                ),
                "fabric-pro-dev.yaml: env[1].key 'AZUREAD.TENANTID' must not contain '.'",
            ),
            (
                "oauth reference not declared in env",
                lambda manifest: manifest["containerizedConfig"]["oauth"].__setitem__(
                    "tenantIDEnv", "AzureAd__TenantId"
                ),
                "fabric-pro-dev.yaml: containerizedConfig.oauth.tenantIDEnv references "
                "'AzureAd__TenantId', which is not declared in env",
            ),
            (
                "scope templating a non-clientID variable",
                lambda manifest: manifest["containerizedConfig"]["oauth"].__setitem__(
                    "scopes", ["api://${AZUREAD__TENANTID}/Mcp.Tools.ReadWrite"]
                ),
                "fabric-pro-dev.yaml: containerizedConfig.oauth.scopes[0] references "
                "'AZUREAD__TENANTID'; Obot allows only clientIDEnv ('AZUREAD__CLIENTID')",
            ),
        ]

        for name, mutate, expected_error in cases:
            with self.subTest(name=name):
                manifest = copy.deepcopy(VALID_FABRIC_MANIFEST)
                mutate(manifest)
                errors = self.validate_fabric_manifest(manifest)
                self.assertTrue(
                    any(error.startswith(expected_error) for error in errors),
                    f"{expected_error!r} not raised; got {errors!r}",
                )


if __name__ == "__main__":
    unittest.main()
