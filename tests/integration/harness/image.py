"""
The cloud image the test VM boots from, downloaded once and kept under ~/.cache.

AlmaLinux 10 is an Enterprise Linux like the hosts this collection targets, with SELinux enforcing
and a current podman. The image is pinned by name and checksum: a test run must not change
underneath us because the mirror published a new build.
"""

import hashlib
import os
import shutil
import urllib.request
from pathlib import Path

IMAGE = "AlmaLinux-10-GenericCloud-10.2-20261012.0.x86_64.qcow2"
IMAGE_SHA256 = "cbee4726fc0f5a60694bb4f289003cc221e715dfbacebcce76b54daf386d8eca"

# The main mirror only keeps recent builds; the vault keeps them all
IMAGE_URLS = (
    f"https://repo.almalinux.org/almalinux/10.2/cloud/x86_64/images/{IMAGE}",
    f"https://vault.almalinux.org/10.2/cloud/x86_64/images/{IMAGE}",
)

CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kriansa-quadlet-tests" / "vm"
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_base_image() -> Path:
    """
    Return the path of the verified base image, downloading it first if needed.

    The download goes to a directory of its own and is only renamed into place once its checksum
    matches, so a partial or tampered file is never booted.
    """
    images = CACHE_DIR / "images"
    images.mkdir(parents=True, exist_ok=True)
    target = images / IMAGE
    # Verifying ~500 MB on every session would cost seconds; a stamp records that it was done
    stamp = images / f"{IMAGE}.sha256-verified"

    if target.exists() and stamp.exists() and stamp.read_text().strip() == IMAGE_SHA256:
        return target

    if not target.exists():
        partial = images / f"{IMAGE}.part"
        errors = []
        for url in IMAGE_URLS:
            try:
                with (
                    urllib.request.urlopen(url, timeout=60) as response,
                    open(partial, "wb") as out,
                ):
                    shutil.copyfileobj(response, out, length=1 << 20)
                break
            except OSError as e:
                errors.append(f"{url}: {e}")
        else:
            raise RuntimeError("could not download the base image:\n" + "\n".join(errors))
        partial.rename(target)

    actual = sha256_of(target)
    if actual != IMAGE_SHA256:
        target.unlink()
        raise RuntimeError(f"{IMAGE} has checksum {actual}, expected {IMAGE_SHA256}; deleted it")
    stamp.write_text(IMAGE_SHA256 + "\n")
    return target
