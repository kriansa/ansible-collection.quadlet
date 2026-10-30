"""
The provisioned test host image: built once from the base image, cached, and reused.

Its cache key covers everything that shapes it - the base image, the provisioning playbook and
this harness - so changing any of them builds a fresh one rather than testing against a stale host.
"""

import fcntl
import hashlib
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ansible_collections.kriansa.quadlet.tests.integration.harness.image import (
    CACHE_DIR,
    IMAGE_SHA256,
    ensure_base_image,
)
from ansible_collections.kriansa.quadlet.tests.integration.harness.playbook import (
    INTEGRATION_DIR,
    PlaybookRunner,
    write_inventory,
)
from ansible_collections.kriansa.quadlet.tests.integration.harness.qemu import Vm
from ansible_collections.kriansa.quadlet.tests.integration.harness.seed import (
    build_seed,
    ensure_ssh_key,
)

PROVISION_PLAYBOOK = INTEGRATION_DIR / "playbooks" / "provision.yml"

# Bump to force a rebuild after changing how the image is made in a way the key cannot see
HARNESS_VERSION = "1"

DISK_SIZE = "20G"


@contextmanager
def _locked(path: Path):
    """Serialise provisioning across concurrent sessions (pytest-xdist workers)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def image_key(public_key: str) -> str:
    digest = hashlib.sha256()
    digest.update(IMAGE_SHA256.encode())
    digest.update(HARNESS_VERSION.encode())
    digest.update(public_key.encode())
    digest.update(PROVISION_PLAYBOOK.read_bytes())
    return digest.hexdigest()[:16]


def create_overlay(base: Path, overlay: Path) -> Path:
    subprocess.run(
        [
            "qemu-img",
            "create",
            "-q",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(base),
            str(overlay),
            DISK_SIZE,
        ],
        check=True,
    )
    return overlay


def ensure_test_image() -> Path:
    """Return the provisioned image, building it first if no current one is cached."""
    private_key, public_key = ensure_ssh_key(CACHE_DIR / "ssh")
    golden = CACHE_DIR / f"test-host-{image_key(public_key)}.qcow2"

    with _locked(CACHE_DIR / "provision.lock"):
        if golden.exists():
            return golden

        base = ensure_base_image()
        with tempfile.TemporaryDirectory(prefix="provision-", dir=CACHE_DIR) as scratch:
            work = Path(scratch)
            disk = create_overlay(base, work / "disk.qcow2")
            seed = build_seed(work / "seed.iso", public_key)
            vm = Vm(
                disk=disk,
                workdir=work,
                private_key=private_key,
                seed=seed,
                internet=True,
                memory_mb=4096,
                cpus=4,
            ).start()
            try:
                vm.wait_for_ssh(timeout=300)
                subprocess.run(
                    vm.ssh_argv("sudo", "cloud-init", "status", "--wait"),
                    capture_output=True,
                    check=False,
                    timeout=600,
                )
                runner = PlaybookRunner(
                    write_inventory(work / "inventory", vm.port), private_key, work / "ansible"
                )
                run = runner.run(PROVISION_PLAYBOOK, timeout=1800)
                if run.returncode != 0:
                    raise RuntimeError(f"provisioning failed:\n{run.output}")
                vm.power_off()
            finally:
                vm.kill()

            # Flatten so the cached image does not depend on the base image staying in place
            partial = golden.with_suffix(".qcow2.part")
            subprocess.run(
                ["qemu-img", "convert", "-O", "qcow2", str(disk), str(partial)], check=True
            )
            partial.rename(golden)

        for stale in CACHE_DIR.glob("test-host-*.qcow2"):
            if stale != golden:
                stale.unlink()
    return golden
