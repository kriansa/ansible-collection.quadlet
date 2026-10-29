# Ansible Collection - kriansa.quadlet

Deploy applications made of [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
files with one task. Each application is a directory of quadlets, systemd timers and the files its
containers mount; the `quadlet_app` module templates them, deploys them under the application's
name, and manages the resulting systemd services.

This module used to be `kriansa.commons.quadlet_app`.

## Installation

List this repository in your `requirements.yml`:

```yaml
---
collections:
  - name: https://github.com/kriansa/ansible-collection.quadlet
    type: git
    version: master
```

Then install it:

```sh
$ ansible-galaxy install -r requirements.yml
```

It needs ansible-core 2.19 or later.

## Usage

```yaml
- name: Deploy application
  kriansa.quadlet.quadlet_app:
    src: apps/myapp
    state: started
```

See the [quadlet_app documentation](docs/QUADLET_APP.md) for the directory layout, what the module
does to each file, and every option.

## Development

The tests run under pytest, in an environment managed by [uv](https://docs.astral.sh/uv/). See
[tests/README.md](tests/README.md).

```sh
$ make test          # unit tests, golden files, and a check of every output with podman's own generator
$ make sanity        # ansible-test sanity
$ make integration   # real deploys to throwaway VMs; needs KVM
```

## License

Apache 2.0
