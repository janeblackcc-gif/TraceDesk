"""Create a bounded loopback filesystem for the destructive disk-pressure scenario."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from loadtests.capacity_evidence import write_json_new


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(runner: Runner, command: Sequence[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = runner(
        list(command), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"volume command failed ({result.returncode}): {result.stderr.strip()[:500]}")
    return result


def _safe_target(path: Path) -> str:
    value = path.as_posix()
    pure = PurePosixPath(value)
    if not pure.is_absolute() or re.fullmatch(r"/srv/tracedesk/[A-Za-z0-9._/-]+", value) is None or ".." in pure.parts:
        raise ValueError("capacity volume paths must be absolute children of /srv/tracedesk")
    return value


def prepare_volume(
    output: Path,
    *,
    image_path: Path,
    mount_point: Path,
    size_mib: int = 512,
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    if not 256 <= size_mib <= 1024:
        raise ValueError("capacity volume size must be between 256 and 1024 MiB")
    image = _safe_target(image_path)
    mount = _safe_target(mount_point)
    image_parts, mount_parts = PurePosixPath(image).parts, PurePosixPath(mount).parts
    if image == mount or image_parts[:len(mount_parts)] == mount_parts or mount_parts[:len(image_parts)] == image_parts:
        raise ValueError("capacity image and mount point must not overlap")
    output.mkdir(parents=True, exist_ok=False)
    _run(runner, ["sudo", "-n", "mkdir", "-p", str(image_path.parent), mount])
    mounted = _run(runner, ["findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", mount], check=False)
    if mounted.returncode != 0:
        probe = _run(runner, ["sudo", "-n", "test", "-e", image], check=False)
        if probe.returncode != 0:
            _run(runner, ["sudo", "-n", "truncate", "--size", f"{size_mib}M", image])
            _run(runner, ["sudo", "-n", "mkfs.ext4", "-F", "-m", "0", image], timeout=300)
        _run(runner, ["sudo", "-n", "mount", "--options", "loop,nodev,nosuid,noexec", image, mount])
    source = _run(runner, ["findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", mount]).stdout.strip()
    if not source.startswith("/dev/loop"):
        raise RuntimeError("capacity data directory is not mounted from a loop device")
    associated = _run(
        runner,
        ["sudo", "-n", "losetup", "--associated", image, "--output", "NAME", "--noheadings"],
    ).stdout.split()
    if source not in associated:
        raise RuntimeError("capacity loop device is not backed by the requested image")
    _run(runner, ["sudo", "-n", "chown", "65532:65532", mount])
    _run(runner, ["sudo", "-n", "chmod", "0770", mount])
    usage = _run(runner, ["df", "--block-size=1", "--output=size,used,avail,target", mount]).stdout.strip()
    report: dict[str, object] = {
        "schema_version": 1,
        "scope": "t075-bounded-volume",
        "formal_claim": "none",
        "status": "prepared",
        "image_path": image,
        "mount_point": mount,
        "size_mib": size_mib,
        "loop_device": source,
        "df": usage,
        "mount_options": ["loop", "nodev", "nosuid", "noexec"],
    }
    write_json_new(output / "volume.json", report)
    return report
