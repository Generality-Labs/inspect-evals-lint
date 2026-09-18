"""Sandbox checks: registry-pulled compose images must be pinned."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

import yaml

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.models import LintResult
from inspect_evals_lint.registry import rule

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


@rule(
    code="IEBP005",
    name="sandbox_image_pinning",
    category="best_practices",
    scopes=("eval", "helper"),
    allowlist=True,
    summary="Registry images in compose files use an immutable tag or digest",
)
def sandbox_image_pinning(ctx: LintContext) -> Iterable[LintResult]:
    """Fail on untagged or ``:latest`` images in ``compose*.y*ml`` files under ``eval_path``.

    A floating reference resolves to whatever the registry currently holds, so a
    registry push silently changes the evaluation environment. Services built
    locally (``build:``) and env-var interpolated references are skipped.
    ``allowlist`` entries ``(eval_name, image)`` warn instead of failing, and a
    stale entry warns so it gets removed.
    """
    eval_path, allowlist = ctx.path, ctx.config.sandbox_image_allowlist
    eval_name = eval_path.name
    compose_files = sorted(eval_path.rglob("compose*.y*ml"))
    if not compose_files:
        yield LintResult(name=CHECK_NAME, status="skip", message="No compose files found")
        return

    seen_allowlisted: set[tuple[str, str]] = set()
    failed = False
    checked_images = 0
    for compose_file in compose_files:
        try:
            compose: Any = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            yield LintResult(
                name=CHECK_NAME,
                status="warn",
                message=f"Could not parse compose file: {e}",
                file=str(compose_file),
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
                yield LintResult(
                    name=CHECK_NAME,
                    status="warn",
                    message=(
                        f"Service '{service_name}' uses allowlisted unpinned "
                        f"image '{image}'; {PIN_ADVICE}, then remove the "
                        "allowlist entry"
                    ),
                    file=str(compose_file),
                )

                continue
            failed = True
            yield LintResult(
                name=CHECK_NAME,
                status="fail",
                message=(
                    f"Service '{service_name}' image '{image}' is untagged or "
                    f":latest, so registry pushes silently change the eval "
                    f"environment; {PIN_ADVICE}"
                ),
                file=str(compose_file),
            )

    stale = {(name, image) for (name, image) in allowlist if name == eval_name} - seen_allowlisted
    for _, image in sorted(stale):
        yield LintResult(
            name=CHECK_NAME,
            status="warn",
            message=(
                f"Allowlist entry for image '{image}' is no longer needed; "
                f"remove it from {ALLOWLIST_LOCATION}"
            ),
        )

    if not failed and not seen_allowlisted:
        yield LintResult(
            name=CHECK_NAME,
            status="pass",
            message=(
                f"All {checked_images} registry image reference(s) in "
                f"{len(compose_files)} compose file(s) are pinned"
            ),
        )


GPU_CHECK_NAME = "gpu_sandbox_check"

GPU_CHECK_ADVICE = (
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
def gpu_sandbox_check(ctx: LintContext) -> Iterable[LintResult]:
    """An eval that declares ``metadata.requires.gpu`` ships a sandbox check task.

    GPU sandbox images cannot be exercised in ordinary CI, so a broken image
    (missing package, wrong Python, CUDA toolchain not working) would only show
    up as errored samples in a real run. The check task certifies the image on
    GPU hardware through the eval's own scorer. It must appear in ``tasks`` with
    a name ending ``_sandbox_check`` and ``kind: maintenance``, so listings and
    reports do not present its accuracy as a model result.
    """
    eval_path = ctx.path
    eval_yaml_file = eval_path / "eval.yaml"
    if not eval_yaml_file.exists():
        yield LintResult(
            name=GPU_CHECK_NAME,
            status="skip",
            message="No eval.yaml to read a GPU requirement from",
        )

        return
    try:
        data: Any = yaml.safe_load(eval_yaml_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        yield LintResult(
            name=GPU_CHECK_NAME,
            status="warn",
            message=f"Could not parse eval.yaml: {e}",
            file=str(eval_yaml_file),
        )

        return
    if not isinstance(data, dict) or not _requires_gpu(cast(dict[str, Any], data)):
        yield LintResult(
            name=GPU_CHECK_NAME,
            status="skip",
            message="No GPU requirement declared under metadata.requires",
        )

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
        yield LintResult(
            name=GPU_CHECK_NAME,
            status="pass",
            message=f"GPU eval ships sandbox check task(s): {names}",
        )

        return
    if check_tasks:
        names = ", ".join(str(t["name"]) for t in check_tasks)
        message = (
            f"Sandbox check task(s) {names} must be declared with 'kind: maintenance' "
            "so their accuracy is not presented as a model result"
        )
    else:
        message = f"eval.yaml declares metadata.requires.gpu but no sandbox check task; {GPU_CHECK_ADVICE}"
    yield LintResult(name=GPU_CHECK_NAME, status="fail", message=message, file=str(eval_yaml_file))
