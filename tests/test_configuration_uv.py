# SPDX-License-Identifier: GPL-3.0-or-later
"""Garde-fous de la migration Poetry vers uv (issue #13), sans accès réseau."""

from pathlib import Path
import re
import tomllib

import pytest


RACINE = Path(__file__).resolve().parents[1]
CONFIGURATION = tomllib.loads((RACINE / "pyproject.toml").read_text())


def test_metadonnees_pep621_et_projet_non_empaquete():
    projet = CONFIGURATION["project"]
    assert projet["name"] == "Helloasso-Paheko"
    assert re.fullmatch(r"\d+\.\d+\.\d+", projet["version"])
    assert projet["requires-python"] == ">=3.11,<3.14"
    assert projet["license"] == "GPL-3.0-or-later"
    assert CONFIGURATION["tool"]["uv"]["package"] is False
    assert "poetry" not in CONFIGURATION["tool"]
    assert "build-system" not in CONFIGURATION


@pytest.mark.parametrize(
    "dependance",
    [
        "httpx>=0.27.0,<0.28.0",
        "requests>=2.31.0,<3.0.0",
        "python-dotenv>=1.0.0,<2.0.0",
        "email-validator>=2.0.0,<3.0.0",
        "tqdm>=4.66.1,<5.0.0",
        "aiofiles>=23.2.1,<24.0.0",
        "rich>=13.6.0,<14.0.0",
        "loguru>=0.7.3,<0.8.0",
        "tomli-w>=1.0.0,<2.0.0",
        "authlib>=1.3.0,<2.0.0",
        "playwright>=1.55.0,<2.0.0",
        "ofxtools>=0.9.5,<0.10.0",
        "pdfplumber>=0.11.0,<0.12.0",
    ],
)
def test_contraintes_applicatives_preservees(dependance):
    assert dependance in CONFIGURATION["project"]["dependencies"]


@pytest.mark.parametrize(
    "dependance",
    [
        "pytest>=7.4.0,<8.0.0",
        "pytest-asyncio>=0.21.1,<0.22.0",
        "pytest-cov==5.0.0",
        "black>=23.7.0,<24.0.0",
        "pylint>=2.17.5,<3.0.0",
        "mypy>=1.5.1,<2.0.0",
        "flake8>=6.0.0,<7.0.0",
    ],
)
def test_contraintes_de_developpement_preservees(dependance):
    assert dependance in CONFIGURATION["dependency-groups"]["dev"]


def test_palier_de_couverture_preserve():
    assert CONFIGURATION["tool"]["coverage"]["run"]["branch"] is True
    assert CONFIGURATION["tool"]["coverage"]["report"]["fail_under"] >= 16


def test_verrou_uv_versionne_et_poetry_retire():
    verrou = tomllib.loads((RACINE / "uv.lock").read_text())
    assert verrou["requires-python"] == ">=3.11, <3.14"
    paquet = next(p for p in verrou["package"] if p["name"] == "helloasso-paheko")
    assert paquet["version"] == CONFIGURATION["project"]["version"]
    assert paquet["source"] == {"virtual": "."}
    assert not (RACINE / "poetry.lock").exists()


def test_ci_et_documentation_utilisent_le_verrou():
    workflow = (RACINE / ".github/workflows/ci.yml").read_text()
    assert "uv sync --locked --python 3.13" in workflow
    assert "uv run --locked --python 3.13" in workflow
    assert "--no-project" not in workflow
    assert "poetry run" not in (RACINE / "README.md").read_text()
