# Tests

Everything runs under pytest, with the environment managed by uv:

```bash
make test               # unit, golden, quadlet validity and fixture checks (seconds)
make integration        # the VM suite: real deploys to AlmaLinux VMs (~5 min, 3 VMs)
make sanity             # ansible-test sanity
make ansible-test-units # the unit tests, run the way ansible-test runs them
```

## Layers

| Layer | Where | What it proves |
|---|---|---|
| Unit | `tests/unit/plugins/{modules,module_utils,action}/` | Every branch of the module, its module_utils and the action plugin, error paths included, with systemd faked |
| Golden | `tests/unit/plugins/test_quadlet_app_golden.py` | The example apps in `tests/fixtures/apps/` render and deploy exactly as their goldens record |
| Validity | `tests/unit/plugins/test_quadlet_app_dryrun.py` | Every golden output is accepted by podman's own quadlet generator |
| Integration | `tests/integration/` | Real deploys to a real host: systemd transactions, idmapped volumes, SELinux, timers |
| Sanity | `make sanity` | ansible-test's static checks |

### Unit tests and isolation

`tests/conftest.py` makes this checkout the only collection Ansible can see, and points
`passwordstore` at a deterministic stub (`tests/support/lookup_plugins/`). This machine has other
installed copies of the collection, and the real passwordstore lookup, both of which tests must
never touch; `tests/unit/test_isolation.py` checks that they cannot.

Unit tests import code the way production does, through `ansible_collections.kriansa.quadlet...`.
An autouse fixture makes it impossible for a unit test to run a real process on this machine.

### Goldens

Golden files live in `tests/fixtures/golden/quadlet_app/<app>/`:

- `rendered/` is what the action plugin sends to the module (Stage A)
- `deployed/` is every file the module writes on a fresh host, at its real path (Stage B)
- `result.json` is the module's result and every command it runs (Stage B)

They pin current behaviour. A golden diff is a behaviour change, never noise: a commit that changes
what the module produces regenerates the goldens with `make goldens`, so its diff shows exactly
what changed.

### The example apps

`tests/fixtures/apps/` holds made-up apps that, between them, use every feature the module
supports. All of them are rendered with the variables in `tests/fixtures/vars.yml`.

| App | What it exercises |
|---|---|
| `nginx` | a single container with a config directory, templated image and port |
| `postgres` | init scripts, an owned data volume, a network, a backup job run by a timer |
| `webapp` | three containers with dependencies, a fixed subnet, a shared volume, config loops |
| `pod-app` | containers in a pod, the empty `UserNS=` workaround, a timer resolving to the pod |
| `kube-app` | a `.kube` quadlet pointing at a manifest in `config/` |
| `agent` | `UserNS=host`, a bare and a sized `UserNS=auto`, a gathered fact, another app's network |
| `templating` | lookups (subkeys, `missing=empty`, `returnall`), filters, Jinja blocks, raw and comments |
| `edge-cases` | the input shapes behind the pinned quirks that still produce valid quadlets |

`test_examples_cover_every_feature` fails if a change to the apps leaves a feature unexercised.
`tests/unit/test_fixture_hygiene.py` fails on any IP, e-mail or secret-store path in the fixtures
that is not a test value, so nothing real can end up in this public repository by accident.

To add or change an app, edit it and regenerate the goldens with `make goldens`.

### Integration tests

Each pytest session (each xdist worker) boots a throwaway VM from a cached image:

1. An AlmaLinux 10 GenericCloud image (SELinux enforcing, a current podman) is downloaded once
   and checked against its pinned sha256.
2. It is provisioned once the way the `podman` role of `kriansa.commons` sets up a container host,
   plus pre-pulled images (`tests/integration/playbooks/provision.yml`), and cached. Changing that
   playbook builds a new image automatically.
3. Every session boots a copy-on-write overlay of it, with no internet access - a test that
   would pull an image fails instead of depending on the network.

Every deploy is a real playbook run with `ansible-playbook` over ssh. Results are read back as
JSON by a small callback (`tests/integration/callback_plugins/qa_capture.py`), and host state is
read over ssh. "Restarted exactly once" is counted from systemd's journal events, not inferred.

Each test's apps get unique names and are removed when the test ends; a leftover from a crashed
test is cleaned up before the next one starts. On failure, the report includes the app's units,
deployed files, journal and playbook output.

Useful options:

```bash
uv run pytest -m integration tests/integration/test_timers.py -k fires   # a subset
make vm-up                                    # boot a VM and leave it running
uv run pytest -m integration --vm-attach=PORT ...                       # reuse it
QA_KEEP_APPS=1 uv run pytest -m integration ...  # leave apps on the VM after a run
make vm-clean                                 # drop the cached images
```

The integration tests skip themselves, saying why, when `/dev/kvm`, qemu, xorriso or ssh is
missing.

## Pinned behaviour

Tests marked "Pinned" record what the code does today even where that is wrong, so that changing
it is a visible, deliberate act. The notable ones, all verified on a real host:

- Nothing is ever removed from the host: deleted files, volumes and timers stay deployed. This one
  is by design - as with any Ansible module, removing what is no longer wanted is the playbook's
  job.
