#!/usr/bin/env python3
"""Validate and publish the automated-scan coverage of the curated catalog entries.

`.github/workflows/mcpwn.yml` scans only the containerized entries a pull
request changes, and it cannot scan a server that authenticates its MCP path.
This check makes the resulting coverage explicit: every curated entry must
declare whether the scan covers it, and an entry the scan cannot cover must
record the review performed instead. The declarations are rendered into the job
summary so a reviewer never has to read workflow configuration to find out.

Run locally:  python scripts/curated-entry-contract/validate-scan-coverage.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import yaml


def load_curated_entries() -> dict[str, dict[str, object]]:
    """Read the curated entry list from the sibling contract check.

    That file is the single source of truth for which entries are ours. Its
    name is not a valid module name, so it is loaded by path rather than
    imported.
    """
    source = Path(__file__).with_name("validate-curated-entries.py")
    spec = importlib.util.spec_from_file_location("curated_entry_contract", source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CURATED_ENTRIES


CURATED_ENTRIES = load_curated_entries()

COVERAGE_FILE = Path(".github/mcpwn-scan-coverage.yaml")
VALID_SCAN_VALUES = ("automated", "manual")
# `manual` must say what replaced the scan; `automated` needs no alternative.
REQUIRED_FIELDS = {"automated": ("reason",), "manual": ("reason", "alternative")}


def load_declarations(path: Path, fail) -> dict[str, dict[str, object]]:
    """Return declarations keyed by file, reporting structural problems."""
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        fail(f"{path}: invalid YAML: {error}")
        return {}

    if not isinstance(document, dict):
        fail(f"{path}: must be a mapping")
        return {}

    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        fail(f"{path}: entries must be a non-empty list")
        return {}

    declarations: dict[str, dict[str, object]] = {}
    for index, entry in enumerate(entries):
        label = f"{path}: entries[{index}]"
        if not isinstance(entry, dict):
            fail(f"{label} must be a mapping")
            continue

        filename = entry.get("file")
        if not isinstance(filename, str) or not filename.strip():
            fail(f"{label}.file must be a non-empty string")
            continue

        label = f"{path}: {filename}"
        if filename in declarations:
            fail(f"{label} is declared more than once")
            continue

        scan = entry.get("scan")
        if scan not in VALID_SCAN_VALUES:
            fail(f"{label}.scan is {scan!r}, expected one of {list(VALID_SCAN_VALUES)}")
            continue

        for field in REQUIRED_FIELDS[scan]:
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                fail(f"{label}.{field} must be a non-empty string when scan is {scan!r}")

        declarations[filename] = entry

    return declarations


def check_coverage(root: Path, declarations: dict[str, dict[str, object]], fail) -> None:
    """Every curated entry declares its coverage, and every declaration is real."""
    for filename in sorted(CURATED_ENTRIES):
        if filename not in declarations:
            fail(
                f"{COVERAGE_FILE}: {filename} has no scan coverage declaration: "
                "a curated entry must state whether the automated scan covers it"
            )

    for filename in sorted(declarations):
        if not (root / filename).is_file():
            fail(f"{COVERAGE_FILE}: {filename} is declared but missing from the catalog")


def render_summary(declarations: dict[str, dict[str, object]]) -> str:
    """A reviewer-facing table plus the alternative recorded for each gap."""
    lines = [
        "## Automated security scan coverage",
        "",
        "| Curated entry | Runtime | Automated scan |",
        "| --- | --- | --- |",
    ]

    gaps: list[tuple[str, dict[str, object]]] = []
    for filename in sorted(CURATED_ENTRIES):
        declaration = declarations.get(filename, {})
        scan = declaration.get("scan")
        runtime = CURATED_ENTRIES[filename].get("runtime", "unknown")
        covered = "✅ covered" if scan == "automated" else "⚠️ not covered"
        lines.append(f"| `{filename}` | {runtime} | {covered} |")
        if scan == "manual":
            gaps.append((filename, declaration))

    for filename, declaration in gaps:
        lines += [
            "",
            f"### `{filename}` — reviewed manually",
            "",
            f"**Why the scan cannot cover it.** {declaration.get('reason', '').strip()}",
            "",
            f"**Review performed instead.** {declaration.get('alternative', '').strip()}",
        ]

    return "\n".join(lines) + "\n"


def publish_summary(summary: str) -> None:
    print(summary)
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(summary)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    errors: list[str] = []

    def fail(message: str) -> None:
        errors.append(message)

    path = root / COVERAGE_FILE
    if not path.is_file():
        print(f"{COVERAGE_FILE} is missing", file=sys.stderr)
        return 1

    declarations = load_declarations(path, fail)
    check_coverage(root, declarations, fail)

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    publish_summary(render_summary(declarations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
