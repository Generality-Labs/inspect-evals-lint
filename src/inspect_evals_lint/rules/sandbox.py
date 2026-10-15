"""Sandbox rules for Compose images, privileges, and GPU maintenance checks."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

import yaml

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome, Severity
from inspect_evals_lint.registry import Reference, inspect_docs, rule
from inspect_evals_lint.rules._compose import iter_compose_files, line_of, load_compose

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
    Reads every ``compose*.y*ml`` and ``docker-compose*.y*ml`` under the package,
    ``exclude``d directories included, and flags each service whose ``image`` is
    untagged or ``:latest``. Services built locally (``build:``) and
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
    compose_files = iter_compose_files(ctx)
    if not compose_files:
        yield Outcome("skip", "No compose files found")
        return

    issues = 0
    checked_images = 0
    for compose_file in compose_files:
        compose, problem = load_compose(compose_file)
        if problem is not None:
            issues += 1
            yield problem
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
                line=line_of(service, "image"),
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
    "seccomp": {"unconfined"},
    "apparmor": {"unconfined"},
    "label": {"disable", "type:spc_t", "type:unconfined_t"},
    "systempaths": {"unconfined"},
}
_ENGINE_SOCKETS = (
    "docker.sock",
    "docker_engine",
    "podman.sock",
    "containerd.sock",
    "crio.sock",
    "cri-dockerd.sock",
    "buildkitd.sock",
)
_ENGINE_SOCKET_DIRS = {"/", "/run", "/var", "/var/run"}
"""Directories holding ``/var/run/docker.sock``, the default Docker socket."""


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


_INTERPOLATION = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_]\w*")


def _short_syntax(volume: str) -> tuple[str, str | None, list[str]]:
    """Classify a short-syntax mount as bind, volume, anonymous or unknown, with its source and options.

    Options that contain an interpolation keep a NUL character in its place.
    """
    # Mask interpolations so a ':' or '/' inside ${...} does not split or classify the mount.
    tokens = iter(_INTERPOLATION.findall(volume))
    masked = _INTERPOLATION.sub("\0", volume)
    # Keep the drive letter attached to a Windows source path.
    match = re.match(r"^([A-Za-z]:[\\/][^:]*|[^:]+):(.+)$", masked)
    if not match:
        return "anonymous", None, []  # Only a container target.
    masked_source, masked_target = match.groups()
    if not _host_path(masked_target) and not masked_target.startswith("\0"):
        return "anonymous", None, []  # A container target with an access mode.
    mode = re.match(r"^(?:[A-Za-z]:[\\/][^:]*|[^:]*)(?::(.*))?$", masked_target)
    options = mode.group(1).split(",") if mode and mode.group(1) else []
    source = re.sub("\0", lambda _: next(tokens), masked_source)
    # A volume name cannot contain a path separator, so ${HOME}/.ssh is a host path.
    if _host_path(masked_source) or re.search(r"[\\/]", masked_source):
        return "bind", source, options
    if "\0" in masked_source:
        return "unknown", source, options  # ${VOLUME} may name a volume or a path.
    return "volume", source, options


def _bind_source(volume: Any, named_volumes: dict[str, Any]) -> str | None:
    """Return the host source of a bind mount, including local-driver named binds."""
    if isinstance(volume, str):
        kind, source, _ = _short_syntax(volume)
        if kind == "bind":
            return source
        if kind != "volume":
            return None  # The caller warns about an interpolated source.
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


_SANDBOX_SETTINGS = "x-inspect_docker_sandbox"
_ALLOW_WRITES = "unsafe_allow_writable_host_mounts"
_ALLOW_WRITES_KEY = f"{_SANDBOX_SETTINGS}.{_ALLOW_WRITES}"
_PROPAGATIONS = {"shared", "rshared", "slave", "rslave", "private", "rprivate"}
_INTERPOLATED_MODE = "its access mode contains an interpolation that cannot be checked"
_WRITABLE_HINT = (
    "add ':ro', copy the files in with Sample.files, or use a Docker-managed volume; "
    f"if the evaluation must write to the host, set {_ALLOW_WRITES_KEY}: true and allowlist that"
)
_UNVERIFIABLE_HINT = (
    "remove the mount or the setting that prevents its verification; if the evaluation "
    f"needs it, set {_ALLOW_WRITES_KEY}: true and allowlist that"
)


def _mount_rejection(volume: Any, named_volumes: dict[str, Any], can_remount: bool) -> str | None:
    """Why inspect_ai's Docker sandbox refuses a mount by default, or None if it accepts it.

    Mirrors ``inspect_ai.util._sandbox.docker.mounts``, which reads the same
    settings after ``docker compose config`` normalises them.
    """
    if isinstance(volume, str):
        kind, source, options = _short_syntax(volume)
        if kind == "bind" and any("\0" in option for option in options):
            return _INTERPOLATED_MODE
        read_only = "ro" in options
        propagation = next((o for o in options if o in _PROPAGATIONS), "rprivate")
        recursive = None
    else:
        mount = _mapping(volume)
        kind, source = mount.get("type"), mount.get("source")
        if "$" in str(kind) or "$" in str(source):
            return None  # The caller warns that the mount source cannot be checked.
        bind = _mapping(mount.get("bind"))
        propagation, recursive = bind.get("propagation", "rprivate"), bind.get("recursive")
        if any("$" in str(v) for v in (mount.get("read_only"), propagation, recursive)):
            return _INTERPOLATED_MODE
        read_only = _enabled(mount.get("read_only"))
    if kind == "bind":
        if not read_only:
            return "writable host bind"
        if can_remount:
            return "service privileges permit remounting host binds writable"
        if propagation not in ("private", "rprivate"):
            return "bind propagation may expose writable submounts"
        if recursive == "writable":
            return "writable submounts"
    elif kind == "volume":
        definition = _mapping(named_volumes.get(source)) if isinstance(source, str) else {}
        if (
            definition.get("external") not in (None, False)
            or definition.get("driver", "local") != "local"
            or definition.get("driver_opts")
            or "name" in definition
        ):
            return "volume host access cannot be verified"
    elif isinstance(kind, str) and kind not in ("anonymous", "unknown", "tmpfs", "image", "npipe"):
        return f"mount type '{kind}' cannot be verified"
    return None


@dataclass(frozen=True)
class _Privilege:
    """One setting that grants access, with the value an allowlist entry names."""

    field: str
    value: str | None
    detail: str
    line: int | None
    severity: Severity = "error"
    rejection_hint: str | None = None
    """Set for findings from inspect_ai's mount checks, which no allowlist entry can accept."""

    def key(self, service_name: str) -> str:
        key = f"{service_name}:{self.field}"
        return key if self.value is None else f"{key}:{self.value}"


