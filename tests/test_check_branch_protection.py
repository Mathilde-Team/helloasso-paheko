# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests de scripts/check_branch_protection.py, sans réseau (urlopen simulé)."""

from __future__ import annotations

import io
import json
import runpy
import sys
import urllib.error
from pathlib import Path

import check_branch_protection as cbp
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_branch_protection.py"
QUALITE = "Qualité (lint, tests)"
GARDE = "Garde : PR vers main doit venir de dev"


def regle(*checks, admins=True, contexts=()):
    """Règle versionnée minimale (format de l'API « Update branch protection »)."""
    return {
        "required_status_checks": {
            "strict": True,
            "checks": [{"context": c, "app_id": 15368} for c in checks],
            **({"contexts": list(contexts)} if contexts else {}),
        },
        "enforce_admins": admins,
    }


def branche_api(*checks, protegee=True, niveau="everyone", contexts=()):
    """Réponse publique de GET /repos/<depot>/branches/<branche>."""
    if not protegee:
        return {"name": "dev", "protected": False}
    return {
        "name": "dev",
        "protected": True,
        "protection": {
            "enabled": True,
            "required_status_checks": {
                "enforcement_level": niveau,
                "checks": [{"context": c, "app_id": 15368} for c in checks],
                "contexts": list(contexts),
            },
        },
    }


class Reponse(io.BytesIO):
    """Réponse HTTP simulée, utilisable avec ``with``."""

    def __enter__(self):
        """Rend la réponse."""
        return self

    def __exit__(self, *_args):
        """Ferme la réponse."""
        self.close()


class Requetes(list):
    """Requêtes reçues par le faux ``urlopen`` ; ``etat`` : réponse ou erreur à rendre."""

    def __init__(self):
        """Aucune requête ; réponse par défaut : dev protégée par le job Qualité."""
        super().__init__()
        self.etat = {"reponse": branche_api(QUALITE), "erreur": None}


@pytest.fixture
def requetes(monkeypatch):
    """Remplace ``urllib.request.urlopen`` ; rend la liste des requêtes reçues."""
    recues = Requetes()
    etat = recues.etat

    def urlopen(requete, timeout):
        """Note la requête, lève l'erreur prévue ou rend la réponse prévue."""
        recues.append((requete, timeout))
        if etat["erreur"] is not None:
            raise etat["erreur"]
        return Reponse(json.dumps(etat["reponse"]).encode("utf-8"))

    monkeypatch.setattr(cbp.urllib.request, "urlopen", urlopen)
    return recues


@pytest.fixture
def regles(tmp_path):
    """Dossier de règles avec dev.json exigeant le job Qualité."""
    dossier = tmp_path / "branch-protection"
    dossier.mkdir()
    (dossier / "dev.json").write_text(json.dumps(regle(QUALITE)), encoding="utf-8")
    return dossier


def lancer(monkeypatch, *args, depot="Mathilde-Team/exemple", jeton=None):
    """Exécute ``main()`` avec ``args`` et l'environnement d'un workflow."""
    if depot is None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY", depot)
    if jeton is None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GITHUB_TOKEN", jeton)
    monkeypatch.setattr(sys, "argv", ["check_branch_protection.py", *args])
    return cbp.main()


def test_lire_protection_avec_jeton(requetes):
    """GET /repos/<depot>/branches/<branche>, en-têtes de l'API, jeton en Bearer, délai 30 s."""
    assert cbp.lire_protection("o/d", "dev", "jeton-secret") == requetes.etat["reponse"]
    requete, delai = requetes[0]
    assert requete.full_url == "https://api.github.com/repos/o/d/branches/dev"
    assert requete.get_header("Accept") == "application/vnd.github+json"
    assert requete.get_header("X-github-api-version") == "2022-11-28"
    assert requete.get_header("Authorization") == "Bearer jeton-secret"
    assert delai == 30


def test_lire_protection_sans_jeton(requetes):
    """Sans jeton : aucune en-tête Authorization (dépôt public)."""
    cbp.lire_protection("o/d", "main", None)
    assert not requetes[0][0].has_header("Authorization")


def test_checks_attendus():
    """Checks de ``checks`` et de l'ancien champ ``contexts`` réunis ; règle sans checks : aucun."""
    assert cbp.checks_attendus(regle(QUALITE, contexts=["ancien"])) == {QUALITE, "ancien"}
    assert cbp.checks_attendus({"required_status_checks": None}) == set()
    assert cbp.checks_attendus({}) == set()


def test_comparer_conforme():
    """Mêmes checks, appliqués aux administrateurs : aucun écart."""
    assert cbp.comparer(regle(QUALITE), branche_api(QUALITE)) == []
    assert cbp.comparer(regle(QUALITE, contexts=["x"]), branche_api(QUALITE, contexts=["x"])) == []


def test_comparer_branche_non_protegee():
    """Branche non protégée : un seul écart, les autres contrôles n'ont pas de sens."""
    assert cbp.comparer(regle(QUALITE), branche_api(protegee=False)) == [
        "la branche n'est pas protégée"
    ]


def test_comparer_checks_manquants_et_en_trop():
    """Check absent et check non prévu : deux écarts, noms triés."""
    ecarts = cbp.comparer(regle(QUALITE, GARDE), branche_api("Z", QUALITE, "A"))
    assert ecarts == [
        f"checks requis absents : {GARDE}",
        "checks requis non prévus par la règle : A, Z",
    ]


