"""Sandbox rules: pinned compose images, and a sandbox check task for GPU evaluations."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

import yaml

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import rule

PIN_HINT = (
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


@rule(
    code="IEBP005",
    name="sandbox_image_pinning",
    category="best_practices",
    scopes=("eval", "helper"),
    allowlist=True,
    summary="Registry images in compose files use an immutable tag or digest",
)
def sandbox_image_pinning(ctx: LintContext) -> Iterable[Finding]:
    """Fail on untagged or ``:latest`` images in ``compose*.y*ml`` files.

    A floating reference resolves to whatever the registry currently holds, so a
    registry push silently changes the evaluation environment. Services built
    locally (``build:``) and ``${VAR}`` interpolated references are skipped.
    Allowlist entries ``(package, image)`` warn instead of failing, and a stale
    entry warns so it gets removed. One diagnostic per service.
    """
    allowlist = ctx.config.sandbox_image_allowlist
    compose_files = sorted(ctx.path.rglob("compose*.y*ml"))
    if not compose_files:
        yield Outcome("skip", "No compose files found")
        return

    seen_allowlisted: set[tuple[str, str]] = set()
    issues = 0
    checked_images = 0
    for compose_file in compose_files:
        try:
            compose: Any = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            issues += 1
            yield Diagnostic(
                f"Could not parse compose file: {e}", file=compose_file, severity="warning"
            )
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
            issues += 1
            if (ctx.name, image) in allowlist:
                seen_allowlisted.add((ctx.name, image))
                yield Diagnostic(
                    f"Service '{service_name}' uses allowlisted unpinned image '{image}'",
                    file=compose_file,
                    severity="warning",
                    hint=f"{PIN_HINT}, then remove the allowlist entry",
                )
            else:
                yield Diagnostic(
                    f"Service '{service_name}' image '{image}' is untagged or :latest, "
                    "so registry pushes silently change the eval environment",
                    file=compose_file,
                    hint=PIN_HINT,
                )

    stale = {(name, image) for (name, image) in allowlist if name == ctx.name} - seen_allowlisted
    for _, image in sorted(stale):
        yield Diagnostic(
            f"Allowlist entry for image '{image}' is no longer needed",
            file=ctx.root / "pyproject.toml",
            severity="warning",
            hint=f"remove it from {ALLOWLIST_LOCATION}",
        )

    if not issues and not stale:
        yield Outcome(
            "pass",
            f"All {checked_images} registry image reference(s) in "
            f"{len(compose_files)} compose file(s) are pinned",
        )


GPU_CHECK_HINT = (
    "declare a task named '<eval>_sandbox_check' with 'kind: maintenance' in "
    "eval.yaml that runs the eval's scorer over fixture answers with known "
    "verdicts inside the pinned image (see inspect_evals.utils.sandbox_check "
    "and kernelbench_sandbox_check for the pattern)"
)


def _requires_gpu(data: dict[str, Any]) -> bool:
    metadata: Any = data.get("metadata")
    if not isinstance(metadata, dict):
        return False
    requires: Any = cast(dict[str, Any], metadata).get("requires")
    if not isinstance(requires, dict):
        return False
    gpu: Any = cast(dict[str, Any], requires).get("gpu")
    return gpu is True or isinstance(gpu, dict)


@rule(
    code="IEBP006",
    name="gpu_sandbox_check",
    category="best_practices",
    summary="An evaluation requiring a GPU ships a maintenance sandbox check task",
)
def gpu_sandbox_check(ctx: LintContext) -> Iterable[Finding]:
    """An eval that declares ``metadata.requires.gpu`` ships a sandbox check task.

    GPU sandbox images cannot be exercised in ordinary CI, so a broken image
    (missing package, wrong Python, CUDA toolchain not working) would only show
    up as errored samples in a real run. The check task certifies the image on
    GPU hardware through the eval's own scorer. It must appear in ``tasks`` with
    a name ending ``_sandbox_check`` and ``kind: maintenance``, so listings and
    reports do not present its accuracy as a model result.
    """
    eval_yaml_file = ctx.path / "eval.yaml"
    if not eval_yaml_file.exists():
        yield Outcome("skip", "No eval.yaml to read a GPU requirement from")
        return
    try:
        data: Any = yaml.safe_load(eval_yaml_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        yield Diagnostic(f"Could not parse eval.yaml: {e}", file=eval_yaml_file, severity="warning")
        return
    if not isinstance(data, dict) or not _requires_gpu(cast(dict[str, Any], data)):
        yield Outcome("skip", "No GPU requirement declared under metadata.requires")
        return

    tasks: Any = cast(dict[str, Any], data).get("tasks")
    raw_tasks: list[Any] = cast(list[Any], tasks) if isinstance(tasks, list) else []
    task_entries: list[dict[str, Any]] = [
        cast(dict[str, Any], t) for t in raw_tasks if isinstance(t, dict)
    ]
    check_tasks = [t for t in task_entries if str(t.get("name", "")).endswith("_sandbox_check")]
    maintenance = [t for t in check_tasks if t.get("kind") == "maintenance"]
    if maintenance:
        names = ", ".join(str(t["name"]) for t in maintenance)
        yield Outcome("pass", f"GPU eval ships sandbox check task(s): {names}")
        return
    if check_tasks:
        names = ", ".join(str(t["name"]) for t in check_tasks)
        yield Diagnostic(
            f"Sandbox check task(s) {names} must be declared with 'kind: maintenance'",
            file=eval_yaml_file,
            hint="so their accuracy is not presented as a model result",
        )
    else:
        yield Diagnostic(
            "eval.yaml declares metadata.requires.gpu but no sandbox check task",
            file=eval_yaml_file,
            hint=GPU_CHECK_HINT,
        )
