"""
The example apps: made-up apps that, together, use every feature the modules support.

Layout: tests/fixtures/apps/<app>/{quadlets,units,init,config}/...
They are all rendered with the variables in tests/fixtures/vars.yml.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from ansible.parsing.dataloader import DataLoader

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
APPS_DIR = FIXTURES_DIR / "apps"
VARS_FILE = FIXTURES_DIR / "vars.yml"


@dataclass(frozen=True)
class ExampleApp:
    """One example app."""

    name: str

    @property
    def path(self) -> Path:
        return APPS_DIR / self.name

    @property
    def id(self) -> str:  # pylint: disable=invalid-name
        return self.name


def example_apps() -> List[ExampleApp]:
    """Every example app, sorted by name."""
    return [ExampleApp(app.name) for app in sorted(APPS_DIR.iterdir()) if app.is_dir()]


def example_vars() -> Dict[str, Any]:
    """The variables the apps are rendered with, loaded the way a playbook loads them."""
    return dict(DataLoader().load_from_file(str(VARS_FILE)) or {})
