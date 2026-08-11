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
                "Optional shared API key for headless access; leave blank to "
                "authorize each User with Resend OAuth."
            ),
            "key": "Authorization",
            "required": False,
            "sensitive": True,
            "prefix": "Bearer ",
        }
    ],
}

VALID_RESEND_MANIFEST = {
    "name": "Resend",
    "entryKey": "obot-resend",
    "serverUserType": "multiUser",
    "shortDescription": (
        "Send email and manage Resend audiences, broadcasts, domains, and webhooks"
    ),
    "description": (
        "The official Resend MCP server connects AI agents to Resend's hosted "
        "email platform for transactional email, contacts, broadcasts, domains, "
        "and inbound-event workflows.\n"
    ),
    "metadata": {
        "categories": "Communication, Developer Tools",
    },
    "icon": "https://resend.com/favicon.ico",
    "repoURL": "https://github.com/resend/resend-mcp",
    "runtime": "remote",
    "remoteConfig": {
        "fixedURL": "https://mcp.resend.com/mcp",
        "headers": copy.deepcopy(EXPECTED_RESEND_ENTRY["remoteHeaders"]),
    },
    "toolPreview": [
        {
            "name": "send-email",
            "description": "Send a transactional email to one or more recipients.",
            "params": {
                "to": "Recipient email addresses.",
                "subject": "Email subject line.",
                "text": "Plain-text email body.",
            },
        },
        {
            "name": "list-contacts",
            "description": "List contacts, optionally filtered to a segment.",
            "params": {
                "segmentId": "Optional segment whose contacts should be listed.",
                "limit": "Maximum contacts to return.",
            },
        },
        {
            "name": "verify-domain",
            "description": "Trigger DNS verification for a Resend sender domain.",
            "params": {
                "id": "Domain ID to verify.",
            },
        },
        {
            "name": "create-broadcast",
            "description": "Create a broadcast campaign for a Resend segment.",
            "params": {
                "name": "Broadcast name.",
                "segmentId": "Audience segment ID.",
                "subject": "Broadcast email subject.",
            },
        },
        {
            "name": "create-webhook",
            "description": "Create a webhook for Resend delivery and complaint events.",
            "params": {
                "endpoint": "Webhook destination URL.",
                "events": "Webhook event types to subscribe to.",
            },
        },
    ],
}


class CuratedEntryContractTest(unittest.TestCase):
    maxDiff = None

    def write_catalog(self, directory: Path, resend_manifest: dict) -> None:
        for filename in curated_entries.CURATED_ENTRIES:
            source = REPO_ROOT / filename
            target = directory / filename
            if filename == "resend.yaml":
                target.write_text(yaml.safe_dump(resend_manifest, sort_keys=False))
                continue
            shutil.copy(source, target)

        if "resend.yaml" not in curated_entries.CURATED_ENTRIES:
            (directory / "resend.yaml").write_text(
                yaml.safe_dump(resend_manifest, sort_keys=False)
            )

    def validate_resend_manifest(self, resend_manifest: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_catalog(root, resend_manifest)
            return curated_entries.validate(root)

    def test_curated_entries_pin_resend_remote_contract(self) -> None:
        self.assertEqual(
            curated_entries.CURATED_ENTRIES["resend.yaml"],
            EXPECTED_RESEND_ENTRY,
        )

    def test_accepts_curated_resend_manifest(self) -> None:
        self.assertEqual(self.validate_resend_manifest(copy.deepcopy(VALID_RESEND_MANIFEST)), [])

    def test_rejects_resend_contract_regressions(self) -> None:
        cases = [
            (
                "required header",
                lambda manifest: manifest["remoteConfig"]["headers"][0].__setitem__("required", True),
                "resend.yaml: remoteConfig.headers[Authorization].required must be false",
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
                "resend.yaml: env must be absent: Resend remote auth is OAuth or an optional shared bearer token",
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
                "resend.yaml: multiUserConfig.userDefinedHeaders must be absent: Resend remote auth is not configured per-user in Studio",
            ),
        ]

        for name, mutate, expected_error in cases:
            with self.subTest(name=name):
                manifest = copy.deepcopy(VALID_RESEND_MANIFEST)
                mutate(manifest)
                self.assertIn(expected_error, self.validate_resend_manifest(manifest))


if __name__ == "__main__":
    unittest.main()
