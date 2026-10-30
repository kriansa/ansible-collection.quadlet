# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""
Values shared by the files of the quadlet_app module.

The other files read these as `constants.NAME` rather than importing each name. That way the value
is looked up every time it is used, so the unit tests can move every host path into a temporary
directory by changing it here alone.
"""

# Default deployment paths
QUADLET_SYSTEMD_DIR = "/etc/containers/systemd"
QUADLET_APP_BASE_DIR = "/srv"

# Where plain systemd units go. A timer is not a quadlet type, so it has to be deployed as itself
# rather than into the generator's directory, which systemd never reads.
SYSTEMD_UNIT_DIR = "/etc/systemd/system"

# Holds one marker per volume recording the ownership that was last applied to it. The marker
# filename carries the uid/gid, so changing a volume's declared owner changes the path and the
# bootstrap unit's condition lets it run again.
QUADLET_APP_STATE_DIR = "/var/lib/quadlet-app"

# Podman's image and volume store. Containers are ordered against it so they never start before
# the filesystem holding their image is mounted.
QUADLET_SYSTEMD_STORAGE_DIR = "/var/lib/containers/storage"

# Quadlet unit types, read from quadlets/
VALID_QUADLET_EXTENSIONS = (".container", ".volume", ".network", ".pod", ".kube")

# Plain systemd units, read from units/. Systemd reads these directly; podman's generator never
# sees them.
VALID_UNIT_EXTENSIONS = (".timer",)

# The service podman's generator emits for each quadlet type, as the suffix replacing the
# quadlet's own. Something naming a quadlet as a systemd unit has to point at this rather than at
# the quadlet's filename.
QUADLET_SERVICE_SUFFIXES = {
    ".container": ".service",
    ".kube": ".service",
    ".pod": "-pod.service",
    ".volume": "-volume.service",
    ".network": "-network.service",
}

# Directives naming one quadlet of the app, optionally followed by ":" and options
QUADLET_REFERENCE_DIRECTIVES = {"Network", "Pod", "Volume"}

# Size forced onto a bare UserNS=auto. Podman otherwise derives the range from the image's highest
# uid, which can be as little as 1024 - far too small for an app that writes as a high uid, and the
# resulting files are then unmappable.
DEFAULT_USERNS_SIZE = 65536
