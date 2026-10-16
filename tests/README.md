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
| Unit | `tests/unit/plugins/{modules,action}/` | Every branch of the module and action plugin, error paths included, with systemd faked |
| Golden | `tests/unit/plugins/test_quadlet_app_golden.py` | The example apps in `tests/fixtures/apps/` render and deploy exactly as they did at the baseline |
| Validity | `tests/unit/plugins/test_quadlet_app_dryrun.py` | Every golden output is accepted by podman's own quadlet generator |
| Integration | `tests/integration/` | Real deploys to a real host: systemd transactions, idmapped volumes, SELinux, timers |
| Sanity | `make sanity` | ansible-test's static checks |

### Unit tests and isolation

`tests/conftest.py` makes this checkout the only collection Ansible can see, and points
`passwordstore` at a deterministic stub (`tests/support/lookup_plugins/`). This machine has other
installed copies of the collection, and the real passwordstore lookup, both of which tests must
never touch; `tests/unit/test_isolation.py` checks that they cannot.

Unit tests import code the way production does, through `ansible_collections.kriansa.commons...`.
An autouse fixture makes it impossible for a unit test to run a real process on this machine.

### Goldens

Golden files live in `tests/fixtures/golden/quadlet_app/<app>/`:

- `rendered/` is what the action plugin sends to the module (Stage A)
- `deployed/` is every file the module writes on a fresh host, at its real path (Stage B)
- `result.json` is the module's result and every command it runs (Stage B)

They pin current behaviour, bugs included. They were captured from quadlet_app at commit `9678cea`,
and `make goldens` refuses to rewrite them from any other version of that code. A golden diff is a
behaviour change, never noise.

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
2. It is provisioned once with this collection's own `podman` role, plus pre-pulled images
   (`tests/integration/playbooks/provision.yml`), and cached. Changing that playbook or the
   podman role builds a new image automatically.
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

- Comments above the first section header of a `.container`, `.volume`, `.network` or `.pod` are
  dropped; a file with no header deploys empty.
- Only main's direct dependencies are restarted on a change; one two `Requires=` away keeps its
  old definition.
- `state: installed` still runs the volume ownership bootstrap.
- Validation runs after files are written, and covers every app on the host: a broken file fails
  the deploy of every other app, and a failed run leaves its own files behind, so a retry skips
  validation and fails on a unit systemd never loaded - only a file change or `force` recovers.
- `no_log` on the module's file data masks every string in it wherever it appears in the
  output: `quadlet_files`, `unit_files`, and error messages naming a file read `********`.
- A file that renders to a bare value (`{{ 42 }}`) fails the module; a non-UTF-8 file fails it
  mid-write and leaves the file empty.
- With podman's local volume driver, an empty volume's root is re-chowned to the container's
  user on every container start, undoing `X-Owner`. Volume drivers are exempt, which is why hosts
  using one are not affected.
- The injected `UserNS=auto` makes every container that joins a `.pod` fail to start; an empty
  `UserNS=` in the container avoids it.
- A container that exits right after starting does not fail the deploy: quadlet containers
  report ready when they start.
- Nothing is ever removed from the host: deleted files, volumes and timers stay deployed.
