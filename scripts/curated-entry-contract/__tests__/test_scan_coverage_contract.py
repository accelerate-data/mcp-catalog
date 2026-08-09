from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
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
    def test_rejects_unknown_schema_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.yaml"
            for version in (True, 1.0, "1", 2, None):
                path.write_text(
                    yaml.safe_dump(
                        {
                            "version": version,
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
        self.assertNotIn("changed_files='${{", workflow)
        self.assertIn("CHANGED_FILES: ${{ steps.changed-files.outputs.all_changed_files }}", workflow)

    def test_docker_arguments_remain_discrete(self) -> None:
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        script = next(step["run"] for step in steps if step.get("name") == "Start mcp server")

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            capture = temp / "argv.json"
            docker = temp / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
            )
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "ARGS_JSON": json.dumps(["--label=a b", "line one\nline two", "$(false)"]),
                "CAPTURE": str(capture),
                "ENV_JSON": json.dumps([{"key": "SAFE_KEY"}]),
                "IMAGE": "example.invalid/image:tag",
                "PATH": f"{temp}:{os.environ['PATH']}",
                "PORT": "8080",
            }

            subprocess.run(["bash", "-euo", "pipefail", "-c", script], check=True, env=env)

            self.assertEqual(
                json.loads(capture.read_text()),
                [
                    "run",
                    "-d",
                    "--name",
                    "mcpwn-target",
                    "-p",
                    "8080:8080",
                    "-e",
                    "SAFE_KEY",
                    "example.invalid/image:tag",
                    "--label=a b",
                    "line one\nline two",
                    "$(false)",
                ],
            )


if __name__ == "__main__":
    unittest.main()
