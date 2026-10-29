"""M. .pod and .kube quadlets."""

import pytest

from ansible_collections.kriansa.commons.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration

KUBE_POD = """\
apiVersion: v1
kind: Pod
metadata:
  name: {{ quadlet_app_name }}-kube
spec:
  containers:
    - name: idle
      image: docker.io/library/alpine:3.24
      command: ["/bin/sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"]
"""


def pod_app(make_app, tag, container_extra=""):
    app = make_app(tag)
    app.write("quadlets/backend.pod", "[Pod]\nVolume=%config%:/etc/shared\n")
    app.write("quadlets/main.container", f"[Container]\n{IMAGE}\n{IDLE}\nPod=backend.pod\n{container_extra}")
    app.write("config/backend/shared.conf", "x=1\n")
    return app


def test_pod_file_is_named_and_prefixed(make_app, host):
    app = pod_app(make_app, "podfile")

    app.deploy("installed")

    pod = host.read(app.quadlet_path("backend.pod"))
    assert f"PodName={app.name}--backend" in pod
    assert f"Volume=/srv/{app.name}/config/backend:/etc/shared:ro,Z,idmap" in pod
    container = host.read(app.quadlet_path("main.container"))
    assert f"Pod={app.name}--backend.pod" in container
    assert host.unit_exists(f"{app.name}--backend-pod.service")


def test_container_in_a_pod_runs(make_app, host):
    """A container joining a pod uses the pod's user namespace, so it gets no default of its own."""
    app = pod_app(make_app, "podns")

    step = app.deploy("started")

    assert not step.failed, step.msg
    assert "UserNS" not in host.read(app.quadlet_path("main.container"))
    assert host.active_state(app.service("main")) == "active"
    assert host.active_state(f"{app.name}--backend-pod.service") == "active"
    # A pod's volumes are mounted into every container that joins it
    assert host.container_exec(app.container("main"), "cat /etc/shared/shared.conf").stdout == "x=1\n"


def test_kube(make_app, host):
    app = make_app("kube")
    app.write("quadlets/main.kube",
              "[Kube]\nYaml=/srv/{{ quadlet_app_name }}/config/k/pod.yaml\nNetwork=main.network\n")
    app.write("quadlets/main.network", "[Network]\n")
    app.write("config/k/pod.yaml", KUBE_POD)

    step = app.deploy("started")

    assert not step.failed, step.msg
    kube = host.read(app.quadlet_path("main.kube"))
    assert "ContainerName" not in kube and "UserNS" not in kube
    assert f"Network={app.name}--main.network" in kube
    assert host.active_state(app.service("main")) == "active"
    pods = host.output("podman pod ps --format '{{.Name}} {{.Status}}'")
    assert f"{app.name}-kube Running" in pods
