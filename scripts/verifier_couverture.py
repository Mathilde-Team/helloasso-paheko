# SPDX-License-Identifier: GPL-3.0-or-later
"""Vérifie séparément les lignes et les branches, sans arrondir les taux."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from coverage import Coverage
from loguru import logger


def verifier_couverture(rapport: dict, seuil: float) -> bool:
    """Le seuil unique vient de la configuration coverage du dépôt."""
    logger.debug("Vérification de la couverture : entrée seuil={}", seuil)
    totaux = rapport["totals"]
    conforme = True
    for nom, couverts, total in (
        ("lignes", "covered_lines", "num_statements"),
        ("branches", "covered_branches", "num_branches"),
    ):
        nombre = totaux[total]
        taux = 100 * totaux[couverts] / nombre if nombre else 100.0
        logger.info("Couverture {} : {:.4f} %, seuil {} %", nom, taux, seuil)
        conforme = conforme and taux >= seuil
    logger.debug("Vérification de la couverture : sortie conforme={}", conforme)
    return conforme


def main(argv: list[str] | None = None) -> int:
    """Renvoie un échec si le rapport est absent, invalide ou insuffisant."""
    logger.debug("Contrôle de couverture : entrée")
    arguments = sys.argv[1:] if argv is None else argv
    chemin = Path(arguments[0]) if arguments else Path("coverage.json")
    try:
        rapport = json.loads(chemin.read_text(encoding="utf-8"))
        seuil = Coverage().get_option("report:fail_under")
        code = 0 if verifier_couverture(rapport, seuil) else 1
    except (OSError, ValueError, KeyError, TypeError):
        logger.exception("Rapport de couverture illisible ou incomplet")
        code = 1
    logger.debug("Contrôle de couverture : sortie {}", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