@pytest.mark.parametrize(
    ("admins", "niveau", "ecart"),
    [
        (True, "non_admins", "attendu True, réel False"),
        (False, "everyone", "attendu False, réel True"),
        (True, "everyone", None),
        (False, "non_admins", None),
    ],
)
def test_comparer_administrateurs(admins, niveau, ecart):
    """``enforce_admins`` comparé au niveau d'application des checks."""
    ecarts = cbp.comparer(regle(QUALITE, admins=admins), branche_api(QUALITE, niveau=niveau))
    attendu = [] if ecart is None else [f"application aux administrateurs : {ecart}"]
    assert ecarts == attendu


def test_comparer_sans_checks_ignore_administrateurs():
    """Règle sans checks requis : le niveau d'application n'est pas lisible, il n'est pas comparé."""
    assert cbp.comparer({"enforce_admins": True}, branche_api(niveau="off")) == []
    assert cbp.comparer({}, {"protected": True}) == []


def test_main_conforme(monkeypatch, requetes, regles, capsys):
    """Protection conforme : code 0, notice des points non vérifiables, message de conformité."""
    assert lancer(monkeypatch, "--branche", "dev", "--regles", str(regles), jeton="t") == 0
    sortie = capsys.readouterr().out
    assert "::error::" not in sortie
    assert "::notice::non vérifiable avec le GITHUB_TOKEN" in sortie
    assert f"Protection de dev conforme à {regles}/dev.json" in sortie
    assert requetes[0][0].get_header("Authorization") == "Bearer t"
    assert requetes[0][0].full_url.endswith("/repos/Mathilde-Team/exemple/branches/dev")


def test_main_divergente(monkeypatch, requetes, regles, capsys):
    """Écart : code 1, une erreur par écart et la commande pour appliquer la règle."""
    requetes.etat["reponse"] = branche_api("Autre", niveau="non_admins")
    code = lancer(monkeypatch, "--branche", "dev", "--regles", str(regles), "--depot", "o/d")
    sortie = capsys.readouterr().out
    assert code == 1
    assert f"::error::dev : checks requis absents : {QUALITE}" in sortie
    assert "::error::dev : checks requis non prévus par la règle : Autre" in sortie
    assert "::error::dev : application aux administrateurs" in sortie
    assert f"gh api -X PUT repos/o/d/branches/dev/protection --input {regles}/dev.json" in sortie


def test_main_depot_inconnu(monkeypatch, requetes, regles, capsys):
    """Ni --depot ni GITHUB_REPOSITORY : code 2, aucune requête."""
    assert lancer(monkeypatch, "--branche", "dev", "--regles", str(regles), depot=None) == 2
    assert "::error::dépôt inconnu" in capsys.readouterr().out
    assert requetes == []


def test_main_regle_absente(monkeypatch, requetes, regles, capsys):
    """Fichier JSON absent : code 2 et chemin cité, au lieu d'une trace Python ; aucune requête."""
    assert lancer(monkeypatch, "--branche", "main", "--regles", str(regles)) == 2
    assert f"::error::règle absente : {regles}/main.json" in capsys.readouterr().out
    assert requetes == []


def test_main_regle_illisible(monkeypatch, requetes, regles, capsys):
    """JSON mal formé : code 2, ligne de l'erreur citée ; aucune requête."""
    (regles / "dev.json").write_text('{\n  "enforce_admins": tru\n}\n', encoding="utf-8")
    assert lancer(monkeypatch, "--branche", "dev", "--regles", str(regles)) == 2
    assert f"::error::règle illisible : {regles}/dev.json (ligne 2," in capsys.readouterr().out
    assert requetes == []


def test_main_erreur_http(monkeypatch, requetes, regles, capsys):
    """Erreur HTTP de l'API (branche absente, jeton refusé) : code 1, statut cité."""
    requetes.etat["erreur"] = urllib.error.HTTPError(
        "https://api.github.com/repos/o/d/branches/dev", 404, "Not Found", {}, None
    )
    assert lancer(monkeypatch, "--branche", "dev", "--regles", str(regles)) == 1
    assert "::error::lecture de la branche dev impossible : HTTP 404" in capsys.readouterr().out


def test_main_api_injoignable(monkeypatch, requetes, regles, capsys):
    """API injoignable (DNS, réseau) : code 1, cause citée, au lieu d'une trace Python."""
    requetes.etat["erreur"] = urllib.error.URLError("Name or service not known")
    assert lancer(monkeypatch, "--branche", "dev", "--regles", str(regles)) == 1
    assert "::error::API GitHub injoignable : Name or service not known" in capsys.readouterr().out


def test_branche_obligatoire(monkeypatch, capsys):
    """--branche manquante : argparse refuse (code 2)."""
    monkeypatch.setattr(sys, "argv", ["check_branch_protection.py"])
    with pytest.raises(SystemExit) as sortie:
        cbp.main()
    assert sortie.value.code == 2
    assert "--branche" in capsys.readouterr().err


def test_execution_directe(monkeypatch, requetes, regles, capsys):
    """``python scripts/check_branch_protection.py`` : ``sys.exit`` reçoit le code de ``main()``."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/d")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--branche", "dev", "--regles", str(regles)])
    with pytest.raises(SystemExit) as sortie:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert sortie.value.code == 0
    assert "conforme" in capsys.readouterr().out
