"""SSH keys and the cloud-init seed that lets the harness log into a fresh VM."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Tuple

# The account the harness logs in as; it can sudo without a password
USER = "qa"


def ensure_ssh_key(directory: Path) -> Tuple[Path, str]:
    """
    Return (private key path, public key text), generating the pair the first time.

    The key only ever opens throwaway VMs on localhost, so it carries no passphrase.
    """
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / "id_ed25519"
    if not private.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "kriansa-quadlet-tests", "-f", str(private)],
            check=True,
        )
    return private, (directory / "id_ed25519.pub").read_text().strip()


def build_seed(iso: Path, public_key: str, hostname: str = "qa-host") -> Path:
    """Write a NoCloud seed ISO creating the harness user with the given key."""
    user_data = "\n".join(
        [
            "#cloud-config",
            f"hostname: {hostname}",
            "users:",
            f"  - name: {USER}",
            "    groups: [wheel]",
            '    sudo: "ALL=(ALL) NOPASSWD:ALL"',
            "    shell: /bin/bash",
            "    ssh_authorized_keys:",
            f"      - {public_key}",
            "ssh_pwauth: false",
            "",
        ]
    )
    with tempfile.TemporaryDirectory() as staging:
        staging_dir = Path(staging)
        (staging_dir / "user-data").write_text(user_data)
        (staging_dir / "meta-data").write_text(f"instance-id: {hostname}\nlocal-hostname: {hostname}\n")
        subprocess.run(
            ["xorriso", "-as", "mkisofs", "-quiet", "-output", str(iso), "-volid", "cidata",
             "-joliet", "-rock", "user-data", "meta-data"],
            cwd=staging_dir,
            check=True,
            # xorriso prints a banner even with -quiet
            capture_output=True,
        )
    return iso
