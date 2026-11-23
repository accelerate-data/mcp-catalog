#!/usr/bin/env python3
"""Validate and publish the automated-scan coverage of the catalog entries.

`.github/workflows/mcpwn.yml` scans only the containerized entries a pull
request changes, and it cannot scan a server that authenticates its MCP path.
This check makes the resulting coverage explicit: every curated entry must
declare whether the scan covers it, and an entry the scan cannot cover must
record the review performed instead. The declarations are rendered into the job
summary so a reviewer never has to read workflow configuration to find out.

An inherited catalog entry may be declared too, so the ones the scan cannot
start carry a recorded reason instead of failing every pull request that
touches them. Only an entry the scan can select — one declaring a
containerizedConfig image — may be declared.

Run locally:  python scripts/curated-entry-contract/validate-scan-coverage.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import yaml


def load_curated_entries_module():
    """Load the sibling contract check as a module.

    Its name is not a valid module name, so it is loaded by path rather than
    imported. It owns `CURATED_ENTRIES` (which entries are ours) and
    `active_catalog_files` (which files Obot serves), so this check reuses
    both rather than re-deriving either.
    """
    source = Path(__file__).with_name("validate-curated-entries.py")
    spec = importlib.util.spec_from_file_location("curated_entry_contract", source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CURATED_ENTRIES = load_curated_entries_module().CURATED_ENTRIES

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

    version = document.get("version")
    if type(version) is not int or version != 1:
        fail(f"{path}: version must be 1")

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
    """The declarations match the entries the scan can select.

    Every entry the scan can select must be declared, not only the curated
    ones: a containerized entry the scan cannot start is exactly the one that
    needs a recorded reason, and most of those are inherited from upstream
    rather than first-party. Requiring the declaration is what keeps coverage
    accurate when a new containerized entry arrives — permitting it would let
    one be added and scanned with no recorded decision at all.
    """
    containerized = containerized_catalog_files(root)

    for filename in sorted(CURATED_ENTRIES):
        if filename not in declarations:
            fail(
                f"{COVERAGE_FILE}: {filename} has no scan coverage declaration: "
                "a curated entry must state whether the automated scan covers it"
            )

    for filename in sorted(containerized - set(CURATED_ENTRIES)):
        if filename not in declarations:
            fail(
                f"{COVERAGE_FILE}: {filename} declares a containerizedConfig image "
                "and the scan selects it, but has no coverage declaration: state "
                "whether the scan covers it, and if not, why"
            )

    for filename in sorted(declarations):
        if not (root / filename).is_file():
            fail(f"{COVERAGE_FILE}: {filename} is declared but missing from the catalog")
        elif filename not in CURATED_ENTRIES and filename not in containerized:
            fail(
                f"{COVERAGE_FILE}: {filename} is not an entry the scan can select; "
                "only a curated entry or one declaring a containerizedConfig image "
                "may declare scan coverage"
            )


def containerized_catalog_files(root: Path) -> set[str]:
    """Active catalog entries the scan matrix can select, by relative path.

    This uses the same file selection the catalog itself uses
    (`active_catalog_files`, which honours `.ignoreobotcatalogs` and skips
    dot-directories), while the fan-out selects with `find . -name '*.yaml'`.
    The two agree today because every containerized entry lives under
    `obot-images/`. A containerized entry placed under `scripts/` or `.github/`
    would be scanned by the fan-out yet not require a declaration here, so
    `test_containerized_universe_matches_the_fanout_selection` pins the two
    selections together.
    """
    catalog = load_curated_entries_module()
    selected: set[str] = set()
    for path in catalog.active_catalog_files(root):
        try:
            manifest = yaml.safe_load(path.read_text())
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            # validate-curated-entries.py reports an unreadable or unparsable
            # manifest. This check only needs to know which entries the scan
            # can select, so it skips one it cannot read rather than failing
            # the run with a traceback for a problem already reported.
            continue
        if isinstance(manifest, dict) and isinstance(
            manifest.get("containerizedConfig"), dict
        ):
            selected.add(path.relative_to(root).as_posix())
    return selected


def render_summary(declarations: dict[str, dict[str, object]], root: Path) -> str:
    """A reviewer-facing table plus the alternative recorded for each gap."""
    lines = [
        "## Automated security scan coverage",
        "",
        "| Entry | Runtime | Automated scan |",
        "| --- | --- | --- |",
    ]

    # Curated entries first, then any inherited entry declared `manual`: the
    # inherited ones the scan can start need no declaration, so the only ones
    # listed are the gaps a reviewer has to weigh.
    inherited = sorted(name for name in declarations if name not in CURATED_ENTRIES)
    gaps: list[tuple[str, dict[str, object]]] = []
    for filename in sorted(CURATED_ENTRIES) + inherited:
        declaration = declarations.get(filename, {})
        scan = declaration.get("scan")
        runtime = CURATED_ENTRIES.get(filename, {}).get("runtime") or entry_runtime(
            root / filename
        )
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


def entry_runtime(path: Path) -> str:
    try:
        manifest = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return "unknown"
    runtime = manifest.get("runtime") if isinstance(manifest, dict) else None
    return runtime if isinstance(runtime, str) else "unknown"


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

    publish_summary(render_summary(declarations, root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
