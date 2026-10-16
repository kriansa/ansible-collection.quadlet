# Entry points for the test suite. Each target is a thin wrapper; see tests/README.md for details.

.PHONY: test test-cov goldens sanity ansible-test-units integration vm-up vm-clean

# Unit, golden and quadlet validity tests. The VM suite is excluded by default.
test:
	uv run pytest

test-cov:
	uv run pytest --cov --cov-report=term-missing

# Rewrites the quadlet_app golden files. Only meaningful against the baseline quadlet_app code,
# which the golden helpers verify before writing anything.
goldens:
	uv run pytest tests/unit/plugins/test_quadlet_app_golden.py --update-goldens

# ansible-test needs network access the first time, to build its per-test environments
sanity:
	tests/bin/ansible-test sanity --venv --python 3.14

# The same unit tests as `make test`, run the way ansible-test runs them
ansible-test-units:
	tests/bin/ansible-test units --venv --python 3.14

# The VM suite: one AlmaLinux VM per worker. The first run downloads and provisions the image.
integration:
	uv run pytest -m integration -n 3 tests/integration

# Boot a test VM and leave it running, to poke at by hand or reuse with --vm-attach=PORT
vm-up:
	uv run pytest -m integration tests/integration/test_harness.py --keep-vm -q

# Drop the cached base and test images (they are rebuilt on the next integration run)
vm-clean:
	rm -rf "$${XDG_CACHE_HOME:-$$HOME/.cache}/kriansa-commons-tests/vm"