def _values(container: dict[str, Any], field: str) -> list[tuple[str, int | None]]:
    """A list setting's entries with their lines, or the setting itself when it is one value."""
    value = container.get(field)
    if isinstance(value, list):
        return [(str(item), line_of(value, i)) for i, item in enumerate(_sequence(value))]
    return [(str(value), line_of(container, field))] if value else []


def _host_sourced(
    service: dict[str, Any], compose: dict[str, Any], field: str
) -> Iterable[_Privilege]:
    """Secrets or configs the service uses whose definition reads a host file or variable."""
    definitions = _mapping(compose.get(field))
    references = service.get(field)
    for index, reference in enumerate(_sequence(references)):
        name = reference if isinstance(reference, str) else _mapping(reference).get("source")
        if not isinstance(name, str):
            continue
        definition = _mapping(definitions.get(name))
        line = line_of(references, index)
        path, variable = definition.get("file"), definition.get("environment")
        if isinstance(path, str):
            yield _Privilege(field, path, f"exposes host file '{path}' as '{name}'", line)
        elif isinstance(variable, str):
            yield _Privilege(
                field,
                f"env:{variable}",
                f"exposes host environment variable '{variable}' as '{name}'",
                line,
            )


def _rejected(field: str, label: str, reason: str, line: int | None) -> _Privilege:
    """A mount setting that inspect_ai's Docker sandbox refuses, or warns it cannot check."""
    if reason == _INTERPOLATED_MODE:
        detail = f"mount of '{label}': {reason}"
        return _Privilege(field, label, detail, line, "warning", rejection_hint=_WRITABLE_HINT)
    hint = _WRITABLE_HINT if reason == "writable host bind" else _UNVERIFIABLE_HINT
    detail = f"inspect_ai's Docker sandbox refuses '{label}': {reason}"
    return _Privilege(field, label, detail, line, rejection_hint=hint)


