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
                with self.subTest(version=version):
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

    def test_rejects_declaration_for_an_entry_the_scan_cannot_select(self) -> None:
        """Only a curated entry or a containerized one may be declared.

        An inherited entry is declarable so the ones the scan cannot start can
        carry a recorded reason; a remote entry is not, because the scan matrix
        never selects it and a declaration there would claim a gap that does
        not exist.
        """
        errors: list[str] = []
        declarations = {
            "fabric-core.yaml": {"scan": "manual"},
            "remotes/zoom.yaml": {"scan": "manual"},
        }

        scan_coverage.check_coverage(Path.cwd(), declarations, errors.append)

        self.assertTrue(
            any(
                "remotes/zoom.yaml is not an entry the scan can select" in error
                for error in errors
            ),
            errors,
        )

    def test_accepts_a_containerized_entry_the_scan_cannot_start(self) -> None:
        """Every containerized entry the scan selects must be declared.

        The declaration is required, not merely permitted: that is what keeps
        coverage accurate when a new containerized entry arrives, instead of
        letting it be added and scanned with no recorded decision.
        """
        errors: list[str] = []
        declarations = {
            name: {"scan": "manual", "reason": "r", "alternative": "a"}
            for name in scan_coverage.CURATED_ENTRIES
        }
        declarations["obot-images/brave_search.yaml"] = {
            "scan": "manual",
            "reason": "r",
            "alternative": "a",
        }

        # Incomplete: the other containerized entries the scan selects are
        # undeclared, and each must be reported.
        scan_coverage.check_coverage(Path.cwd(), declarations, errors.append)
        self.assertTrue(
            any(
                "declares a containerizedConfig image" in error and "no coverage" in error
                for error in errors
            ),
            errors,
        )

        # Complete: declaring every containerized entry plus every curated one
        # passes. The set is read from the tree independently of the function
        # under test, so this cannot agree with a broken implementation.
        root = Path.cwd()
        containerized: set[str] = set()
        for path in root.rglob("*.yaml"):
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            manifest = yaml.safe_load(path.read_text())
            if isinstance(manifest, dict) and isinstance(
                manifest.get("containerizedConfig"), dict
            ):
                containerized.add(relative.as_posix())

        complete = {
            name: {"scan": "manual", "reason": "r", "alternative": "a"}
            for name in containerized | set(scan_coverage.CURATED_ENTRIES)
        }
        errors = []
        scan_coverage.check_coverage(root, complete, errors.append)
        self.assertEqual(errors, [])
        # The independent walk and the implementation must agree, so a change
        # to either is visible here.
        self.assertEqual(containerized, scan_coverage.containerized_catalog_files(root))

    def test_containerized_universe_matches_the_fanout_selection(self) -> None:
        """The entries that must be declared are the ones the fan-out scans.

        The validator selects through the catalog's own ignore rules while the
        fan-out uses `find . -name '*.yaml'`. They agree today because every
        containerized entry lives under `obot-images/`; this fails if one is
        added somewhere the validator skips, which would be scanned yet never
        require a declaration.
        """
        root = Path.cwd()
        fanout: set[str] = set()
        for path in root.rglob("*.yaml"):
            relative = path.relative_to(root)
            manifest = yaml.safe_load(path.read_text())
            if isinstance(manifest, dict) and isinstance(
                manifest.get("containerizedConfig"), dict
            ):
                fanout.add(relative.as_posix())

        self.assertEqual(
            fanout - scan_coverage.containerized_catalog_files(root),
            set(),
            "these containerized entries are scanned by the fan-out but could "
            "not be declared unscannable",
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
                "ENV_JSON": json.dumps([{"key": "SAFE_KEY", "value": "a value"}]),
                "GITHUB_ENV": str(temp / "github_env"),
                "IMAGE": "example.invalid/image:tag",
                "MCP_PATH": "/mcp",
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
                    "SAFE_KEY=a value",
                    "example.invalid/image:tag",
                    "--label=a b",
                    "line one\nline two",
                    "$(false)",
                ],
            )

    def test_static_values_are_forwarded_and_the_declared_path_is_used(self) -> None:
        """A static value reaches docker, and the scan probes the declared path.

        The PR lane runs without repository secrets, so a static config value
        is the only credential-shaped input it can supply. It has to arrive as
        KEY=VALUE: a bare KEY reads the runner's environment instead.
        """
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        start = next(step["run"] for step in steps if step.get("name") == "Start mcp server")
        scan = next(step["run"] for step in steps if step.get("name") == "Run mcpwn scan")

        # The scan probes the declared path, not the root.
        self.assertIn('"http://localhost:8080${MCP_PATH}"', scan)

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            capture = temp / "argv.json"
            github_env = temp / "github_env"
            docker = temp / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
            )
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "ARGS_JSON": json.dumps(["serve"]),
                "CAPTURE": str(capture),
                "ENV_JSON": json.dumps(
                    [{"key": "DANGEROUSLY_DISABLE_OAUTH", "value": "true"}]
                ),
                "GITHUB_ENV": str(github_env),
                "IMAGE": "example.invalid/image:tag",
                "MCP_PATH": "/tableau-mcp",
                "PATH": f"{temp}:{os.environ['PATH']}",
                "PORT": "8098",
            }

            subprocess.run(["bash", "-euo", "pipefail", "-c", start], check=True, env=env)

            argv = json.loads(capture.read_text())
            self.assertIn("DANGEROUSLY_DISABLE_OAUTH=true", argv)
            self.assertIn("8080:8098", argv)
            self.assertIn("MCP_PATH=/tableau-mcp", github_env.read_text())

    def test_an_entry_without_static_values_forwards_no_environment(self) -> None:
        """An entry with no static value starts the container with no -e flag.

        Most inherited entries take their inputs from the operator, so their
        filtered env list is empty; the step must still start the container.
        """
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        start = next(step["run"] for step in steps if step.get("name") == "Start mcp server")

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
                "ARGS_JSON": json.dumps(["serve"]),
                "CAPTURE": str(capture),
                "ENV_JSON": json.dumps([]),
                "GITHUB_ENV": str(temp / "github_env"),
                "IMAGE": "example.invalid/image:tag",
                "MCP_PATH": "/",
                "PATH": f"{temp}:{os.environ['PATH']}",
                "PORT": "8080",
            }

            subprocess.run(["bash", "-euo", "pipefail", "-c", start], check=True, env=env)

            argv = json.loads(capture.read_text())
            self.assertNotIn("-e", argv)
            self.assertEqual(argv[-2:], ["example.invalid/image:tag", "serve"])

    def test_a_missing_declared_path_falls_back_to_the_root(self) -> None:
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        start = next(step["run"] for step in steps if step.get("name") == "Start mcp server")

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            github_env = temp / "github_env"
            docker = temp / "docker"
            docker.write_text("#!/usr/bin/env python3\nimport sys\n")
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "ARGS_JSON": json.dumps(["serve"]),
                "ENV_JSON": json.dumps([]),
                "GITHUB_ENV": str(github_env),
                "IMAGE": "example.invalid/image:tag",
                "MCP_PATH": "",
                "PATH": f"{temp}:{os.environ['PATH']}",
                "PORT": "8080",
            }

            subprocess.run(["bash", "-euo", "pipefail", "-c", start], check=True, env=env)

            self.assertIn("MCP_PATH=/", github_env.read_text())

    def test_readiness_wait_is_bounded_and_reports_container_logs(self) -> None:
        """A bounded wait replaces the race the scanner used to lose.

        `docker run -d` returns as soon as the container is created, so the
        scan connected about a second later and failed with
        ConnectionRefusedError on an image that needs longer to bind.
        """
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        wait = next(
            step["run"]
            for step in steps
            if step.get("name") == "Wait for the mcp server to accept connections"
        )

        self.assertIn("READY_TIMEOUT_SECONDS=${READY_TIMEOUT_SECONDS:-60}", wait)
        # A container that exits is reported with its own logs, not left to the
        # scanner to describe as a refused connection.
        self.assertIn("docker logs mcpwn-target", wait)
        self.assertIn("exited", wait)
        # Readiness must be an HTTP response. Docker's userland proxy accepts a
        # TCP connection to the published port even when nothing inside the
        # container is listening, so a connect test passes against a container
        # that has not started serving.
        self.assertIn("curl", wait)
        self.assertNotIn("/dev/tcp/", wait)
        # The wait must be ordered before the scan.
        names = [step.get("name") for step in steps]
        self.assertLess(
            names.index("Wait for the mcp server to accept connections"),
            names.index("Run mcpwn scan"),
        )

    def test_start_step_rejects_a_path_that_would_escape_github_env(self) -> None:
        """A declared path cannot inject a variable into later steps.

        The path comes from catalog YAML in the pull request, and the start
        step writes it to $GITHUB_ENV. A newline there starts a second
        assignment, so an entry could set an arbitrary variable in the steps
        that follow.
        """
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        start = next(step["run"] for step in steps if step.get("name") == "Start mcp server")

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            docker = temp / "docker"
            docker.write_text("#!/usr/bin/env python3\nimport sys\n")
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            github_env = temp / "github_env"
            github_env.write_text("")
            env = os.environ | {
                "ARGS_JSON": json.dumps(["serve"]),
                "ENV_JSON": json.dumps([]),
                "GITHUB_ENV": str(github_env),
                "IMAGE": "example.invalid/image:tag",
                "MCP_PATH": "/ok\nINJECTED=1",
                "PATH": f"{temp}:{os.environ['PATH']}",
                "PORT": "8080",
            }

            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", start],
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("Invalid MCP endpoint path", result.stderr)
            self.assertNotIn("INJECTED", github_env.read_text())

    def test_start_step_accepts_an_entry_that_declares_no_args(self) -> None:
        """`toJson` of an absent property is the string "null".

        Four containerized entries declare no `args` at all, so the payload the
        matrix produces for them is `null`. Rejecting that stops the container
        from starting and breaks exactly the entries the scan covers.
        """
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        start = next(step["run"] for step in steps if step.get("name") == "Start mcp server")

        for args_payload in ("null", "[]"):
            with self.subTest(args=args_payload):
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
                        "ARGS_JSON": args_payload,
                        "CAPTURE": str(capture),
                        "ENV_JSON": json.dumps([]),
                        "GITHUB_ENV": str(temp / "github_env"),
                        "IMAGE": "example.invalid/image:tag",
                        "MCP_PATH": "/",
                        "PATH": f"{temp}:{os.environ['PATH']}",
                        "PORT": "8080",
                    }

                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", start],
                        env=env,
                        capture_output=True,
                        text=True,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(capture.read_text())[-1], "example.invalid/image:tag")

    def test_start_step_rejects_a_malformed_env_payload(self) -> None:
        """A payload that is not a JSON array fails here, not as a timeout.

        A failing jq inside a process substitution is invisible even under
        `set -o pipefail`, so the container would start with no inputs and the
        readiness wait would report a timeout instead of the real cause. An
        array of the wrong element shape has the same effect, so the elements
        are checked too.
        """
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        start = next(step["run"] for step in steps if step.get("name") == "Start mcp server")

        for env_payload, why in (
            ("not-json", "not JSON"),
            (json.dumps([{"novalue": 1}]), "an element with no string key"),
            (json.dumps([1, 2]), "an element that is not an object"),
        ):
            with self.subTest(payload=env_payload):
                with tempfile.TemporaryDirectory() as directory:
                    temp = Path(directory)
                    docker = temp / "docker"
                    docker.write_text("#!/usr/bin/env python3\nimport sys\n")
                    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
                    env = os.environ | {
                        "ARGS_JSON": json.dumps(["serve"]),
                        "ENV_JSON": env_payload,
                        "GITHUB_ENV": str(temp / "github_env"),
                        "IMAGE": "example.invalid/image:tag",
                        "MCP_PATH": "/",
                        "PATH": f"{temp}:{os.environ['PATH']}",
                        "PORT": "8080",
                    }

                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", start],
                        env=env,
                        capture_output=True,
                        text=True,
                    )

                    self.assertNotEqual(result.returncode, 0, f"{why} must be rejected")
                    self.assertIn("ENV_JSON must be", result.stderr)

    def test_worker_validates_env_names_and_keeps_no_secrets_scope(self) -> None:
        """The worker rejects a malformed env name, as the PR lane does.

        It also no longer needs the runner's secrets: nothing is started from
        the runner environment now that bare `-e KEY` forwarding is gone.
        """
        workflow = yaml.safe_load(
            (Path.cwd() / ".github/workflows/mcpwn-worker.yml").read_text()
        )
        by_name = {step.get("name"): step for step in workflow["jobs"]["scan"]["steps"]}

        self.assertIn('A-Za-z_][A-Za-z0-9_]*', by_name["Start mcp server"]["run"])
        # No step in this workflow may take the runner's secrets.
        for step in workflow["jobs"]["scan"]["steps"]:
            self.assertNotIn(
                "secrets",
                json.dumps(step.get("env", {})),
                f"{step.get('name')} still scopes the runner secrets",
            )

    def test_readiness_wait_rejects_an_inconclusive_status(self) -> None:
        """A status that is not a real HTTP code must not read as ready.

        Docker's userland proxy completes the TCP handshake on a published
        port even when nothing inside the container is listening, so readiness
        has to come from an HTTP response. `000` is curl's "could not
        connect", and an empty value means curl was missing or silent; neither
        may be mistaken for a serving container.
        """
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        wait = next(
            step["run"]
            for step in steps
            if step.get("name") == "Wait for the mcp server to accept connections"
        )

        for reported in ("000", ""):
            with self.subTest(reported=reported):
                with tempfile.TemporaryDirectory() as directory:
                    temp = Path(directory)
                    log = temp / "container.log"
                    log.write_text("still starting\n")
                    docker = temp / "docker"
                    docker.write_text(
                        "#!/usr/bin/env python3\n"
                        "import os, pathlib, sys\n"
                        "if sys.argv[1] == 'inspect':\n"
                        "    print('running')\n"
                        "elif sys.argv[1] == 'logs':\n"
                        "    print(pathlib.Path(os.environ['LOGS']).read_text(), end='')\n"
                    )
                    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
                    curl = temp / "curl"
                    curl.write_text(
                        "#!/usr/bin/env python3\n"
                        f"import sys; sys.stdout.write({reported!r})\n"
                    )
                    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
                    env = os.environ | {
                        "LOGS": str(log),
                        "PATH": f"{temp}:{os.environ['PATH']}",
                        "PORT": "8080",
                        "READY_TIMEOUT_SECONDS": "2",
                    }

                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", wait],
                        env=env,
                        capture_output=True,
                        text=True,
                    )

                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("did not answer HTTP", result.stderr + result.stdout)

    def test_readiness_wait_accepts_any_real_http_status(self) -> None:
        """A 404 still means the server is answering.

        Readiness proves the server is reachable; the declared path is what the
        scan reports on, so an entry serving elsewhere must surface as the
        scan's own 404 rather than as a readiness timeout.
        """
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        wait = next(
            step["run"]
            for step in steps
            if step.get("name") == "Wait for the mcp server to accept connections"
        )

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            docker = temp / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if sys.argv[1] == 'inspect':\n"
                "    print('running')\n"
                "elif sys.argv[1] == 'logs':\n"
                "    pass\n"
            )
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            curl = temp / "curl"
            curl.write_text("#!/usr/bin/env python3\nimport sys; sys.stdout.write('404')\n")
            curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "PATH": f"{temp}:{os.environ['PATH']}",
                "PORT": "8080",
                "READY_TIMEOUT_SECONDS": "2",
            }

            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", wait], env=env, capture_output=True, text=True
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("answered HTTP 404", result.stdout)

    def test_readiness_wait_fails_when_the_container_exits(self) -> None:
        """The wait reports the container's logs and fails when it exited."""
        workflow = yaml.safe_load((Path.cwd() / ".github/workflows/mcpwn.yml").read_text())
        steps = workflow["jobs"]["mcpwn-scan"]["steps"]
        wait = next(
            step["run"]
            for step in steps
            if step.get("name") == "Wait for the mcp server to accept connections"
        )

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            calls = temp / "calls.txt"
            log = temp / "container.log"
            log.write_text("Error: --brave-api-key is required\n")
            docker = temp / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, sys\n"
                "with open(os.environ['CALLS'], 'a') as handle:\n"
                "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
                "if sys.argv[1] == 'inspect':\n"
                "    print('exited')\n"
                "elif sys.argv[1] == 'logs':\n"
                "    print(pathlib.Path(os.environ['LOGS']).read_text(), end='')\n"
            )
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            # The endpoint never answers, so the wait must run out and fail.
            curl = temp / "curl"
            curl.write_text("#!/usr/bin/env python3\nimport sys\nprint('000')\n")
            curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CALLS": str(calls),
                "LOGS": str(log),
                "PATH": f"{temp}:{os.environ['PATH']}",
                "PORT": "8080",
                "READY_TIMEOUT_SECONDS": "2",
            }

            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", wait], env=env, capture_output=True, text=True
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("--brave-api-key is required", result.stdout + result.stderr)

    def test_worker_probes_the_declared_path_and_waits_for_the_port(self) -> None:
        """The weekly fan-out worker has the same two defects as the PR lane.

        It used a fixed `sleep 2`, which does not cover an image that needs
        longer to bind, and it always probed the root. Both are fixed here so
        the scheduled run stops failing for reasons the PR lane no longer has.
        """
        workflow = yaml.safe_load(
            (Path.cwd() / ".github/workflows/mcpwn-worker.yml").read_text()
        )
        steps = workflow["jobs"]["scan"]["steps"]
        by_name = {step.get("name"): step for step in steps}

        self.assertIn("path", workflow[True]["workflow_dispatch"]["inputs"])
        scan = by_name["Run mcpwn scan"]["run"]
        self.assertIn('"http://localhost:8080${MCP_PATH}"', scan)

        # MCP_PATH has to survive into the scan step, which is a later step
        # than the wait. Scoping it to the wait made every worker probe the
        # root again, which is the defect this change exists to fix.
        decode = by_name["Decode args/env and build docker flags"]["run"]
        self.assertIn("MCP_PATH=", decode)
        self.assertNotIn("MCP_PATH", by_name["Wait for the mcp server to accept connections"].get("env", {}))

        wait = by_name["Wait for the mcp server to accept connections"]["run"]
        self.assertIn("READY_TIMEOUT_SECONDS=${READY_TIMEOUT_SECONDS:-60}", wait)
        self.assertIn("docker logs", wait)
        self.assertIn("curl", wait)
        self.assertNotIn("/dev/tcp/", wait)
        self.assertNotIn("sleep 2\n", wait)

        # A static value is forwarded as KEY=VALUE, not as a bare key.
        start = by_name["Start mcp server"]["run"]
        self.assertIn('-e "$key=$value"', start)
        self.assertNotIn("ARGS_STRING", start)

    def test_worker_does_not_interpolate_dispatch_inputs_into_shell(self) -> None:
        """A dispatch input must reach the shell through the environment.

        The worker runs with every repository secret in scope, so an input
        spliced into the script could close a quote and run a command. Each
        value is passed as an env entry and read as a variable instead.
        """
        workflow = yaml.safe_load(
            (Path.cwd() / ".github/workflows/mcpwn-worker.yml").read_text()
        )
        for step in workflow["jobs"]["scan"]["steps"]:
            script = step.get("run", "")
            for expression in ("${{ inputs.port }}", "${{ inputs.image }}", "${{ inputs.path }}"):
                self.assertNotIn(
                    expression,
                    script,
                    f"{step.get('name')} interpolates {expression} into shell source",
                )

    def test_fanout_passes_the_declared_path_to_the_worker(self) -> None:
        workflow = yaml.safe_load(
            (Path.cwd() / ".github/workflows/mcpwn-fanout.yml").read_text()
        )
        dispatch = next(
            step
            for step in workflow["jobs"]["dispatch"]["steps"]
            if step.get("name") == "Dispatch one worker run per target"
        )

        self.assertIn("path: String(t.containerizedConfig.path || '/')", dispatch["with"]["script"])


if __name__ == "__main__":
    unittest.main()
