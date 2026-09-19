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
    """Registry images in compose files use an immutable tag or digest.

    ## What it does
    Reads every ``compose*.y*ml`` under the package and flags each service whose
    ``image`` is untagged or ``:latest``. Services built locally (``build:``) and
    ``${VAR}`` interpolated references are skipped. Each diagnostic is keyed by the
    image reference, which is what an allowlist entry names.

    ## Why is this bad?
    A floating reference resolves to whatever the registry holds today. A push
    upstream silently changes the evaluation environment, and results stop being
    comparable across runs without anything in the repository changing.

    ## Example
    ```yaml
    services:
      default:
        image: aisiuk/inspect-tool-support
    ```
    Use instead:
    ```yaml
    services:
      default:
        image: aisiuk/inspect-tool-support:1.4.2
        # or: aisiuk/inspect-tool-support@sha256:...
    ```

    ## Options
    - `allowlists.sandbox_image_pinning`: `{ package = ["image/ref"] }` entries reported as warnings while they are pinned.
    """
    compose_files = sorted(ctx.path.rglob("compose*.y*ml"))
    if not compose_files:
        yield Outcome("skip", "No compose files found")
        return

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
            yield Diagnostic(
                f"Service '{service_name}' image '{image}' is untagged or :latest, "
                "so registry pushes silently change the eval environment",
                file=compose_file,
                hint=PIN_HINT,
                key=image,
            )

    if not issues:
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
    """An evaluation requiring a GPU ships a maintenance sandbox check task.

    ## What it does
    When ``eval.yaml`` declares ``metadata.requires.gpu``, ``tasks`` must include a
    task whose name ends ``_sandbox_check`` and which is declared with
    ``kind: maintenance``.

    ## Why is this bad?
    GPU sandbox images cannot be exercised in ordinary CI, so a broken image (a
    missing package, the wrong Python, a CUDA toolchain that does not work) would
    only show up as errored samples in a real run. The check task certifies the
    image on GPU hardware through the evaluation's own scorer, and ``kind:
    maintenance`` keeps its accuracy out of listings that present model results.

    ## Example
    ```yaml
    tasks:
      - name: kernelbench
      - name: kernelbench_sandbox_check
        kind: maintenance
    metadata:
      requires:
        gpu: true
    ```
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
