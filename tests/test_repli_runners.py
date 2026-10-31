# SPDX-License-Identifier: GPL-3.0-or-later
"""Les checks requis gardent la surcharge runner et un repli disponible."""

from pathlib import Path

import pytest


@pytest.mark.parametrize("workflow", ["ci.yml", "guard-main.yml"])
def test_repli_github_et_surcharge_runner(workflow):
    chemin = Path(__file__).resolve().parents[1] / ".github" / "workflows" / workflow
    lignes = chemin.read_text(encoding="utf-8").splitlines()
    choix = [ligne.strip() for ligne in lignes if ligne.strip().startswith("runs-on:")]
    assert choix == [
        """runs-on: ${{ fromJSON(vars.RUNS_ON_LINUX || '"ubuntu-latest"') }}"""
    ]
