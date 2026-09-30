"""Sandbox rules for Compose images, privileges, and GPU maintenance checks."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, cast

import yaml

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome, Severity
from inspect_evals_lint.registry import Reference, inspect_docs, rule

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
    references=(
        inspect_docs("sandboxing", "Sandboxing: Task Configuration", "task-configuration"),
    ),
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


_HOST_NAMESPACES = ("network_mode", "pid", "ipc", "userns_mode", "uts", "cgroup")
_UNCONFINED_OPTIONS = {
    "seccomp": "unconfined",
    "apparmor": "unconfined",
    "label": "disable",
    "systempaths": "unconfined",
}


def _mapping(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _sequence(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _enabled(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _host_path(source: str) -> bool:
    return (
        source in (".", "..")
        or source.startswith(("/", "./", "../", ".\\", "..\\", "~", "\\"))
        or bool(re.match(r"^[A-Za-z]:[\\/]", source))
    )


def _bind_source(volume: Any, named_volumes: dict[str, Any]) -> str | None:
    """Return the host source of a bind mount, including local-driver named binds."""
    if isinstance(volume, str):
        # Keep the drive letter attached to a Windows source path.
        match = re.match(r"^([A-Za-z]:[\\/][^:]*|[^:]+):(.+)$", volume)
        if not match:
            return None  # An anonymous volume has only a container target.
        source, target = match.groups()
        if not _host_path(target) and not target.startswith("$"):
            return None  # Anonymous volume with an access mode.
        if _host_path(source):
            return source
    else:
        mount = _mapping(volume)
        if mount.get("type") in ("bind", "npipe"):
            return str(mount.get("source", "<unspecified>"))
        if mount.get("type") != "volume":
            return None
        source = mount.get("source")
    if not isinstance(source, str):
        return None
    definition = _mapping(named_volumes.get(source))
    if definition.get("driver", "local") != "local":
        return None
    options = _mapping(definition.get("driver_opts"))
    modes = {mode.strip() for mode in str(options.get("o", "")).split(",")}
    if modes & {"bind", "rbind"}:
        return str(options.get("device", "<unspecified>"))
    return None


def _service_privileges(
    service: dict[str, Any], named_volumes: dict[str, Any]
) -> Iterable[tuple[str, str, Severity]]:
    for field in ("privileged", "use_api_socket", *_HOST_NAMESPACES):
        value = service.get(field)
        if field in ("privileged", "use_api_socket") and _enabled(value):
            detail = (
                "runs with elevated container privileges"
                if field == "privileged"
                else "exposes the container engine API socket and credentials"
            )
            yield field, detail, "error"
        elif field in _HOST_NAMESPACES and value == "host":
            yield field, "uses a host namespace", "error"
        elif (
            field in ("network_mode", "pid", "ipc")
            and isinstance(value, str)
            and value.startswith("container:")
        ):
            yield field, f"joins an external container namespace ({value})", "error"
        elif isinstance(value, str) and "$" in value:
            yield field, "contains an interpolation whose privileges cannot be checked", "warning"

    for field, detail in (
        ("cap_add", "adds Linux capabilities"),
        ("devices", "grants device access"),
        ("device_cgroup_rules", "adds device access rules"),
    ):
        if service.get(field):
            yield field, detail, "error"

    for option in _sequence(service.get("security_opt")):
        if not isinstance(option, str):
            continue
        parts = re.split(r"[:=]", option, maxsplit=1)
        if len(parts) == 2 and _UNCONFINED_OPTIONS.get(parts[0]) == parts[1]:
            yield "security_opt", f"disables a security restriction ({option})", "error"
        elif "$" in option:
            yield (
                "security_opt",
                "contains an interpolation whose restrictions cannot be checked",
                "warning",
            )

    for hook in ("pre_start", "post_start", "pre_stop"):
        for command in _sequence(service.get(hook)):
            value = _mapping(command).get("privileged")
            if _enabled(value):
                yield (
                    f"{hook}.privileged",
                    "runs a lifecycle command with elevated privileges",
                    "error",
                )
            elif isinstance(value, str) and "$" in value:
                yield (
                    f"{hook}.privileged",
                    "contains an interpolation whose privileges cannot be checked",
                    "warning",
                )

    for volume in _sequence(service.get("volumes")):
        source = _bind_source(volume, named_volumes)
        if source is not None:
            detail = f"bind-mounts host path '{source}'"
            if "docker.sock" in source or "docker_engine" in source:
                detail += ", exposing the Docker daemon socket"
            yield "volumes", detail, "error"
        elif (isinstance(volume, str) and "$" in volume) or (
            any("$" in str(_mapping(volume).get(field, "")) for field in ("type", "source"))
        ):
            yield (
                "volumes",
                "contains an interpolation whose mount source cannot be checked",
                "warning",
            )

    for source in _sequence(service.get("volumes_from")):
        if isinstance(source, str) and source.startswith("container:"):
            yield "volumes_from", f"imports mounts from an external container ({source})", "error"
        elif isinstance(source, str) and "$" in source:
            yield (
                "volumes_from",
                "contains an interpolation whose container cannot be checked",
                "warning",
            )


@rule(
    code="IESC001",
    name="sandbox_privileges",
    category="security",
    scopes=("eval", "helper"),
    allowlist=True,
    summary="Compose services do not grant additional sandbox privileges or host access",
    references=(inspect_docs("sandboxing", "Sandboxing"),),
)
def sandbox_privileges(ctx: LintContext) -> Iterable[Finding]:
    """Compose services do not grant additional sandbox privileges or host access.

    ## What it does

    Reads parsed YAML from every ``compose*.y*ml`` and ``docker-compose*.y*ml``
    under the package, including nested files and overrides. Reports one error
    per service and field for:

    - ``privileged: true`` and ``use_api_socket: true``.
    - Nonempty ``cap_add``, ``devices``, and ``device_cgroup_rules``.
    - ``security_opt`` disabling seccomp, AppArmor, SELinux labels, or protected
      system paths. Both ``option:value`` and ``option=value`` are checked.
    - ``host`` in ``network_mode``, ``pid``, ``ipc``, ``userns_mode``, ``uts``, or
      ``cgroup``, and ``container:`` namespaces in ``network_mode``, ``pid``, or ``ipc``.
    - Host bind mounts in either volume syntax, Windows named pipe mounts, and
      named volumes using the local driver's ``bind`` or ``rbind`` options.
      Docker daemon socket sources are identified in the message. Read-only
      host mounts are also reported because they still expose host data.
    - ``volumes_from`` referring to external containers.
    - Privileged ``pre_start``, ``post_start``, and ``pre_stop`` commands.

    GPU reservations under ``deploy.resources.reservations.devices``, ordinary
    named or anonymous volumes, and namespaces shared by ``service:`` reference
    are accepted. YAML anchors are resolved; comments are ignored.

    ## Why is this bad?

    These settings can give model-controlled processes access to host data,
    devices, namespaces, or the container engine, or remove runtime restrictions.
    Some evaluations need them. Each exception needs review and an allowlist
    entry; a finding does not establish that a setting is exploitable.

    ## Example

    ```yaml
    services:
      default:
        image: example/sandbox:1.0
        privileged: true
        volumes:
          - /var/run/docker.sock:/var/run/docker.sock
    ```

    Remove the settings when they are unnecessary. If required, document the
    reason next to ``default:privileged`` and ``default:volumes`` entries in
    ``allowlists.sandbox_privileges``.

    ## Scope and related checks

    This is a static check of declared Compose settings. It does not run Docker,
    read environment files, inspect images, combine overrides, follow ``include``
    or ``extends`` files, or inspect Kubernetes settings. Interpolated privilege,
    namespace, security-option, and mount values that cannot be checked produce
    warnings. A pass means no listed settings were found in the files checked.

    Dockerfile ``USER`` and Compose ``user`` belong in a separate runtime-user
    rule: root within a container does not itself grant host access. That rule
    would need to account for the selected build stage, inherited image user,
    Compose overrides, and Inspect's per-command user selection. Flagging every
    ``USER root`` would also flag temporary root use during image builds.

    Build privileges (``build.privileged``, ``build.entitlements``, and Dockerfile
    ``RUN --security=insecure`` or ``RUN --network=host``) belong in a separate
    build-isolation rule. Published ports, external networks, and host-gateway
    mappings need a network-exposure policy. Missing hardening settings such as
    ``read_only`` or ``no-new-privileges`` are outside this rule's checks.

    ## Options

    - ``allowlists.sandbox_privileges``: ``{ package = ["service:field"] }``
      entries report warnings while present. The key applies to that service
      and field across the package's Compose files. Nested fields use keys such
      as ``default:post_start.privileged``. Removed settings leave stale entries
      for the runner to report.
    """
    compose_files = sorted(
        set(ctx.path.rglob("compose*.y*ml")) | set(ctx.path.rglob("docker-compose*.y*ml"))
    )
    if not compose_files:
        yield Outcome("skip", "No compose files found")
        return

    issues = 0
    checked_services = 0
    for compose_file in compose_files:
        try:
            compose: Any = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            issues += 1
            yield Diagnostic(
                f"Could not parse compose file: {e}", file=compose_file, severity="warning"
            )
            continue
        services = _mapping(compose).get("services", {})
        if not isinstance(compose, dict) or not isinstance(services, dict):
            issues += 1
            yield Diagnostic(
                "Could not check Compose privileges: expected a mapping of services",
                file=compose_file,
                severity="warning",
            )
            continue
        for service_name, service in _mapping(services).items():
            if not isinstance(service, dict):
                issues += 1
                yield Diagnostic(
                    f"Could not check service '{service_name}': expected a mapping",
                    file=compose_file,
                    severity="warning",
                )
                continue
            checked_services += 1
            findings: dict[str, list[tuple[str, Severity]]] = {}
            for field, detail, severity in _service_privileges(
                _mapping(service), _mapping(_mapping(compose).get("volumes"))
            ):
                findings.setdefault(field, []).append((detail, severity))
            for field, details in findings.items():
                issues += 1
                severity = "error" if any(s == "error" for _, s in details) else "warning"
                yield Diagnostic(
                    f"Service '{service_name}' {field}: "
                    + "; ".join(dict.fromkeys(detail for detail, _ in details)),
                    file=compose_file,
                    severity=severity,
                    key=f"{service_name}:{field}",
                    hint="remove the setting, or review and document the required access in "
                    "allowlists.sandbox_privileges",
                )
    if not issues:
        yield Outcome(
            "pass",
            f"No additional privileges or host access found in {checked_services} "
            f"service(s) across {len(compose_files)} compose file(s)",
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
    references=(
        inspect_docs("sandboxing", "Sandboxing: Container Resources", "container-resources"),
        Reference(
            "Modal sandbox: Docker Compose (GPU reservations)",
            "https://meridianlabs-ai.github.io/inspect_sandboxes/modal.html#docker-compose",
        ),
        Reference(
            "Daytona sandbox: Docker Compose (GPU reservations)",
            "https://meridianlabs-ai.github.io/inspect_sandboxes/daytona.html#docker-compose",
        ),
        Reference(
            "Kubernetes sandbox: Targeting kubeconfig contexts (GPU nodes)",
            "https://k8s-sandbox.aisi.org.uk/tips/configuration/#targeting-specific-or-multiple-kubeconfig-contexts",
        ),
    ),
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
