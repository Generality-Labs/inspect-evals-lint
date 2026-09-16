"""Sandbox checks: registry-pulled compose images must be pinned."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from inspect_evals_lint.models import LintReport, LintResult

CHECK_NAME = "sandbox_image_pinning"

PIN_ADVICE = (
    "pin it: use a dated tag you publish yourself for images you rebuild "
    "(e.g. ':2026-08-01'), an immutable upstream tag for images you don't "
    "control, or an @sha256 digest if no immutable tag exists"
)

ALLOWLIST_LOCATION = "[tool.inspect-evals-lint.sandbox_image_allowlist] in pyproject.toml"


def _is_pinned(image: str) -> bool:
    if "@sha256:" in image:
        return True
    last_segment = image.rsplit("/", 1)[-1]
    tag = last_segment.split(":", 1)[1] if ":" in last_segment else None
    return tag is not None and tag != "latest"


def check_sandbox_image_pinning(
    eval_path: Path,
    report: LintReport,
    allowlist: frozenset[tuple[str, str]] = frozenset(),
) -> None:
    """Fail on untagged or ``:latest`` images in ``compose*.y*ml`` files under ``eval_path``.

    A floating reference resolves to whatever the registry currently holds, so a
    registry push silently changes the evaluation environment. Services built
    locally (``build:``) and env-var interpolated references are skipped.
    ``allowlist`` entries ``(eval_name, image)`` warn instead of failing, and a
    stale entry warns so it gets removed.
    """
    eval_name = eval_path.name
    compose_files = sorted(eval_path.rglob("compose*.y*ml"))
    if not compose_files:
        report.add(LintResult(name=CHECK_NAME, status="skip", message="No compose files found"))
        return

    seen_allowlisted: set[tuple[str, str]] = set()
    failed = False
    checked_images = 0
    for compose_file in compose_files:
        try:
            compose: Any = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            report.add(
                LintResult(
                    name=CHECK_NAME,
                    status="warn",
                    message=f"Could not parse compose file: {e}",
                    file=str(compose_file),
                )
            )
            failed = True
            continue
        if not isinstance(compose, dict):
            continue
        services: Any = cast(dict[str, Any], compose).get("services")
        if not isinstance(services, dict):
            continue
        for service_name, raw_service in cast(dict[str, Any], services).items():
            if not isinstance(raw_service, dict):
                continue
            service = cast(dict[str, Any], raw_service)
            image: Any = service.get("image")
            if (
                not isinstance(image, str)
                or "build" in service
                or "${" in image
                or _is_pinned(image)
            ):
                if isinstance(image, str):
                    checked_images += 1
                continue
            checked_images += 1
            if (eval_name, image) in allowlist:
                seen_allowlisted.add((eval_name, image))
                report.add(
                    LintResult(
                        name=CHECK_NAME,
                        status="warn",
                        message=(
                            f"Service '{service_name}' uses allowlisted unpinned "
                            f"image '{image}'; {PIN_ADVICE}, then remove the "
                            "allowlist entry"
                        ),
                        file=str(compose_file),
                    )
                )
                continue
            failed = True
            report.add(
                LintResult(
                    name=CHECK_NAME,
                    status="fail",
                    message=(
                        f"Service '{service_name}' image '{image}' is untagged or "
                        f":latest, so registry pushes silently change the eval "
                        f"environment; {PIN_ADVICE}"
                    ),
                    file=str(compose_file),
                )
            )

    stale = {(name, image) for (name, image) in allowlist if name == eval_name} - seen_allowlisted
    for _, image in sorted(stale):
        report.add(
            LintResult(
                name=CHECK_NAME,
                status="warn",
                message=(
                    f"Allowlist entry for image '{image}' is no longer needed; "
                    f"remove it from {ALLOWLIST_LOCATION}"
                ),
            )
        )

    if not failed and not seen_allowlisted:
        report.add(
            LintResult(
                name=CHECK_NAME,
                status="pass",
                message=(
                    f"All {checked_images} registry image reference(s) in "
                    f"{len(compose_files)} compose file(s) are pinned"
                ),
            )
        )
