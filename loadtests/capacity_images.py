"""Pin existing target images through a loopback-only registry for T-075."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from loadtests.capacity_evidence import PINNED_IMAGE, compose_config, image_references, write_json_new
from loadtests.capacity_runtime import HostControl


IMAGE_KEYS = {
    "app": "TRACEDESK_APP_IMAGE",
    "parse-worker": "TRACEDESK_PARSE_WORKER_IMAGE",
    "parser": "TRACEDESK_PARSER_IMAGE",
    "proxy": "TRACEDESK_PROXY_IMAGE",
}


def _replace_env(source: Path, destination: Path, replacements: dict[str, str]) -> None:
    if destination.exists():
        raise ValueError("destination capacity env file already exists")
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0] if "=" in stripped and not stripped.startswith("#") else ""
        if key in replacements:
            output.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key in sorted(set(replacements) - seen):
        output.append(f"{key}={replacements[key]}")
    destination.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass


def pin_images(
    output: Path,
    *,
    control: HostControl,
    destination_env: Path,
    data_dir: Path | None = None,
    registry: str = "localhost:5000",
    tag: str = "t075",
) -> dict[str, object]:
    if re.fullmatch(r"localhost:\d{2,5}", registry) is None:
        raise ValueError("capacity registry must be loopback localhost:<port>")
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", tag) is None:
        raise ValueError("capacity image tag is invalid")
    output.mkdir(parents=True, exist_ok=False)
    config, _ = compose_config(control)
    sources = image_references(config, require_pinned=False)
    registry_probe = control.run(["docker", "inspect", "tracedesk-t075-registry"], check=False)
    if registry_probe.returncode != 0:
        control.run([
            "docker", "run", "--detach", "--restart", "unless-stopped",
            "--publish", "127.0.0.1:5000:5000", "--name", "tracedesk-t075-registry", "registry:2",
        ], timeout=300)
    else:
        control.run(["docker", "start", "tracedesk-t075-registry"], check=False)

    pinned: dict[str, str] = {}
    source_ids: dict[str, str] = {}
    for role, source in sources.items():
        image_id = control.run(["docker", "image", "inspect", "--format", "{{.Id}}", source]).stdout.strip()
        if not image_id.startswith("sha256:"):
            raise RuntimeError(f"source image identity is unavailable: {role}")
        source_ids[role] = image_id
        target = f"{registry}/tracedesk-t075/{role}:{tag}"
        control.run(["docker", "tag", source, target])
        control.run(["docker", "push", target], timeout=600)
        digests = control.run(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", target]
        ).stdout.strip()
        values = json.loads(digests)
        if type(values) is not list:
            raise RuntimeError(f"registry digest response is invalid: {role}")
        prefix = f"{registry}/tracedesk-t075/{role}@sha256:"
        match = next((value for value in values if type(value) is str and value.startswith(prefix)), None)
        if type(match) is not str or PINNED_IMAGE.fullmatch(match) is None:
            raise RuntimeError(f"pushed image did not receive a digest: {role}")
        pinned[role] = match

    replacements = {IMAGE_KEYS[role]: reference for role, reference in pinned.items()}
    if data_dir is not None:
        replacements["TRACEDESK_DATA_DIR"] = data_dir.as_posix()
    _replace_env(control.env_file, destination_env, replacements)
    capacity_control = HostControl(
        project_dir=control.project_dir,
        compose_files=control.compose_files,
        env_file=destination_env,
        project_name=control.project_name,
        generation_unit=control.generation_unit,
        runner=control.runner,
    )
    capacity_config, _ = compose_config(capacity_control)
    verified = image_references(capacity_config)
    if verified != pinned:
        raise RuntimeError("capacity compose config did not preserve pinned image references")
    report: dict[str, object] = {
        "schema_version": 1,
        "scope": "t075-image-pinning",
        "formal_claim": "none",
        "status": "prepared",
        "registry": registry,
        "source_image_ids": source_ids,
        "pinned_images": pinned,
        "destination_env": str(destination_env),
        "credentials_copied_to_report": False,
    }
    write_json_new(output / "image-pinning.json", report)
    return report