def _service_privileges(
    service: dict[str, Any], compose: dict[str, Any], enforce_mounts: bool
) -> Iterable[_Privilege]:
    named_volumes = _mapping(compose.get("volumes"))
    can_remount = _enabled(service.get("privileged")) or any(
        cap.upper().removeprefix("CAP_") in ("SYS_ADMIN", "ALL")
        for cap, _ in _values(service, "cap_add")
    )
    for field in ("privileged", "use_api_socket", *_HOST_NAMESPACES):
        value = service.get(field)
        line = line_of(service, field)
        if field in ("privileged", "use_api_socket") and _enabled(value):
            detail = (
                "runs with elevated container privileges"
                if field == "privileged"
                else "exposes the container engine API socket and credentials"
            )
            yield _Privilege(field, None, detail, line)
        elif field in _HOST_NAMESPACES and value == "host":
            yield _Privilege(field, value, "uses a host namespace", line)
        elif (
            field in ("network_mode", "pid", "ipc")
            and isinstance(value, str)
            and value.startswith("container:")
        ):
            yield _Privilege(field, value, f"joins an external container namespace ({value})", line)
        elif isinstance(value, str) and "$" in value:
            yield _Privilege(
                field,
                value,
                "contains an interpolation whose privileges cannot be checked",
                line,
                "warning",
            )

    for field, detail in (
        ("cap_add", "adds Linux capability"),
        ("devices", "grants device access to"),
        ("device_cgroup_rules", "adds device access rule"),
    ):
        for value, line in _values(service, field):
            if "$" in value:
                yield _Privilege(
                    field,
                    value,
                    "contains an interpolation whose access cannot be checked",
                    line,
                    "warning",
                )
            else:
                yield _Privilege(field, value, f"{detail} '{value}'", line)

    for option, line in _values(service, "security_opt"):
        parts = re.split(r"[:=]", option, maxsplit=1)
        if len(parts) == 2 and parts[1] in _UNCONFINED_OPTIONS.get(parts[0], set()):
            yield _Privilege(
                "security_opt", option, f"disables a security restriction ({option})", line
            )
        elif "$" in option:
            yield _Privilege(
                "security_opt",
                option,
                "contains an interpolation whose restrictions cannot be checked",
                line,
                "warning",
            )

    for hook in ("pre_start", "post_start", "pre_stop"):
        for command in _sequence(service.get(hook)):
            value = _mapping(command).get("privileged")
            line = line_of(command, "privileged")
            if _enabled(value):
                yield _Privilege(
                    f"{hook}.privileged",
                    None,
                    "runs a lifecycle command with elevated privileges",
                    line,
                )
            elif isinstance(value, str) and "$" in value:
                yield _Privilege(
                    f"{hook}.privileged",
                    None,
                    "contains an interpolation whose privileges cannot be checked",
                    line,
                    "warning",
                )

    volumes = service.get("volumes")
    for index, volume in enumerate(_sequence(volumes)):
        line = line_of(volumes, index)
        source = _bind_source(volume, named_volumes)
        if source is not None:
            detail = f"bind-mounts host path '{source}'"
            # The target counts too: clients look for the socket at /var/run/docker.sock.
            if (source.rstrip("/") or "/") in _ENGINE_SOCKET_DIRS or any(
                name in source or name in str(volume) for name in _ENGINE_SOCKETS
            ):
                detail += ", exposing a container engine API socket"
            yield _Privilege("volumes", source, detail, line)
        elif isinstance(volume, str) and "$" in volume:
            yield _Privilege(
                "volumes",
                volume,
                "contains an interpolation whose mount source cannot be checked",
                line,
                "warning",
            )
        elif any("$" in str(_mapping(volume).get(field, "")) for field in ("type", "source")):
            yield _Privilege(
                "volumes",
                str(_mapping(volume).get("source")),
                "contains an interpolation whose mount source cannot be checked",
                line,
                "warning",
            )
        if enforce_mounts and (reason := _mount_rejection(volume, named_volumes, can_remount)):
            mount = _mapping(volume)
            label = (
                _short_syntax(volume)[1]
                if isinstance(volume, str)
                else mount.get("source", mount.get("target"))
            )
            yield _rejected("volumes", str(label), reason, line)

    yield from _host_sourced(service, compose, "secrets")
    yield from _host_sourced(service, compose, "configs")
    if enforce_mounts and can_remount:
        for field in ("secrets", "configs"):
            if service.get(field):
                reason = "service privileges permit remounting host binds writable"
                yield _rejected(field, field, reason, line_of(service, field))

    for source, line in _values(service, "volumes_from"):
        if source.startswith("container:"):
            yield _Privilege(
                "volumes_from",
                source,
                f"imports mounts from an external container ({source})",
                line,
            )
        elif "$" in source:
            yield _Privilege(
                "volumes_from",
                source,
                "contains an interpolation whose container cannot be checked",
                line,
                "warning",
            )
        if enforce_mounts:
            yield _rejected("volumes_from", source, "volumes_from cannot be verified", line)


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
    under the package, including nested files and overrides. ``exclude`` does not
    apply: Compose files configure the sandbox from the host even when they sit
    beside challenge code that is excluded. Reports one error per service,
    field and value for the settings described below. It also reports the
    mounts that inspect_ai's Docker sandbox refuses, which no allowlist entry
    can accept.

    GPU reservations under ``deploy.resources.reservations.devices``, ordinary
    named or anonymous volumes, and namespaces shared by ``service:`` reference
    are accepted. YAML anchors are resolved; commented-out settings are ignored.
    A value tagged ``!override`` or ``!reset`` is checked as written.

    Each finding points at the line of the setting, or of the list item for list
    settings such as ``volumes`` and ``cap_add``. A setting merged from a YAML
    anchor points at the anchor, so a suppression comment there covers every
    service that merges it.

    ## Why is this bad?

    These settings can give model-controlled processes access to host data,
    devices, namespaces, or the container engine, or remove runtime restrictions.
    Some evaluations need them. Each exception needs review and an allowlist
    entry; a finding does not establish that a setting is exploitable.

    The effects depend on the host platform, daemon configuration, and remaining
    permissions. In particular, sharing a namespace does not automatically
    authorize every operation on the resources it exposes.

    ### Privileges and device access

    - ``privileged: true`` grants all Linux capabilities, exposes host devices,
      and relaxes security profiles. This can allow sandbox processes to alter
      host resources and take control of the host.
      [Docker runtime privileges](https://docs.docker.com/engine/containers/run/#runtime-privilege-and-linux-capabilities).
    - Any nonempty ``cap_add`` requests additional Linux capabilities. Each
      capability authorizes particular operations: for example, ``SYS_ADMIN``
      includes mounting filesystems, while ``SYS_PTRACE`` permits process
      inspection subject to other restrictions. The implications depend on the
      capabilities requested and the namespaces in which they apply.
      [Linux capabilities](https://man7.org/linux/man-pages/man7/capabilities.7.html).
    - Any nonempty ``devices`` exposes selected devices and their drivers to the
      container. For example, access to a disk device can expose data beyond the
      container's mounted directories. The effect depends on the device and its
      allowed operations.
      [Docker device access](https://docs.docker.com/reference/cli/docker/container/run/#device).
    - Any nonempty ``device_cgroup_rules`` adds device permissions by type and
      major/minor number. Rules can allow reading, writing, and creating device
      nodes; wildcards can cover whole classes of devices. These permissions
      can apply when a device node becomes available later.
      [Linux device access rules](https://docs.kernel.org/admin-guide/cgroup-v1/devices.html).

    ### Disabled security restrictions

    The following values in ``security_opt`` are reported with either ``:`` or
    ``=`` as the separator. Each disables a different restriction.

    - ``seccomp=unconfined`` removes the container's system-call filter. Code can
      attempt kernel operations that the default profile would reject, subject
      to remaining permission checks.
      [Docker seccomp profiles](https://docs.docker.com/engine/security/seccomp/).
    - ``apparmor=unconfined`` removes AppArmor profile enforcement where AppArmor
      is enabled. File access, mounts, and other operations lose the additional
      restrictions imposed by that profile.
      [Docker AppArmor profiles](https://docs.docker.com/engine/security/apparmor/).
    - ``label=disable`` disables SELinux labeling for the container. On an
      SELinux-enabled host, this removes label-based confinement that helps
      separate container processes and resources. ``label=type:spc_t`` (the
      super-privileged container type) and ``label=type:unconfined_t`` run the
      container in an SELinux domain without that confinement, with the same
      effect.
      [Docker security options](https://docs.docker.com/reference/cli/docker/container/run/#security-opt).
    - ``systempaths=unconfined`` removes the runtime's masking and read-only
      protection of system paths. Kernel information and controls at those
      paths become accessible subject to remaining permissions.
      [Docker system-path security option](https://docs.docker.com/reference/cli/docker/container/run/#security-opt).

    ### Shared namespaces

    | Setting | Implication |
    | --- | --- |
    | ``network_mode: host`` | Shares host networking. Processes can reach services on host loopback and listen on host ports without a port mapping. [Docker host networking](https://docs.docker.com/engine/network/drivers/host/). |
    | ``pid: host`` | Makes host processes visible. Signaling or inspecting them then depends on credentials, capabilities, and other controls. [Linux PID namespaces](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html). |
    | ``ipc: host`` | Shares host IPC resources, including System V shared memory, semaphores, and message queues. Processes may read data or interfere with applications when IPC permissions allow it. [Linux IPC namespaces](https://man7.org/linux/man-pages/man7/ipc_namespaces.7.html). |
    | ``userns_mode: host`` | Disables per-container user-namespace remapping where the Docker daemon enables it. Container user IDs lose that remapping's separation from host IDs. [Docker user-namespace remapping](https://docs.docker.com/engine/security/userns-remap/#disable-namespace-remapping-for-a-container). |
    | ``uts: host`` | Shares the host's hostname and NIS domain name. A process with the required capability can change these identifiers for other processes in that namespace. [Linux UTS namespaces](https://man7.org/linux/man-pages/man7/uts_namespaces.7.html). |
    | ``cgroup: host`` | Exposes the host view of cgroup paths and hierarchy. This reveals information outside the container's private view; it does not itself remove resource limits or grant write access. [Linux cgroup namespaces](https://man7.org/linux/man-pages/man7/cgroup_namespaces.7.html). |
    | ``network_mode: container:...`` | Joins another container's networking, including its loopback services and listening ports. [Docker container networking mode](https://docs.docker.com/engine/network/#container-networks). |
    | ``pid: container:...`` | Shares another container's process namespace, allowing process visibility and operations subject to permissions. [Docker PID settings](https://docs.docker.com/reference/cli/docker/container/run/#pid). |
    | ``ipc: container:...`` | Shares another container's IPC resources, allowing data access or interference subject to permissions. [Docker IPC settings](https://docs.docker.com/reference/cli/docker/container/run/#ipc). |

    ``container:`` references depend on containers outside the declared Compose
    service relationships. The rule cannot establish their configuration.
    References using ``service:`` remain within those declared relationships
    and are outside this check.

    ### Host files and engine access

    - Host bind mounts in either short or long ``volumes`` syntax expose a host
      path. Writable mounts can let sandbox code change host files, and
      inspect_ai refuses them (see below); read-only mounts still expose their
      contents, potentially including credentials or evaluation answers.
      Relative paths and Windows host paths are checked too, as is a
      short-syntax source with a path separator outside its interpolations,
      such as ``${HOME}/.ssh``: a volume name cannot contain one.
      [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/).
    - Named volumes whose local ``driver_opts.o`` contains ``bind`` or ``rbind``
      also expose the host path in ``driver_opts.device``. Review them as host
      bind mounts even though the service refers to a volume name.
      [Compose local-driver bind example](https://docs.docker.com/reference/compose-file/volumes/#driver_opts).
      The ``rbind`` option also includes existing mounts below the source path,
      potentially exposing additional filesystems.
      [Linux recursive bind mounts](https://man7.org/linux/man-pages/man8/mount.8.html).
    - Windows named pipe mounts (``type: npipe``) expose a host communication
      endpoint. The implications depend on the service listening on that pipe
      and the caller's permissions.
      [Compose mount types](https://docs.docker.com/reference/compose-file/services/#volumes).
    - Container engine sockets receive an additional callout when the source
      or target names a Docker, Podman, containerd, CRI-O or BuildKit socket
      (``docker.sock``, ``docker_engine``, ``podman.sock``, ``containerd.sock``,
      ``crio.sock``, ``cri-dockerd.sock``, ``buildkitd.sock``), or the source is
      a directory holding ``/var/run/docker.sock``, such as ``/var/run``. Access
      to an engine API can allow creation of containers with host mounts or
      elevated privileges.
      [Docker daemon access](https://docs.docker.com/engine/security/#docker-daemon-attack-surface).
      A read-only socket mount does not restrict which API operations a
      connected client can request. Review must therefore account for
      [Docker's API authorization policy](https://docs.docker.com/engine/extend/plugins_authorization/),
      which allows all operations by default.
    - ``secrets`` and ``configs`` whose top-level definition has a ``file`` or
      ``environment`` source copy a host file or environment variable into the
      container, at ``/run/secrets/<name>`` for a secret. Review them as you
      would a read-only bind mount of that file. ``content`` and ``external``
      sources are accepted. Keys name the file, or ``env:<variable>``.
      [Compose secrets](https://docs.docker.com/reference/compose-file/secrets/),
      [Compose configs](https://docs.docker.com/reference/compose-file/configs/).
    - ``use_api_socket: true`` provides the engine socket and the user's
      credentials. In addition to engine access, this can permit registry
      operations using those credentials.
      [Compose API socket access](https://docs.docker.com/reference/compose-file/services/#use_api_socket).
    - ``volumes_from: [container:...]`` imports another container's mounts,
      potentially exposing files or sockets whose sources are not declared in
      this Compose file. A read-only import still exposes readable data.
      [Compose imported volumes](https://docs.docker.com/reference/compose-file/services/#volumes_from).

    ### Privileged lifecycle commands

    The rule also reports ``privileged: true`` within each lifecycle hook:

    - ``pre_start`` runs an initialization step with extra privileges in a
      temporary container before the service starts.
      [Compose pre-start hooks](https://docs.docker.com/reference/compose-file/services/#pre_start).
    - ``post_start`` runs a command with extra privileges in the running service
      container after startup.
      [Compose post-start hooks](https://docs.docker.com/reference/compose-file/services/#post_start).
    - ``pre_stop`` runs a command with extra privileges before the service
      container stops.
      [Compose pre-stop hooks](https://docs.docker.com/reference/compose-file/services/#pre_stop).

    These hooks need review even when the service itself has no ``privileged``
    setting. Check what the command does and whether sandbox code can modify
    any scripts or inputs it will use with those privileges.

    ### Mounts inspect_ai's Docker sandbox refuses

    In releases after 0.3.279, inspect_ai's Docker sandbox refuses to start a
    Compose project with a mount that could let sandbox code write to host
    files ([inspect_ai#5830](https://github.com/UKGovernmentBEIS/inspect_ai/pull/5830)).
    A malicious agent with host write access could rewrite a Compose file to
    grant later samples more privileges, or redirect a later mount with a
    symlink. The rule reports the mounts that inspect_ai refuses as errors
    without an allowlist key:

    - a host bind mount without ``:ro`` in short syntax or ``read_only: true``
      in long syntax;
    - a read-only bind mount, ``configs`` or ``secrets`` in a service with
      ``privileged: true``, or ``SYS_ADMIN`` or ``ALL`` in ``cap_add``, since
      the service could remount them writable;
    - bind propagation other than ``private`` or ``rprivate``, and
      ``bind.recursive: writable``;
    - any ``volumes_from``, including ``service:`` references;
    - a named volume that is ``external``, uses a driver other than
      ``local``, sets ``driver_opts`` or sets a custom ``name``;
    - a mount type other than ``bind``, ``volume``, ``tmpfs``, ``image`` or
      ``npipe``.

    An access mode with an interpolation produces a warning, because only the
    resolved value shows whether the mount is read-only.

    Fix a refused mount by adding ``:ro``, copying the files in with
    ``Sample.files``, or using a Docker-managed volume. An evaluation that
    needs host writes can set the top-level opt-out:

    ```yaml
    x-inspect_docker_sandbox:
      unsafe_allow_writable_host_mounts: true
    ```

    inspect_ai then skips its mount checks for that Compose file, and so does
    this rule. The rule reports the opt-out itself under the key
    ``x-inspect_docker_sandbox.unsafe_allow_writable_host_mounts``, for review
    and an allowlist entry. inspect_ai accepts only a literal boolean there, so
    any other value, including an interpolation, is an error.

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
    reason next to ``default:privileged`` and
    ``default:volumes:/var/run/docker.sock`` entries in
    ``allowlists.sandbox_privileges``.

    ## Scope and related checks

    This is a static check of declared Compose settings. It does not run Docker,
    read environment files, inspect images, combine overrides, follow ``include``
    or ``extends`` files, or inspect Kubernetes settings. It does not check the
    Docker versions inspect_ai requires for read-only mounts. Interpolated
    privilege, namespace, capability, device, security-option, and mount values
    that cannot be checked produce warnings because [Compose interpolation](https://docs.docker.com/reference/compose-file/interpolation/)
    can change the effective settings at runtime. A pass means no listed
    settings were found in the files checked.

    Service ``environment`` and ``env_file`` values are not checked, though
    they can also carry host values into the container. Published ports,
    external networks, and host-gateway mappings need a network-exposure policy. Missing hardening settings such as ``read_only`` or
    ``no-new-privileges`` are outside this rule's checks.

    ## Options

    - ``allowlists.sandbox_privileges``: ``{ package = ["service:field:value"] }``
      entries report warnings while present. Each finding names its key in the
      hint. The value is the capability, device, rule, namespace mode,
      security option, host path, secret or config source, or container, so allowing
      ``default:cap_add:SYS_PTRACE`` does not allow ``ALL``, and allowing one
      host path does not allow another. Settings without a value use
      ``service:field``, such as ``default:privileged`` and
      ``default:post_start.privileged``. The writable-mount opt-out uses
      ``x-inspect_docker_sandbox.unsafe_allow_writable_host_mounts``. A key
      applies to that service across the package's Compose files. Removed
      settings leave stale entries for the runner to report.
    """
    compose_files = iter_compose_files(ctx)
    if not compose_files:
        yield Outcome("skip", "No compose files found")
        return

    issues = 0
    checked_services = 0
    for compose_file in compose_files:
        compose, problem = load_compose(compose_file)
        if problem is not None:
            issues += 1
            yield problem
            continue
        services = _mapping(compose).get("services")
        if services is None:
            services = {}
        if not isinstance(compose, dict) or not isinstance(services, dict):
            issues += 1
            yield Diagnostic(
                "Could not check Compose privileges: expected a mapping of services",
                file=compose_file,
                severity="warning",
            )
            continue
        settings: Any = _mapping(compose).get(_SANDBOX_SETTINGS)
        allow_writes = _mapping(settings).get(_ALLOW_WRITES)
        if (settings is not None and not isinstance(settings, dict)) or (
            allow_writes is not None and not isinstance(allow_writes, bool)
        ):
            issues += 1
            yield Diagnostic(
                f"{_ALLOW_WRITES_KEY} is not a literal boolean under a {_SANDBOX_SETTINGS} "
                "mapping, so inspect_ai's Docker sandbox refuses the Compose file",
                file=compose_file,
                line=line_of(compose, _SANDBOX_SETTINGS),
                hint="write it as a literal true or false, without interpolation",
            )
        elif allow_writes:
            issues += 1
            yield Diagnostic(
                f"{_ALLOW_WRITES_KEY} turns off inspect_ai's host mount checks, so sandbox "
                "code may write to host files through this Compose file's mounts",
                file=compose_file,
                line=line_of(settings, _ALLOW_WRITES),
                key=_ALLOW_WRITES_KEY,
                hint="remove it and make the mounts read-only, or review the host writes and "
                f"document them as '{_ALLOW_WRITES_KEY}' in allowlists.sandbox_privileges",
            )
        for service_name, service in _mapping(services).items():
            if not isinstance(service, dict):
                issues += 1
                yield Diagnostic(
                    f"Could not check service '{service_name}': expected a mapping",
                    file=compose_file,
                    line=line_of(services, service_name),
                    severity="warning",
                )
                continue
            checked_services += 1
            privileges = _service_privileges(
                _mapping(service), _mapping(compose), enforce_mounts=allow_writes is not True
            )
            for privilege in privileges:
                issues += 1
                key = privilege.key(service_name)
                if privilege.rejection_hint is not None:
                    key, hint = None, privilege.rejection_hint
                else:
                    hint = (
                        "remove the setting, or review the required access and document it "
                        f"as '{key}' in allowlists.sandbox_privileges"
                    )
                yield Diagnostic(
                    f"Service '{service_name}' {privilege.field}: {privilege.detail}",
                    file=compose_file,
                    line=privilege.line,
                    severity=privilege.severity,
                    key=key,
                    hint=hint,
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
