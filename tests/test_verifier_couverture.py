# SPDX-License-Identifier: GPL-3.0-or-later
"""Non-régression du seuil indépendant des lignes et des branches."""

import json
import runpy
import sys
from pathlib import Path

import pytest

from verifier_couverture import main, verifier_couverture


def rapport(lignes=100, branches=100, total_lignes=100, total_branches=100):
    return {
        "totals": {
            "covered_lines": lignes,
            "num_statements": total_lignes,
            "covered_branches": branches,
            "num_branches": total_branches,
        }
    }


@pytest.mark.parametrize(
    "donnees,conforme",
    [
        (rapport(), True),
        (rapport(95, 95), True),
        (rapport(100, 94), False),
        (rapport(94, 100), False),
        (rapport(94, 94), False),
        (rapport(9499, 10000, 10000, 10000), False),
        (rapport(10000, 9499, 10000, 10000), False),
        (rapport(0, 0, 0, 0), True),
    ],
)
def test_axes_independants_sans_arrondi(donnees, conforme):
    assert verifier_couverture(donnees, 95) is conforme


@pytest.mark.parametrize("lignes,branches,code", [(95, 95, 0), (100, 94, 1)])
def test_commande_lit_le_seuil_unique(tmp_path, lignes, branches, code):
    chemin = tmp_path / "rapport.json"
    chemin.write_text(json.dumps(rapport(lignes, branches)), encoding="utf-8")
    assert main([str(chemin)]) == code


@pytest.mark.parametrize("contenu", ["{", "{}", '{"totals": null}'])
def test_rapport_invalide_refuse(tmp_path, contenu):
    chemin = tmp_path / "rapport.json"
    chemin.write_text(contenu, encoding="utf-8")
    assert main([str(chemin)]) == 1


def test_rapport_absent_refuse(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main([]) == 1


def test_point_entree_reel(tmp_path, monkeypatch):
    chemin = tmp_path / "rapport.json"
    chemin.write_text(json.dumps(rapport()), encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "verifier_couverture.py"
    monkeypatch.setattr(sys, "argv", [str(script), str(chemin)])
    with pytest.raises(SystemExit) as sortie:
        runpy.run_path(str(script), run_name="__main__")
    assert sortie.value.code == 0
