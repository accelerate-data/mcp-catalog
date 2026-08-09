from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml


MODULE_PATH = Path(__file__).parents[1] / "validate-scan-coverage.py"
SPEC = importlib.util.spec_from_file_location("scan_coverage_contract", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
scan_coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan_coverage)


class ScanCoverageContractTest(unittest.TestCase):
    def test_rejects_unknown_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "version": 2,
                        "entries": [
                            {
                                "file": "fabric-core.yaml",
                                "scan": "manual",
                                "reason": "remote runtime",
                                "alternative": "contract review",
                            }
                        ],
                    }
                )
            )
            errors: list[str] = []

            scan_coverage.load_declarations(path, errors.append)

            self.assertIn(f"{path}: version must be 1", errors)

    def test_rejects_declaration_for_non_curated_catalog_entry(self) -> None:
        errors: list[str] = []
        declarations = {
            "fabric-core.yaml": {"scan": "manual"},
            "aws.yaml": {"scan": "manual"},
        }

        scan_coverage.check_coverage(Path.cwd(), declarations, errors.append)

        self.assertIn(
            ".github/mcpwn-scan-coverage.yaml: aws.yaml is not a curated entry; "
            "only curated entries may declare scan coverage",
            errors,
        )

    def test_pr_scan_runs_without_target_privileges_or_secrets(self) -> None:
        workflow = (Path.cwd() / ".github/workflows/mcpwn.yml").read_text()

        self.assertIn("  pull_request:\n", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("env: ${{ secrets }}", workflow)
        self.assertNotIn("pull-requests: write", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertIn('docker "${docker_args[@]}" "$IMAGE" "${server_args[@]}"', workflow)


if __name__ == "__main__":
    unittest.main()
