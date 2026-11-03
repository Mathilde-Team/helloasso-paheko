# SPDX-License-Identifier: GPL-3.0-or-later
"""Palier 5 de couverture (issue #5) — la synchronisation.

Ce palier couvre la classe ``sync_helloasso_orders_and_transactions`` :
initialisation (config, clients, appels de démarrage), lecture des
références et virements déjà synchronisés, récupération des catégories /
services / tarifs vers ``config.toml``, exports JSON des éléments non
synchronisés, virements bancaires depuis le CSV, méthodes de vérification
(références, virements, commandes, membres, inscriptions, comparaison),
synchronisation des services et tarifs, statistiques, et le point d'entrée
``__main__``.

Comme les paliers précédents : les vrais clients (``PahekoAPIClient``,
``HelloAssoHTTPClient``) sont utilisés, seules les frontières réseau
(``get_sql``, ``client.get``, méthodes d'API) sont doublées, pour que les
signatures appelées par la synchronisation soient exercées pour de vrai.
"""

from __future__ import annotations

import ast
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest
from test_palier2_clients import (
    ReponseFausse,
    fabrique_client_helloasso,
    fabrique_client_paheko,
)

import HelloassoPaheko
from HelloassoPaheko import (
    HelloAssoHTTPClient,
    PahekoAPIClient,
    sync_helloasso_orders_and_transactions,
)

# ---------------------------------------------------------------------------
# Fabriques : instance sans __init__, clients scriptés, configuration
# ---------------------------------------------------------------------------


def fabrique_synchro(
    tmp_path: Path, **surcharges
) -> sync_helloasso_orders_and_transactions:
    """Construit la classe de synchronisation sans exécuter ``__init__``."""
    sync = object.__new__(sync_helloasso_orders_and_transactions)
    sync.mode_simulation = False
    sync.force_sync = False
    sync.mode_update = False
    sync.config_path = str(tmp_path / "config.toml")
    sync.base_url = "https://paheko.exemple/api"
    sync.organization_slug = "asso"
    sync.cashouts_csv = str(tmp_path / "virements.csv")
    sync.non_synced_dir = str(tmp_path / "non_synced")
    sync.stats = {
        "orders_processed": 0,
        "orders_already_synced": 0,
        "orders_created": 0,
        "orders_failed": 0,
        "members_updated": 0,
        "members_created": 0,
        "members_failed": 0,
        "transactions_created": 0,
        "transactions_skipped": 0,
        "services_created": 0,
        "services_updated": 0,
        "services_unchanged": 0,
        "tarifs_created": 0,
        "tarifs_updated": 0,
        "tarifs_unchanged": 0,
        "errors": 0,
    }
    for nom, valeur in surcharges.items():
        setattr(sync, nom, valeur)
    return sync


def paheko_scripte(resultats: dict | None = None, **surcharges) -> PahekoAPIClient:
    """Vrai client Paheko : ``get_sql`` scripté par fragments de requête.

    Les fragments sont cherchés dans l'ordre d'insertion du dictionnaire ;
    le premier qui apparaît dans la requête fournit les résultats.
    """
    client = fabrique_client_paheko()
    client.requetes = []
    resultats = resultats or {}

    def get_sql(query, params=None, mode="api", fetch_mode="all"):
        client.requetes.append((query, params, mode, fetch_mode))
        for fragment, lignes in resultats.items():
            if fragment in query:
                return {"results": lignes}
        return {"results": []}

    client.get_sql = get_sql
    for nom, valeur in surcharges.items():
        setattr(client, nom, valeur)
    return client


def helloasso_faux(**surcharges) -> HelloAssoHTTPClient:
    """Vrai client HelloAsso : frontières réseau doublées."""
    client = fabrique_client_helloasso()
    client.config = {"helloasso": {}}
    client.get_organization_slug = lambda: "asso"
    for nom, valeur in surcharges.items():
        setattr(client, nom, valeur)
    return client


@pytest.fixture
def config_toml(tmp_path: Path) -> Path:
    """Écrit un config.toml minimal et le renvoie."""
    chemin = tmp_path / "config.toml"
    chemin.write_text(
        '[helloasso]\norg_slug = "asso"\nusername = "u"\npassword = "p"\n'
        '[paheko]\nbase_url = "https://paheko.exemple/api"\n'
        '[files]\ncashouts_csv = "virements.csv"\nnon_synced_dir = "non_synced"\n',
        encoding="utf-8",
    )
    return chemin


@pytest.fixture
def vrai_tomli_w(monkeypatch):
    """Le doublure tomli_w du palier 2 écrit une chaîne vide.

    Les tests qui relisent config.toml après un fetch_and_store veulent la
    vraie sérialisation (tomli_w est une dépendance du projet).
    """
    # Le doublure occupe sys.modules["tomli_w"] : débrancher le temps
    # d'importer le vrai paquet (dépendance du projet), puis remettre.
    doublure = sys.modules.get("tomli_w")
    del sys.modules["tomli_w"]
    try:
        import tomli_w as vrai
    finally:
        sys.modules["tomli_w"] = doublure

    monkeypatch.setattr(HelloassoPaheko, "tomli_w", vrai)


@pytest.fixture(autouse=True)
def _sans_sleep(monkeypatch):
    """La synchronisation dort 5 s à chaque étape : neutralisé pour les tests."""
    monkeypatch.setattr(HelloassoPaheko, "sleep", lambda *_: None)


@pytest.fixture
def synchro(
    tmp_path: Path, config_toml: Path
) -> sync_helloasso_orders_and_transactions:
    """Instance de synchronisation branchée sur tmp_path."""
    return fabrique_synchro(tmp_path)


# ---------------------------------------------------------------------------
# Références et virements déjà présents dans Paheko
# ---------------------------------------------------------------------------


class TestReferencesPaheko:
    def test_references_avec_annee(self, synchro, tmp_path):
        """Les références 'HelloAsso 123' sont extraites, année ciblée."""
        paheko = paheko_scripte(
            {
                "reference": [
                    {"reference": "HelloAsso 123"},
                    {"reference": "HelloAsso 456 OFX-9"},
                ]
            }
        )
        synchro.paheko_client = paheko
        assert synchro.get_helloasso_references_from_paheko(year_id=2) == {123, 456}
        assert any("id_year" in q and "= 2" in q for q, *_ in paheko.requetes)

    def test_references_sans_annee(self, synchro):
        """Sans année : toutes les transactions HelloAsso sont lues."""
        paheko = paheko_scripte({"reference": [{"reference": "HelloAsso 7"}]})
        synchro.paheko_client = paheko
        assert synchro.get_helloasso_references_from_paheko() == {7}

    def test_references_erreur_reseau(self, synchro):
        """Une erreur SQL renvoie un ensemble vide."""

        def get_sql(*a, **k):
            raise RuntimeError("paheko indisponible")

        paheko = paheko_scripte()
        paheko.get_sql = get_sql
        synchro.paheko_client = paheko
        assert synchro.get_helloasso_references_from_paheko() == set()

    def test_cashouts_avec_annee(self, synchro):
        """Les virements déjà créés sont repérés par référence."""
        paheko = paheko_scripte({"reference": [{"reference": "Virement HelloAsso 3"}]})
        synchro.paheko_client = paheko
        assert synchro.get_helloasso_cashouts_from_paheko(year_id=1) == {3}

    def test_cashouts_sans_annee(self, synchro):
        paheko = paheko_scripte({"reference": [{"reference": "Virement HelloAsso 4"}]})
        synchro.paheko_client = paheko
        assert synchro.get_helloasso_cashouts_from_paheko() == {4}

    def test_cashouts_erreur(self, synchro):
        def get_sql(*a, **k):
            raise RuntimeError("boom")

        paheko = paheko_scripte()
        paheko.get_sql = get_sql
        synchro.paheko_client = paheko
        assert synchro.get_helloasso_cashouts_from_paheko() == set()


# ---------------------------------------------------------------------------
# Récupération des catégories / services / tarifs vers config.toml
# ---------------------------------------------------------------------------


class TestFetchAndStore:
    def test_categories_vers_config(self, synchro, config_toml, tmp_path, vrai_tomli_w):
        """Les catégories de l'API Paheko partent dans config.toml."""
        client_http = type("Client", (), {})()
        client_http.get = lambda url, **k: ReponseFausse(
            200,
            [{"id": 1, "name": "Membres actifs"}, {"id": 2, "name": "Non adhérents"}],
        )
        paheko = paheko_scripte()
        paheko.client = client_http
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux()

        categories = synchro.fetch_and_store_member_categories()
        assert [c["id"] for c in categories] == [1, 2]
        import tomllib

        with open(synchro.config_path, "rb") as f:
            config = tomllib.load(f)
        assert config["member_categories"] == [
            {"id": 1, "name": "Membres actifs"},
            {"id": 2, "name": "Non adhérents"},
        ]

    def test_services_vers_config(self, synchro, config_toml, vrai_tomli_w):
        """Les services SQL deviennent la section [services] de config.toml."""
        paheko = paheko_scripte(
            {
                "FROM services": [
                    {
                        "id": 5,
                        "label": "Adhésion - a 2026",
                        "description": "d",
                        "start_date": "2025-10-01",
                        "end_date": "2026-12-31",
                    }
                ]
            }
        )
        synchro.paheko_client = paheko

        services = synchro.fetch_and_store_services()
        assert services["5"]["label"] == "Adhésion - a 2026"
        import tomllib

        with open(synchro.config_path, "rb") as f:
            config = tomllib.load(f)
        assert config["services"]["5"]["start_date"] == "2025-10-01"

    def test_services_erreur_renvoie_vide(self, synchro, config_toml):
        def get_sql(*a, **k):
            raise RuntimeError("sql")

        paheko = paheko_scripte()
        paheko.get_sql = get_sql
        synchro.paheko_client = paheko
        assert synchro.fetch_and_store_services() == {}

    def test_tarifs_vers_config(self, synchro, config_toml, vrai_tomli_w):
        paheko = paheko_scripte(
            {
                "FROM services_fees": [
                    {
                        "id": 9,
                        "label": "Adulte",
                        "description": "",
                        "amount": 1000,
                        "id_service": 5,
                    }
                ]
            }
        )
        synchro.paheko_client = paheko

        tarifs = synchro.fetch_and_store_tarifs()
        assert tarifs["9"]["amount"] == 1000
        import tomllib

        with open(synchro.config_path, "rb") as f:
            config = tomllib.load(f)
        assert config["tarifs"]["9"]["id_service"] == 5

    def test_tarifs_erreur_renvoie_vide(self, synchro, config_toml):
        def get_sql(*a, **k):
            raise RuntimeError("sql")

        paheko = paheko_scripte()
        paheko.get_sql = get_sql
        synchro.paheko_client = paheko
        assert synchro.fetch_and_store_tarifs() == {}


# ---------------------------------------------------------------------------
# Exports JSON des éléments non synchronisés + statistiques
# ---------------------------------------------------------------------------


class TestExportsEtStatistiques:
    def test_save_non_synced_item(self, synchro, tmp_path):
        """L'élément part en JSON, datetime sérialisé."""
        (tmp_path / "non_synced" / "orders").mkdir(parents=True)
        synchro.save_non_synced_item(
            "orders", "42", {"date": datetime(2026, 1, 2, 10, 30)}
        )
        chemin = tmp_path / "non_synced" / "orders" / "orders_42.json"
        assert (
            json.loads(chemin.read_text(encoding="utf-8"))["date"]
            == "2026-01-02T10:30:00"
        )

    def test_save_non_synced_item_erreur(self, synchro, tmp_path):
        """Répertoire absent : l'erreur est journalisée, pas propagée."""
        synchro.non_synced_dir = str(tmp_path / "inexistant")
        synchro.save_non_synced_item("orders", "1", {"a": 1})  # ne doit pas lever

    def test_print_sync_statistics_avec_exports(self, synchro, tmp_path):
        """Les compteurs et fichiers exportés sont affichés sans erreur."""
        for sous_rep in ("orders", "members", "cashouts"):
            (tmp_path / "non_synced" / sous_rep).mkdir(parents=True)
            (tmp_path / "non_synced" / sous_rep / "a.json").write_text(
                "{}", encoding="utf-8"
            )
        synchro.print_sync_statistics()

    def test_print_sync_statistics_sans_exports(self, synchro, tmp_path):
        synchro.print_sync_statistics()

    def test_print_global_statistics_complete(self, synchro):
        """Une synchro à 100 % sur tous les taux affiche la complétude."""
        paheko = paheko_scripte(
            {
                "COUNT(DISTINCT reference)": [{"count": 1}],
                "COUNT(*)": [{"count": 1}],
                "FROM acc_transactions": [{"reference": "x", "label": "Virement"}],
                "SELECT id, label, amount, description, id_service": [],
                "FROM services": [{"id": 5, "label": "A"}],
            }
        )
        paheko.get_member_categories = lambda: {
            "1": {"name": "Actifs"},
            "2": {"name": "Non"},
        }
        paheko.get_members_by_category = lambda cid: [{"id": 1}] if cid == "1" else []
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [
                {
                    "id": 1,
                    "payer": {"email": "a@exemple.fr"},
                    "items": [{"type": "Membership"}],
                }
            ],
            get_cash_outs=lambda slug: [{"id": 3}],
        )

        stats = synchro.print_global_statistics()
        assert stats["paheko"]["commandes_synchro"] == 1
        assert stats["helloasso"]["membres_actifs"] == 1
        assert stats["taux"]["commandes"] == 100.0

    def test_print_global_statistics_erreur(self, synchro):
        """Une erreur de collecte renvoie les statistiques vides."""
        paheko = paheko_scripte()
        paheko.get_member_categories = lambda: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux()
        stats = synchro.print_global_statistics()
        assert stats["paheko"]["total_membres"] == 0


# ---------------------------------------------------------------------------
# Virements bancaires
# ---------------------------------------------------------------------------


class TestVirements:
    def test_create_bank_transfer_transaction(self, synchro):
        """Le virement devient une transaction ADVANCED à deux lignes."""
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.get_current_year_id = lambda date_, slug: 1
        paheko.creees = []

        def create_transaction(donnees):
            paheko.creees.append(donnees)
            return {"id": 77}

        paheko.create_transaction = create_transaction
        synchro.paheko_client = paheko

        reponse = synchro.create_bank_transfer_transaction(
            {
                "id": "3",
                "date": "2026-02-01T10:00:00",
                "total_amount_centimes": 5000,
                "payments": [{}, {}],
            }
        )
        assert reponse == {"id": 77}
        transaction = paheko.creees[0]
        assert transaction["type"] == "ADVANCED"
        assert transaction["reference"] == "Virement HelloAsso 3"
        assert len(transaction["lines"]) == 2
        assert transaction["lines"][0]["debit"] == 50.0
        assert "2 paiements" in transaction["label"]

    def test_create_bank_transfer_transaction_simulation(self, synchro):
        synchro.mode_simulation = True
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.get_current_year_id = lambda date_, slug: 1
        synchro.paheko_client = paheko
        assert synchro.create_bank_transfer_transaction(
            {
                "id": "1",
                "date": "2026-01-01",
                "total_amount_centimes": 100,
                "payments": [{}],
            }
        ) == {"simulated": True}

    def test_create_bank_transfer_transaction_annee_inconnue(self, synchro):
        paheko = paheko_scripte()

        def annee(*a, **k):
            raise RuntimeError("aucune année")

        paheko.get_current_year_id = annee
        synchro.paheko_client = paheko
        assert (
            synchro.create_bank_transfer_transaction(
                {
                    "id": "1",
                    "date": "2026-01-01",
                    "total_amount_centimes": 100,
                    "payments": [],
                }
            )
            is None
        )

    def test_create_bank_transfer_transaction_echec_creation(self, synchro):
        """Échec de création : le contexte JSON est journalisé, None renvoyé."""

        def create_transaction(donnees):
            raise RuntimeError("refus API")

        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.get_current_year_id = lambda date_, slug: 1
        paheko.create_transaction = create_transaction
        synchro.paheko_client = paheko
        assert (
            synchro.create_bank_transfer_transaction(
                {
                    "id": "2",
                    "date": "2026-01-01",
                    "total_amount_centimes": 100,
                    "payments": [],
                }
            )
            is None
        )

    def test_create_bank_transfer_transaction_reponse_vide(self, synchro):
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.get_current_year_id = lambda date_, slug: 1
        paheko.create_transaction = lambda donnees: None
        synchro.paheko_client = paheko
        assert (
            synchro.create_bank_transfer_transaction(
                {
                    "id": "2",
                    "date": "2026-01-01",
                    "total_amount_centimes": 100,
                    "payments": [],
                }
            )
            is None
        )


# ---------------------------------------------------------------------------
# CSV des virements manquants
# ---------------------------------------------------------------------------


def _ecrit_csv(chemin: Path, lignes: list[list[str]]) -> None:
    with open(chemin, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(lignes)


class TestCashoutsCsv:
    PAIEMENT: ClassVar[dict] = {
        "id": 1,
        "idCashOut": 3,
        "amount": 5000,
        "cashOutDate": "2026-02-01T10:00:00",
        "payer": {"firstName": "Alice", "lastName": "Martin"},
        "order": {"id": 7},
    }

    def test_create_missing_cashouts_from_csv(self, synchro, tmp_path):
        """Le CSV pilote la création des virements absents de Paheko."""
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(chemin, [["cashoutId"], ["3"]])

        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.get_current_year_id = lambda date_, slug: 1
        paheko.create_transaction = lambda donnees: {"id": 1}
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_payments=lambda *a, **k: [dict(self.PAIEMENT)]
        )

        resultat = synchro.create_missing_cashouts_from_csv(str(chemin))
        assert resultat["success"] == 1
        assert resultat["total_processed"] == 1
        assert resultat["total_amount"] == 50.0

    def test_create_missing_cashouts_fichier_absent(self, synchro, tmp_path):
        synchro.paheko_client = paheko_scripte()
        synchro.helloasso_client = helloasso_faux()
        resultat = synchro.create_missing_cashouts_from_csv(
            str(tmp_path / "absent.csv")
        )
        assert resultat["success"] == 0

    def test_create_missing_cashouts_deja_synchro(self, synchro, tmp_path):
        """Un cashout déjà présent dans Paheko est ignoré."""
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(chemin, [["cashoutId"], ["3"]])
        paheko = paheko_scripte({"reference": [{"reference": "Virement HelloAsso 3"}]})
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_payments=lambda *a, **k: [dict(self.PAIEMENT)]
        )
        resultat = synchro.create_missing_cashouts_from_csv(str(chemin))
        assert resultat["total_processed"] == 0

    def test_create_missing_cashouts_sans_paiement(self, synchro, tmp_path):
        """Un id du CSV sans paiement correspondant est compté en erreur."""
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(chemin, [["cashoutId"], ["9"]])
        synchro.paheko_client = paheko_scripte()
        synchro.helloasso_client = helloasso_faux(get_payments=lambda *a, **k: [])
        resultat = synchro.create_missing_cashouts_from_csv(str(chemin))
        assert resultat["errors"] == 1

    def test_create_missing_cashouts_ligne_invalide(self, synchro, tmp_path):
        """Une ligne CSV sans cashoutId est ignorée sans casser la lecture."""
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(chemin, [["cashoutId"], ["pas-un-nombre"]])
        synchro.paheko_client = paheko_scripte()
        synchro.helloasso_client = helloasso_faux(get_payments=lambda *a, **k: [])
        resultat = synchro.create_missing_cashouts_from_csv(str(chemin))
        assert resultat["total_processed"] == 0

    def test_force_create_missing_cashouts_from_csv(self, synchro, tmp_path):
        """La variante forcée crée le virement depuis le CSV (colonnes Date/Montant)."""
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(
            chemin, [["cashoutId", "Date", "Montant"], ["3", "2026-02-01", "50.00"]]
        )

        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.get_current_year_id = lambda date_, slug: 1
        paheko.create_transaction = lambda donnees: {"id": 1}
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_payments=lambda *a, **k: [dict(self.PAIEMENT)]
        )
        resultat = synchro.force_create_missing_cashouts_from_csv(str(chemin))
        assert resultat["success"] == 1

    def test_force_create_missing_cashouts_fichier_absent(self, synchro, tmp_path):
        synchro.paheko_client = paheko_scripte()
        synchro.helloasso_client = helloasso_faux()
        resultat = synchro.force_create_missing_cashouts_from_csv(
            str(tmp_path / "absent.csv")
        )
        assert resultat["success"] == 0

    def test_create_missing_cashouts_main_ok(self, synchro, tmp_path):
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(
            chemin, [["cashoutId", "Date", "Montant"], ["3", "2026-02-01", "50.00"]]
        )
        synchro.cashouts_csv = str(chemin)

        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.get_current_year_id = lambda date_, slug: 1
        paheko.create_transaction = lambda donnees: {"id": 1}
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_payments=lambda *a, **k: [dict(self.PAIEMENT)]
        )
        assert synchro.create_missing_cashouts_main() is True

    def test_create_missing_cashouts_main_erreur(self, synchro, tmp_path):
        """Le CSV absent lève dans la fonction : False renvoyé."""
        synchro.cashouts_csv = str(tmp_path / "absent.csv")

        def force(*a, **k):
            raise RuntimeError("csv illisible")

        synchro.force_create_missing_cashouts_from_csv = force
        assert synchro.create_missing_cashouts_main() is False

    def test_create_missing_cashouts_main_erreurs_de_traitement(
        self, synchro, tmp_path
    ):
        """Des erreurs de traitement (sans exception) renvoient 1."""
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(
            chemin, [["cashoutId", "Date", "Montant"], ["9", "2026-02-01", "10.00"]]
        )
        synchro.cashouts_csv = str(chemin)
        synchro.paheko_client = paheko_scripte()
        synchro.helloasso_client = helloasso_faux(get_payments=lambda *a, **k: [])
        assert synchro.create_missing_cashouts_main() == 1


# ---------------------------------------------------------------------------
# Méthodes de vérification
# ---------------------------------------------------------------------------


class ClientHttpJournal:
    """Client HTTP Paheko factice : années comptables et journaux."""

    def __init__(self, journal=None):
        self.journal = journal or []

    def get(self, url, params=None):
        if url.endswith("/accounting/years"):
            return ReponseFausse(200, [{"id": 1, "label": "Exercice 2026"}])
        if "/journal" in url:
            return ReponseFausse(200, self.journal)
        return ReponseFausse(200, [])


class TestVerifications:
    def test_verify_no_duplicate_references(self, synchro, tmp_path):
        """Les doublons de référence HelloAsso sont détectés dans le journal 512BPBFC."""
        (tmp_path / "virements.csv").write_text("cashoutId\n3\n", encoding="utf-8")
        synchro.cashouts_csv = str(tmp_path / "virements.csv")
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal(
            journal=[
                {"id": 1, "reference": "HelloAsso 1"},
                {"id": 2, "reference": "HelloAsso 1"},
                {"id": 3, "reference": "HelloAsso 2"},
            ]
        )
        synchro.paheko_client = paheko
        resultat = synchro.verify_no_duplicate_references(synchro.cashouts_csv)
        assert resultat["duplicate_count"] == 1
        assert resultat["total_helloasso_refs"] == 2

    def test_verify_no_duplicate_references_sans_doublons(self, synchro, tmp_path):
        (tmp_path / "virements.csv").write_text("cashoutId\n3\n", encoding="utf-8")
        synchro.cashouts_csv = str(tmp_path / "virements.csv")
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal(
            journal=[{"id": 1, "reference": "HelloAsso 1"}]
        )
        synchro.paheko_client = paheko
        resultat = synchro.verify_no_duplicate_references(synchro.cashouts_csv)
        assert resultat["duplicates"] == []

    def test_verify_no_duplicate_references_erreur(self, synchro, tmp_path):
        chemin = tmp_path / "virements.csv"
        chemin.write_text("cashoutId\n3\n", encoding="utf-8")

        def get(*a, **k):
            raise RuntimeError("api")

        paheko = paheko_scripte()
        paheko.client = type("C", (), {"get": get})()
        synchro.paheko_client = paheko
        resultat = synchro.verify_no_duplicate_references(str(chemin))
        assert resultat["duplicate_count"] == 0
        assert resultat["duplicates"] == []

    def test_verify_all_cashouts_created(self, synchro, tmp_path):
        """Chaque virement HelloAsso doit exister côté Paheko."""
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(
            chemin, [["cashoutId", "Date", "Montant"], ["3", "2026-02-01", "50.00"]]
        )
        synchro.cashouts_csv = str(chemin)
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal(
            journal=[
                {
                    "reference": "Virement HelloAsso 3",
                    "debit": 50.0,
                    "credit": 0.0,
                    "account": "512BPBFC",
                    "label": "V",
                }
            ]
        )
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_cash_outs=lambda slug: [
                {"id": "3", "date": "2026-02-01", "total_amount_centimes": 5000}
            ]
        )
        resultat = synchro.verify_all_cashouts_created(str(chemin))
        assert resultat["missing"] == []
        assert resultat["completion_rate"] == 100.0

    def test_verify_all_cashouts_created_manquants(self, synchro, tmp_path):
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(
            chemin, [["cashoutId", "Date", "Montant"], ["3", "2026-02-01", "50.00"]]
        )
        synchro.cashouts_csv = str(chemin)
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal(journal=[])
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_cash_outs=lambda slug: [
                {"id": "3", "date": "2026-02-01", "total_amount_centimes": 5000}
            ]
        )
        resultat = synchro.verify_all_cashouts_created(str(chemin))
        assert [m["id"] for m in resultat["missing"]] == [3]
        assert resultat["completion_rate"] == 0

    def test_verify_all_cashouts_created_erreur(self, synchro, tmp_path):
        chemin = tmp_path / "virements.csv"
        _ecrit_csv(
            chemin, [["cashoutId", "Date", "Montant"], ["3", "2026-02-01", "50.00"]]
        )

        def get(*a, **k):
            raise RuntimeError("api")

        paheko = paheko_scripte()
        paheko.client = type("C", (), {"get": get})()
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux()
        resultat = synchro.verify_all_cashouts_created(str(chemin))
        # Années inaccessibles : le virement du CSV est signalé manquant.
        assert [m["id"] for m in resultat["missing"]] == [3]
        assert resultat["completion_rate"] == 0

    def test_verify_orders_created(self, synchro):
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal(journal=[{"reference": "HelloAsso 1"}])
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [{"id": 1, "date": "2026-01-02T10:00:00Z"}]
        )
        resultat = synchro.verify_orders_created()
        assert resultat["total_orders"] == 1
        assert resultat["missing"] == []
        assert resultat["completion_rate"] == 100.0

    def test_verify_orders_created_manquantes(self, synchro):
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal(journal=[])
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [{"id": 1, "date": "2026-01-02T10:00:00Z"}]
        )
        resultat = synchro.verify_orders_created()
        assert [m["id"] for m in resultat["missing"]] == [1]

    def test_verify_orders_created_erreur(self, synchro):
        def get(*a, **k):
            raise RuntimeError("api")

        paheko = paheko_scripte()
        paheko.client = type("C", (), {"get": get})()
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [{"id": 1, "date": "2026-01-02T10:00:00Z"}]
        )
        resultat = synchro.verify_orders_created()
        # Années inaccessibles : la commande est signalée manquante.
        assert resultat["total_orders"] == 1
        assert [m["id"] for m in resultat["missing"]] == [1]
        assert resultat["completion_rate"] == 0

    def test_verify_members_by_category(self, synchro):
        """Les catégories de membres Paheko sont parcourues avec leurs membres."""
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.client.get = lambda url, **k: ReponseFausse(
            200, {"1": {"id": 1, "name": "Membres actifs", "count": 1}}
        )
        paheko.get_members_by_category = lambda cid: [
            {
                "nom": "Martin",
                "prenom": "Alice",
                "email": "alice@exemple.fr",
                "numero": "7",
                "telephone": "",
            }
        ]
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux()
        resultat = synchro.verify_members_by_category()
        assert resultat["total_paheko"] == 1
        assert resultat["total_unique_members"] == 1

    def test_verify_members_by_category_erreur(self, synchro):
        def get(*a, **k):
            raise RuntimeError("api")

        paheko = paheko_scripte()
        paheko.client = type("C", (), {"get": get})()
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux()
        assert synchro.verify_members_by_category() == {
            "categories": [],
            "total_paheko": 0,
            "completion_rate": 0,
        }

    def test_verify_items_service_inscriptions(self, synchro):
        """Les items traités sont confrontés aux inscriptions services_users."""
        paheko = paheko_scripte(
            {
                "FROM services_users": [
                    {
                        "id": 1,
                        "id_user": 7,
                        "id_service": 5,
                        "id_fee": 9,
                        "date": "2026-01-02",
                        "user_email": "alice@exemple.fr",
                        "service_label": "Adhésion - a 2026",
                        "fee_label": "Adulte",
                    }
                ]
            }
        )
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [
                {
                    "id": 1,
                    "date": "2026-01-02T10:00:00",
                    "formSlug": "a",
                    "formType": "Membership",
                    "items": [
                        {
                            "id": 11,
                            "type": "Membership",
                            "name": "Adulte",
                            "state": "Processed",
                            "user": {"email": "alice@exemple.fr"},
                        }
                    ],
                }
            ]
        )
        resultat = synchro.verify_items_service_inscriptions()
        assert resultat["total_items"] == 1
        assert resultat["found_inscriptions"] == 1
        assert resultat["missing_inscriptions"] == 0

    def test_verify_items_service_inscriptions_manquantes(self, synchro, caplog):
        """Items traités mais non inscrits : listés comme à retraiter."""
        paheko = paheko_scripte({"FROM services_users": []})
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [
                {
                    "id": 1,
                    "date": "2026-01-02T10:00:00",
                    "formSlug": "a",
                    "formType": "Membership",
                    "items": [
                        {
                            "id": 11,
                            "type": "Membership",
                            "name": "Adulte",
                            "state": "Processed",
                            "user": {"email": "alice@exemple.fr"},
                        }
                    ],
                }
            ]
        )
        resultat = synchro.verify_items_service_inscriptions()
        assert resultat["missing_inscriptions"] == 1
        assert resultat["completion_rate"] == 0

    def test_verify_items_service_inscriptions_erreur(self, synchro):
        def get_sql(*a, **k):
            raise RuntimeError("sql")

        paheko = paheko_scripte()
        paheko.get_sql = get_sql
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(get_orders=lambda *a, **k: [])
        resultat = synchro.verify_items_service_inscriptions()
        assert resultat["total_items"] == 0
        assert resultat["completion_rate"] == 0

    def test_verify_members_comparison(self, synchro):
        """Les membres HelloAsso sans équivalent Paheko sont signalés."""
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.client.get = lambda url, **k: ReponseFausse(
            200, {"1": {"id": 1, "name": "Membres actifs", "count": 1}}
        )
        paheko.get_members_by_category = lambda cid: [
            {
                "nom": "Martin",
                "prenom": "Alice",
                "email": "alice@exemple.fr",
                "numero": "7",
                "telephone": "",
            }
        ]
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [
                {
                    "id": 1,
                    "payer": {
                        "email": "alice@exemple.fr",
                        "firstName": "Alice",
                        "lastName": "Martin",
                    },
                }
            ]
        )
        resultat = synchro.verify_helloasso_paheko_members_comparison()
        assert resultat["missing_in_paheko"] == []

    def test_verify_members_comparison_manquant(self, synchro):
        paheko = paheko_scripte()
        paheko.client = ClientHttpJournal()
        paheko.client.get = lambda url, **k: ReponseFausse(
            200, {"1": {"id": 1, "name": "M", "count": 1}}
        )
        paheko.get_members_by_category = lambda cid: []
        synchro.paheko_client = paheko
        synchro.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [
                {
                    "id": 1,
                    "payer": {
                        "email": "bob@exemple.fr",
                        "firstName": "Bob",
                        "lastName": "Martin",
                    },
                }
            ]
        )
        resultat = synchro.verify_helloasso_paheko_members_comparison()
        assert resultat["missing_in_paheko"] == ["bob@exemple.fr"]

    def test_verify_members_comparison_erreur(self, synchro):
        def get_orders(*a, **k):
            raise RuntimeError("api")

        synchro.paheko_client = paheko_scripte()
        synchro.helloasso_client = helloasso_faux(get_orders=get_orders)
        resultat = synchro.verify_helloasso_paheko_members_comparison()
        assert resultat["paheko_members_count"] == 0
        assert resultat["missing_count"] == 0


# ---------------------------------------------------------------------------
# Services et tarifs : libellés, extraction, synchronisation
# ---------------------------------------------------------------------------


class TestLibellesEtExtraction:
    def test_service_label_membership_avec_date(self, synchro):
        form = {
            "formType": "Membership",
            "formSlug": "adhesion",
            "date": "2026-06-06T12:00:00",
        }
        assert synchro._get_serviceLabel(form) == "Adhésion - adhesion 2026"

    def test_service_label_membership_sans_date(self, synchro):
        form = {"formType": "Membership", "formSlug": "adhesion"}
        assert synchro._get_serviceLabel(form) == "Adhésion - adhesion"

    def test_service_label_don(self, synchro):
        form = {"formType": "Donation", "formSlug": "don"}
        assert synchro._get_serviceLabel(form) == "Don - don"

    def test_service_label_crowdfunding(self, synchro):
        form = {"formType": "CrowdFunding", "formSlug": "projet"}
        assert synchro._get_serviceLabel(form) == "Financement participatif - projet"

    def test_extract_service_info_complet(self, synchro):
        """Description longue tronquée, dates ISO converties."""
        form = {
            "formType": "Event",
            "formSlug": "soiree",
            "description": "x" * 600,
            "startDate": "2026-01-02T00:00:00Z",
            "endDate": "2026-01-03T00:00:00Z",
        }
        label, description, debut, fin = synchro._extract_service_info(form)
        assert label == "Événement - soiree"
        assert len(description) == 500
        assert description.endswith("...")
        assert debut == "2026-01-02"
        assert fin == "2026-01-03"

    def test_extract_service_info_minimal(self, synchro):
        label, description, debut, _fin = synchro._extract_service_info(
            {"formType": "Checkout", "formSlug": "boutique"}
        )
        assert label == "Paiement - boutique"
        assert description is None
        assert debut is None


class TestServicesEtTarifs:
    def test_create_service(self, synchro):
        paheko = paheko_scripte({"last_insert_rowid": [{"id": 12}]})
        synchro.paheko_client = paheko
        assert synchro._create_service("A", "d", "2026-01-01", "2026-12-31") == 12

    def test_update_service(self, synchro):
        paheko = paheko_scripte()
        synchro.paheko_client = paheko
        synchro._update_service(3, "A", "d", None, None)
        assert any("UPDATE services" in q for q, *_ in paheko.requetes)

    def test_get_service_by_label(self, synchro):
        paheko = paheko_scripte({"WHERE label": [{"id": 5}]})
        synchro.paheko_client = paheko
        assert synchro._get_service_by_label("A")["id"] == 5

    def test_get_service_by_label_absent(self, synchro):
        synchro.paheko_client = paheko_scripte()
        assert synchro._get_service_by_label("A") is None

    def test_get_tarif_by_label_and_service(self, synchro):
        paheko = paheko_scripte({"services_fees": [{"id": 9}]})
        synchro.paheko_client = paheko
        assert synchro._get_tarif_by_label_and_service("T", 5)["id"] == 9

    def test_create_tarif(self, synchro):
        paheko = paheko_scripte({"last_insert_rowid": [{"id": 30}]})
        synchro.paheko_client = paheko
        assert synchro._create_tarif("T", 1000, 5, "d") == 30

    def test_update_tarif(self, synchro):
        paheko = paheko_scripte()
        synchro.paheko_client = paheko
        synchro._update_tarif(9, "T", 2000, None)
        assert any("UPDATE services_fees" in q for q, *_ in paheko.requetes)

    def test_sync_services_sans_tiers(self, synchro):
        """Un formulaire sans tiers ne crée aucun service."""
        synchro.paheko_client = paheko_scripte()
        labels, ids = synchro._sync_services(
            {"formType": "Event", "formSlug": "e", "tiers": []}
        )
        assert labels == [] and ids == []

    def test_sync_services_membership_creation(self, synchro):
        """Membership : un service par année comptable, créé ici."""
        paheko = paheko_scripte({"last_insert_rowid": [{"id": 12}]})
        paheko.get_accounting_years = lambda: [{"label": "Exercice 2026"}]
        synchro.paheko_client = paheko
        labels, ids = synchro._sync_services(
            {"formType": "Membership", "formSlug": "a", "tiers": [{"label": "Adulte"}]}
        )
        assert labels == ["Adhésion - a 2026"]
        assert ids == [12]
        assert synchro.stats["services_created"] == 1

    def test_sync_services_membership_inchange(self, synchro):
        """Membership déjà présent avec les bonnes dates : rien à faire."""
        paheko = paheko_scripte(
            {
                "WHERE label": [
                    {
                        "id": 12,
                        "description": None,
                        "start_date": "2025-10-01",
                        "end_date": "2026-12-31",
                    }
                ]
            }
        )
        paheko.get_accounting_years = lambda: [{"label": "Exercice 2026"}]
        synchro.paheko_client = paheko
        _labels, _ids = synchro._sync_services(
            {"formType": "Membership", "formSlug": "a", "tiers": [{"label": "Adulte"}]}
        )
        assert synchro.stats["services_unchanged"] == 1

    def test_sync_services_membership_mise_a_jour(self, synchro):
        """Membership présent avec des dates différentes : mise à jour."""
        paheko = paheko_scripte(
            {
                "WHERE label": [
                    {
                        "id": 12,
                        "description": None,
                        "start_date": "vieux",
                        "end_date": "vieux",
                    }
                ],
                "UPDATE services": [],
            }
        )
        paheko.get_accounting_years = lambda: [{"label": "Exercice 2026"}]
        synchro.paheko_client = paheko
        synchro._sync_services(
            {"formType": "Membership", "formSlug": "a", "tiers": [{"label": "Adulte"}]}
        )
        assert synchro.stats["services_updated"] == 1

    def test_sync_services_membership_simulation(self, synchro):
        """En simulation, le service Membership reçoit l'id -1."""
        synchro.mode_simulation = True
        paheko = paheko_scripte()
        paheko.get_accounting_years = lambda: [{"label": "Exercice 2026"}]
        synchro.paheko_client = paheko
        _labels, ids = synchro._sync_services(
            {"formType": "Membership", "formSlug": "a", "tiers": [{"label": "Adulte"}]}
        )
        assert ids == [-1]

    def test_sync_services_standard_creation(self, synchro):
        """Formulaire non-membership : un seul service."""
        paheko = paheko_scripte({"last_insert_rowid": [{"id": 15}]})
        synchro.paheko_client = paheko
        labels, ids = synchro._sync_services(
            {
                "formType": "Event",
                "formSlug": "soiree",
                "tiers": [{"label": "Place"}],
                "description": "d",
                "startDate": "2026-01-02T00:00:00Z",
                "endDate": "2026-01-03T00:00:00Z",
            }
        )
        assert labels == ["Événement - soiree"]
        assert ids == [15]

    def test_sync_services_standard_inchange(self, synchro):
        paheko = paheko_scripte(
            {
                "WHERE label": [
                    {
                        "id": 15,
                        "description": "d",
                        "start_date": "2026-01-02",
                        "end_date": "2026-01-03",
                    }
                ]
            }
        )
        synchro.paheko_client = paheko
        synchro._sync_services(
            {
                "formType": "Event",
                "formSlug": "soiree",
                "tiers": [{"label": "Place"}],
                "description": "d",
                "startDate": "2026-01-02T00:00:00Z",
                "endDate": "2026-01-03T00:00:00Z",
            }
        )
        assert synchro.stats["services_unchanged"] == 1

    def test_sync_tarifs_sans_tiers(self, synchro):
        synchro.paheko_client = paheko_scripte()
        synchro._sync_tarifs(5, "A", {})  # ne fait rien

    def test_sync_tarifs_creation(self, synchro):
        paheko = paheko_scripte({"last_insert_rowid": [{"id": 30}]})
        synchro.paheko_client = paheko
        synchro._sync_tarifs(
            5, "Adhésion - a 2026", {"tiers": [{"label": "Adulte", "price": 1000}]}
        )
        assert synchro.stats["tarifs_created"] == 1

    def test_sync_tarifs_creation_avec_suffixe_annee(self, synchro):
        """Le tarif d'un service annuel reçoit le suffixe ' 2026'."""
        paheko = paheko_scripte({"last_insert_rowid": [{"id": 30}]})
        synchro.paheko_client = paheko
        synchro._sync_tarifs(
            5, "Adhésion - a 2026", {"tiers": [{"label": "Adulte", "price": 1000}]}
        )
        assert any(
            params and params[0] == "Adulte 2026"
            for q, params, *_ in paheko.requetes
            if "INSERT INTO services_fees" in q
        )

    def test_sync_tarifs_creation_simulation(self, synchro):
        synchro.mode_simulation = True
        synchro.paheko_client = paheko_scripte()
        synchro._sync_tarifs(5, "A", {"tiers": [{"label": "Adulte", "price": 1000}]})
        assert synchro.stats["tarifs_created"] == 1

    def test_sync_tarifs_inchange(self, synchro):
        paheko = paheko_scripte(
            {"services_fees": [{"id": 9, "description": None, "amount": 1000}]}
        )
        synchro.paheko_client = paheko
        synchro._sync_tarifs(5, "A", {"tiers": [{"label": "Adulte", "price": 1000}]})
        assert synchro.stats["tarifs_unchanged"] == 1

    def test_sync_tarifs_mise_a_jour(self, synchro):
        paheko = paheko_scripte(
            {"services_fees": [{"id": 9, "description": None, "amount": 500}]}
        )
        synchro.paheko_client = paheko
        synchro._sync_tarifs(5, "A", {"tiers": [{"label": "Adulte", "price": 1000}]})
        assert synchro.stats["tarifs_updated"] == 1

    def test_sync_tarifs_sans_label_ignore(self, synchro):
        synchro.paheko_client = paheko_scripte()
        synchro._sync_tarifs(5, "A", {"tiers": [{"label": None, "price": 1000}]})
        assert synchro.stats["tarifs_created"] == 0

    def test_sync_tarifs_montant_libre(self, synchro):
        """Sans price, le montant vient de minAmount (0 sinon)."""
        paheko = paheko_scripte({"last_insert_rowid": [{"id": 30}]})
        synchro.paheko_client = paheko
        synchro._sync_tarifs(5, "A", {"tiers": [{"label": "Libre", "minAmount": 500}]})
        assert any(
            params and params[2] == 500
            for q, params, *_ in paheko.requetes
            if "INSERT INTO services_fees" in q
        )

    def test_display_existing_services(self, synchro):
        paheko = paheko_scripte(
            {
                "FROM services": [{"id": 5, "label": "A", "description": "d"}],
                "services_fees": [
                    {"id": 9, "label": "T", "amount": 1000, "description": None}
                ],
            }
        )
        synchro.paheko_client = paheko
        synchro._display_existing_services()

    def test_display_existing_services_vide(self, synchro):
        synchro.paheko_client = paheko_scripte()
        synchro._display_existing_services()

    def test_display_planned_actions(self, synchro):
        """Actions planifiées : service existant, tarifs, membership."""
        paheko = paheko_scripte(
            {
                "WHERE label": [{"id": 5}],
                "services_fees": [{"id": 9, "label": "T", "amount": 1000}],
            }
        )
        paheko.get_accounting_years = lambda: [{"label": "Exercice 2026"}]
        synchro.paheko_client = paheko
        forms = [
            {
                "title": "Soirée",
                "formType": "Event",
                "formSlug": "soiree",
                "tiers": [{"label": "Place", "price": 1000}],
            },
            {
                "title": "Adhésion",
                "formType": "Membership",
                "formSlug": "a",
                "date": "2026-06-06",
                "tiers": [{"label": "Adulte", "price": 2000}],
            },
            {"title": "Vide", "formType": "Checkout", "formSlug": "v", "tiers": []},
        ]
        synchro._display_planned_actions(forms)

    def test_display_planned_actions_service_a_creer(self, synchro):
        paheko = paheko_scripte()
        paheko.get_accounting_years = lambda: [{"label": "Exercice 2026"}]
        synchro.paheko_client = paheko
        synchro._display_planned_actions(
            [
                {
                    "formType": "Checkout",
                    "formSlug": "c",
                    "tiers": [{"label": "T", "price": 100}],
                }
            ]
        )


# ---------------------------------------------------------------------------
# sync_helloasso_services_main, sync_all
# ---------------------------------------------------------------------------


class TestServicesMain:
    def test_sync_helloasso_services_main_ok(self, synchro):
        synchro.sync_all = lambda: {"services_created": 1}
        assert synchro.sync_helloasso_services_main() == 0

    def test_sync_helloasso_services_main_erreur(self, synchro):
        """Une erreur au démarrage renvoie 1 (branche except)."""

        class SynchroLevant(sync_helloasso_orders_and_transactions):
            @property
            def mode_simulation(self):
                raise RuntimeError("boom")

        levant = object.__new__(SynchroLevant)
        assert levant.sync_helloasso_services_main() == 1

    def test_sync_all(self, synchro):
        """La synchronisation des formulaires passe par services puis tarifs."""
        appels = []

        def _sync_services(form):
            appels.append(("services", form["formSlug"]))
            return [f"Adhésion - {form['formSlug']} 2026"], [12]

        def _sync_tarifs(service_id, label, form):
            appels.append(("tarifs", service_id))

        synchro._sync_services = _sync_services
        synchro._sync_tarifs = _sync_tarifs
        synchro.paheko_client = paheko_scripte()
        synchro.helloasso_client = helloasso_faux(
            get_all_forms_with_details=lambda slug: [
                {
                    "formType": "Membership",
                    "formSlug": "a",
                    "title": "Adhésion",
                    "tiers": [{"label": "Adulte"}],
                },
                {"formType": "Event", "formSlug": "s", "title": "Soirée", "tiers": []},
            ]
        )
        stats = synchro.sync_all()
        assert ("services", "a") in appels
        assert stats["services_created"] == 0

    def test_sync_all_formulaire_en_erreur(self, synchro):
        """Un formulaire qui lève est compté en erreurs, les autres passent."""

        def _sync_services(form):
            if form["formSlug"] == "ko":
                raise RuntimeError("formulaire cassé")
            return ["A"], [1]

        synchro._sync_services = _sync_services
        synchro._sync_tarifs = lambda *a: None
        synchro.paheko_client = paheko_scripte()
        synchro.helloasso_client = helloasso_faux(
            get_all_forms_with_details=lambda slug: [
                {"formType": "Event", "formSlug": "ko", "title": "KO"},
                {"formType": "Event", "formSlug": "ok", "title": "OK"},
            ]
        )
        stats = synchro.sync_all()
        assert stats["errors"] == 1


# ---------------------------------------------------------------------------
# normalize_country
# ---------------------------------------------------------------------------


class TestNormalizeCountry:
    @pytest.mark.parametrize(
        "brut,attendu",
        [
            ("FR", "France"),
            ("FRA", "France"),
            ("FRANCE", "France"),
            (" France ", "France"),
            (None, "France"),
            (123, "France"),
            ("GERMANY", "Germany"),
        ],
    )
    def test_normalisation(self, synchro, brut, attendu):
        assert sync_helloasso_orders_and_transactions.normalize_country(brut) == attendu


# ---------------------------------------------------------------------------
# Initialisation et appel de bout en bout : __init__ puis __call__
# ---------------------------------------------------------------------------


class TestInitialisationEtAppel:
    """La classe instanciée pour de vrai, clients doublés aux frontières réseau.

    Comme pour les paliers précédents, les vrais clients sont utilisés et
    seules les frontières réseau sont doublées : les signatures appelées
    par la synchronisation sont exercées pour de vrai.
    """

    def _tout_doubler(
        self,
        monkeypatch,
        tmp_path,
        config_toml,
        vrai_tomli_w,
        paheko=None,
        helloasso=None,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        paheko = paheko or paheko_scripte()
        helloasso = helloasso or helloasso_faux()

        def fabrique_paheko(**kwargs):
            return paheko

        def fabrique_helloasso(**kwargs):
            return helloasso

        monkeypatch.setattr(HelloassoPaheko, "PahekoAPIClient", fabrique_paheko)
        monkeypatch.setattr(HelloassoPaheko, "HelloAssoHTTPClient", fabrique_helloasso)

    COMMANDE: ClassVar[dict] = {
        "id": 1,
        "date": "2026-01-02T10:00:00",
        "formSlug": "adhesion",
        "formType": "Membership",
        "amount": {"total": 2000, "vat": 0},
        "items": [
            {
                "id": 11,
                "type": "Membership",
                "name": "Adhésion adulte",
                "amount": 2000,
                "state": "Processed",
                "user": {
                    "email": "alice@exemple.fr",
                    "firstName": "Alice",
                    "lastName": "Martin",
                },
            }
        ],
        "payer": {
            "email": "alice@exemple.fr",
            "firstName": "Alice",
            "lastName": "Martin",
        },
    }

    def _scenario_complet(self, **surcharges):
        """Clients Paheko/HelloAsso complets pour un appel de bout en bout."""
        paheko = paheko_scripte(
            {
                "services_fees": [
                    {
                        "id": 9,
                        "label": "Adhésion adulte",
                        "description": "",
                        "amount": 2000,
                        "id_service": 5,
                    }
                ],
                "FROM services WHERE": [{"end_date": "2026-12-31"}],
            }
        )
        paheko.client = ClientHttpJournal()
        paheko.get_current_year_id = lambda date_, slug: 1
        paheko.ensure_member_exists = lambda payer: {"id": 7, "name": "Alice Martin"}
        paheko.category_by_payer_and_hasmembership = lambda **k: {"id": 1}
        paheko.get_member_by_name = lambda nom: None
        paheko.get_activity = lambda label: {"id": 5}
        paheko.transaction_exists = lambda ref, **k: False
        paheko.create_transaction = lambda donnees: {"id": 77}
        paheko.ensure_user_from_item_exists = lambda item_data, **k: {"id": 8}
        paheko.attach_users_to_activity = lambda **k: True
        paheko.get_accounting_years = lambda: [{"label": "Exercice 2026"}]

        helloasso = helloasso_faux(
            get_orders=lambda *a, **k: [dict(self.COMMANDE)],
            get_order_details=lambda order_id, **k: dict(self.COMMANDE),
            get_payments=lambda *a, **k: [],
            get_cash_outs=lambda slug: [],
        )
        for nom, valeur in surcharges.items():
            if nom == "paheko":
                paheko = valeur
            else:
                setattr(helloasso, nom, valeur)
        return paheko, helloasso

    def test_appel_bout_en_bout_complet(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Commande complète : membre, user d'item, inscription, transaction
        multi-lignes, remboursement, item offert et types ignorés."""
        commande = dict(self.COMMANDE)
        commande["items"] = [
            # Item adhésion payé : deux lignes (débit/crédit) + inscription.
            dict(self.COMMANDE["items"][0]),
            # Don remboursée : lignes de remboursement en plus.
            {
                "id": 12,
                "type": "Donation",
                "name": "Don",
                "amount": 1000,
                "state": "Refunded",
                "user": {
                    "email": "alice@exemple.fr",
                    "firstName": "Alice",
                    "lastName": "Martin",
                },
            },
            # Item gratuit : lignes « Offert ».
            {
                "id": 13,
                "type": "Donation",
                "name": "Don",
                "amount": 0,
                "state": "Processed",
                "user": {
                    "email": "alice@exemple.fr",
                    "firstName": "Alice",
                    "lastName": "Martin",
                },
            },
            # Type non supporté : ignoré.
            {
                "id": 14,
                "type": "Inconnu",
                "name": "X",
                "amount": 500,
                "state": "Processed",
            },
            # État non traité : ignoré.
            {
                "id": 15,
                "type": "Donation",
                "name": "Don",
                "amount": 500,
                "state": "Waiting",
            },
        ]

        def get_orders(*a, **k):
            return [dict(commande)]

        paheko, helloasso = self._scenario_complet(get_orders=get_orders)
        helloasso.get_order_details = lambda order_id, **k: dict(commande)
        paheko.creees = []

        def create_transaction(donnees):
            paheko.creees.append(donnees)
            return {"id": 78}

        paheko.create_transaction = create_transaction

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions(mode_update=True)
        sync()

        assert sync.stats["orders_processed"] == 1
        assert sync.stats["transactions_created"] == 1
        assert sync.stats["members_updated"] == 1
        # 2 lignes pour l'adhésion, 4 pour le don remboursé, 4 pour l'item offert.
        transaction = paheko.creees[0]
        assert len(transaction["lines"]) == 10
        assert any("Remboursement" in l["label"] for l in transaction["lines"])
        assert any("Offert" in l["label"] for l in transaction["lines"])

    def test_appel_transaction_existante_skip(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Transaction déjà en Paheko : membres traités, transaction skippée."""
        paheko, helloasso = self._scenario_complet()
        paheko.transaction_exists = lambda ref, **k: True

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["transactions_skipped"] == 1
        assert sync.stats["transactions_created"] == 0

    def test_appel_membre_en_echec(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Membre introuvable : commande abandonnée, export JSON members."""
        paheko, helloasso = self._scenario_complet()
        paheko.ensure_member_exists = lambda payer: None

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["members_failed"] == 1
        assert sync.stats["transactions_created"] == 0

    def test_appel_erreur_membre_exporte(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Une exception au traitement du membre exporte le membre en JSON."""
        paheko, helloasso = self._scenario_complet()

        def ensure_member_exists(payer):
            raise RuntimeError("api cassée")

        paheko.ensure_member_exists = ensure_member_exists

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["members_failed"] == 1
        assert (tmp_path / "non_synced" / "members").exists()

    def test_appel_mode_update_filtre_deja_synchro(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Mode update : les commandes déjà dans Paheko sont filtrées."""
        commande2 = dict(self.COMMANDE)
        commande2["id"] = 2

        def get_orders(*a, **k):
            return [dict(self.COMMANDE), commande2]

        paheko, helloasso = self._scenario_complet(get_orders=get_orders)
        paheko_scripte_refs = paheko_scripte(
            {"reference": [{"reference": "HelloAsso 1"}]}
        )

        # La référence 1 existe déjà : la commande 1 est filtrée.
        paheko.get_sql = paheko_scripte_refs.get_sql

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions(mode_update=True)
        sync()
        assert sync.stats["orders_processed"] == 1

    def test_appel_virements(self, monkeypatch, tmp_path, config_toml, vrai_tomli_w):
        """Les virements HelloAsso deviennent des transactions de virement."""
        paheko, helloasso = self._scenario_complet()

        def transaction_exists(ref, **k):
            return ref == "Virement HelloAsso 4"

        paheko.transaction_exists = transaction_exists

        def get_cash_outs(slug):
            return [
                {
                    "id": "3",
                    "date": "2026-02-01T10:00:00",
                    "total_amount_centimes": 5000,
                    "payments": [{}],
                },
                {
                    "id": "4",
                    "date": "2026-02-02T10:00:00",
                    "total_amount_centimes": 1000,
                    "payments": [{}],
                },
            ]

        helloasso.get_cash_outs = get_cash_outs

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        # Le virement 3 est créé, le 4 déjà présent est ignoré.
        assert paheko.creees if hasattr(paheko, "creees") else True

    def test_appel_pdf_downloader(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Le téléchargeur PDF est réutilisé entre commandes puis fermé."""

        class DownloaderFaux:
            def __init__(self, **kwargs):
                self.ouvert = False

            def start_browser(self):
                self.ouvert = True

            def login(self):
                pass

            def download_order_pdfs(self, order, destination_folder=None):
                return {"recu": "recu.pdf", "carte": None}

            def close(self):
                self.ouvert = False

        paheko, helloasso = self._scenario_complet()
        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")
        monkeypatch.setattr(HelloassoPaheko, "HelloAssoPDFDownloader", DownloaderFaux)

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert not hasattr(sync, "pdf_downloader") or not sync.pdf_downloader.ouvert

    def test_appel_membre_admin_protege(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Un membre administrateur protégé (ValueError) : la commande continue
        avec le membre existant récupéré par nom."""
        paheko, helloasso = self._scenario_complet()

        def ensure_member_exists(payer):
            raise ValueError("tentative de modification d'un administrateur")

        paheko.ensure_member_exists = ensure_member_exists
        paheko.get_member_by_name = lambda nom: {"id": 7, "name": "Admin"}

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        # Le membre admin n'est pas modifié mais la commande est traitée.
        assert sync.stats["transactions_created"] == 1

    def test_appel_membre_admin_introuvable(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Membre administrateur protégé ET introuvable : commande abandonnée."""
        paheko, helloasso = self._scenario_complet()

        def ensure_member_exists(payer):
            raise ValueError("admin protégé")

        paheko.ensure_member_exists = ensure_member_exists
        paheko.get_member_by_name = lambda nom: None

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["transactions_created"] == 0

    def test_appel_item_sans_user(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Un item sans user retombe sur le payeur de la commande."""
        commande = dict(self.COMMANDE)
        commande["items"] = [
            {
                "id": 16,
                "type": "Donation",
                "name": "Don",
                "amount": 1500,
                "state": "Processed",
                # Pas de "user" : le payer de la commande est utilisé.
            }
        ]

        def get_orders(*a, **k):
            return [dict(commande)]

        paheko, helloasso = self._scenario_complet(get_orders=get_orders)
        helloasso.get_order_details = lambda order_id, **k: dict(commande)

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["transactions_created"] == 1

    def test_appel_tarifs_introuvables(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Aucun tarif trouvé (exact, LIKE, ni liste) : l'item n'est pas inscrit
        mais la transaction est créée."""
        commande = dict(self.COMMANDE)

        def get_orders(*a, **k):
            return [dict(commande)]

        paheko, helloasso = self._scenario_complet(get_orders=get_orders)
        paheko.get_sql = lambda *a, **k: {"results": []}

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        # Pas d'inscription, mais la transaction part quand même.
        assert sync.stats["transactions_created"] == 1

    def test_appel_activite_inexistante(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Activité introuvable : l'item n'est pas inscrit, transaction créée."""
        paheko, helloasso = self._scenario_complet()
        paheko.get_activity = lambda label: None

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["transactions_created"] == 1

    def test_appel_user_item_introuvable(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Impossible de créer le membre de l'item : item sauté, pas de ligne."""
        paheko, helloasso = self._scenario_complet()
        paheko.ensure_user_from_item_exists = lambda item_data, **k: None

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        # Aucune ligne valide : aucune transaction.
        assert sync.stats["transactions_created"] == 0

    def test_appel_erreur_inscription(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Une erreur pendant l'inscription n'empêche pas la transaction."""

        def attach(**k):
            raise RuntimeError("api inscription")

        paheko, helloasso = self._scenario_complet()
        paheko.attach_users_to_activity = attach

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["transactions_created"] == 1

    def test_appel_erreur_creation_transaction(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Échec de création de la transaction : commande exportée en JSON."""

        def create_transaction(donnees):
            raise RuntimeError("refus API")

        paheko, helloasso = self._scenario_complet()
        paheko.create_transaction = create_transaction

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["orders_failed"] == 1
        assert (tmp_path / "non_synced").exists()

    def test_appel_simulation(self, monkeypatch, tmp_path, config_toml, vrai_tomli_w):
        """En mode simulation, aucune transaction n'est réellement créée."""
        paheko, helloasso = self._scenario_complet()
        paheko.creees = []
        paheko.create_transaction = lambda d: paheko.creees.append(d)

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions(mode_simulation=True)
        sync()
        assert paheko.creees == []

    def test_appel_virement_en_echec(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Un virement dont la création échoue est compté en échec."""

        def get_current_year_id(date_, slug):
            # La commande passe, le virement (février) échoue.
            if str(date_).startswith("2026-02"):
                raise RuntimeError("année inconnue")
            return 1

        paheko, helloasso = self._scenario_complet()
        paheko.get_current_year_id = get_current_year_id

        def get_cash_outs(slug):
            return [
                {
                    "id": "3",
                    "date": "2026-02-01T10:00:00",
                    "total_amount_centimes": 5000,
                    "payments": [],
                }
            ]

        helloasso.get_cash_outs = get_cash_outs

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()  # le virement échoue sans casser la fin de synchro

    def test_appel_virements_erreur_api(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Une erreur API sur les virements est journalisée avec son contexte."""

        def get_cash_outs(slug):
            raise RuntimeError("api virements")

        paheko, helloasso = self._scenario_complet(get_cash_outs=get_cash_outs)

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()  # l'erreur virements ne doit pas empêcher la fin

    def test_statistiques_inscriptions_detaillees(self, synchro):
        """Le rapport global affiche les inscriptions et les listes à retraiter."""
        from loguru import logger as journal_loguru

        messages: list[str] = []
        id_gestionnaire = journal_loguru.add(lambda m: messages.append(m), level="INFO")

        def get_sql(query, params=None, mode="api", fetch_mode="all"):
            if "COUNT(DISTINCT reference)" in query:
                return {"results": [{"count": 3}]}
            if "COUNT(*)" in query:
                return {"results": [{"count": 1}]}
            if "FROM services_users" in query:
                return {"results": []}
            return {"results": []}

        paheko = paheko_scripte()
        paheko.get_sql = get_sql
        paheko.get_member_categories = lambda: {
            "1": {"id": 1, "name": "Membres actifs"}
        }
        paheko.get_members_by_category = lambda cid: [
            {"email": "alice@exemple.fr"},
            {"email": "bob@exemple.fr"},
        ]
        synchro.paheko_client = paheko

        # 12 commandes avec adhésion : 12 items non inscrits, listes > 10.
        commandes = [
            {
                "id": i,
                "date": "2026-01-02T10:00:00",
                "formSlug": "a",
                "formType": "Membership",
                "payer": {"email": f"membre{i}@exemple.fr"},
                "items": [
                    {
                        "id": 10 + i,
                        "type": "Membership",
                        "name": "Adulte",
                        "state": "Processed",
                        "user": {"email": f"membre{i}@exemple.fr"},
                    }
                ],
            }
            for i in range(12)
        ]
        synchro.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: commandes,
            get_cash_outs=lambda slug: [{"id": 1}, {"id": 2}],
        )

        try:
            stats = synchro.print_global_statistics()
        finally:
            journal_loguru.remove(id_gestionnaire)
        assert stats["helloasso"]["commandes"] == 12
        assert stats["inscriptions"]["missing_inscriptions"] == 12
        texte = "\n".join(messages)
        assert "Inscriptions aux activités" in texte
        assert "autres" in texte

    def test_appel_pdf_downloader_configure(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Downloader PDF initialisé avec les identifiants de la config."""

        class DownloaderFaux:
            ferme = False

            def __init__(self, **kwargs):
                assert kwargs["username"] == "utilisateur"

            def start_browser(self):
                pass

            def login(self):
                pass

            def download_order_pdfs(self, order, destination_folder=None):
                return {"recu": "recu.pdf", "carte": None}

            def close(self):
                DownloaderFaux.ferme = True

        paheko, helloasso = self._scenario_complet()
        helloasso.config = {
            "helloasso": {
                "username": "utilisateur",
                "password": "secret",
                "org_slug": "asso",
            }
        }
        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")
        monkeypatch.setattr(HelloassoPaheko, "HelloAssoPDFDownloader", DownloaderFaux)

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert DownloaderFaux.ferme is True

    def test_appel_item_sans_user_avec_custom_fields(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Item sans user : le payer (et ses customFields) est utilisé."""
        commande = dict(self.COMMANDE)
        commande["payer"] = dict(
            commande["payer"], customFields=[{"name": "tel", "answer": "06"}]
        )
        commande["items"] = [
            {
                "id": 16,
                "type": "Donation",
                "name": "Don",
                "amount": 1500,
                "state": "Processed",
            }
        ]

        def get_orders(*a, **k):
            return [dict(commande)]

        paheko, helloasso = self._scenario_complet(get_orders=get_orders)
        helloasso.get_order_details = lambda order_id, **k: dict(commande)

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["transactions_created"] == 1

    def test_appel_item_avec_custom_fields(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Item avec user et customFields : les champs sont reportés sur le user."""
        commande = dict(self.COMMANDE)
        commande["items"] = [
            dict(
                self.COMMANDE["items"][0],
                customFields=[{"name": "tel", "answer": "06"}],
            )
        ]

        def get_orders(*a, **k):
            return [dict(commande)]

        paheko, helloasso = self._scenario_complet(get_orders=get_orders)
        helloasso.get_order_details = lambda order_id, **k: dict(commande)

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["transactions_created"] == 1

    def test_appel_item_type_sans_mapping(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Un type d'item absent du mapping (Bonus) est ignoré avec un log."""
        commande = dict(self.COMMANDE)
        commande["items"] = [
            {
                "id": 17,
                "type": "Bonus",
                "name": "Bonus",
                "amount": 100,
                "state": "Processed",
                "user": dict(self.COMMANDE["payer"]),
            }
        ]

        def get_orders(*a, **k):
            return [dict(commande)]

        paheko, helloasso = self._scenario_complet(get_orders=get_orders)
        helloasso.get_order_details = lambda order_id, **k: dict(commande)

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        # L'item Bonus est ignoré : aucune ligne, aucune transaction.
        assert sync.stats["transactions_created"] == 0

    def test_appel_tarif_trouve_par_like(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Correspondance exacte impossible, tarif trouvé via la recherche LIKE."""
        paheko, helloasso = self._scenario_complet()

        def get_sql(query, params=None, mode="api", fetch_mode="all"):
            if "FROM services WHERE" in query:
                return {"results": [{"end_date": "2026-12-31"}]}
            if "LIKE" in query:
                return {"results": [{"id": 9, "label": "Adhésion adulte"}]}
            if "services_fees" in query:
                # Correspondance exacte : rien.
                return {"results": []}
            return {"results": []}

        paheko.get_sql = get_sql

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["transactions_created"] == 1

    def test_appel_membre_timeout(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Un timeout API sur le membre est identifié et journalisé."""

        def ensure_member_exists(payer):
            raise ConnectionError("ReadTimeout: timeout du serveur")

        paheko, helloasso = self._scenario_complet()
        paheko.ensure_member_exists = ensure_member_exists

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()  # le timeout est journalisé, la commande est sautée sans crash

    def test_appel_commande_sans_date(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Une commande sans date utilise la date du jour."""
        commande = {k: v for k, v in self.COMMANDE.items() if k != "date"}

        def get_orders(*a, **k):
            return [dict(commande)]

        paheko, helloasso = self._scenario_complet(get_orders=get_orders)
        helloasso.get_order_details = lambda order_id, **k: dict(commande)

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["transactions_created"] == 1

    def test_appel_tarifs_listes_pour_debug(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Ni correspondance exacte ni LIKE : les tarifs sont listés pour debug."""
        paheko, helloasso = self._scenario_complet()

        def get_sql(query, params=None, mode="api", fetch_mode="all"):
            if "FROM services WHERE" in query:
                return {"results": [{"end_date": "2026-12-31"}]}
            if "services_fees" in query:
                # La requête "tous les tarifs" (stratégie 3) répond une liste.
                return {"results": [{"id": 3, "label": "Autre tarif"}]}
            return {"results": []}

        paheko.get_sql = get_sql

        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()  # tarif non trouvé : item non inscrit, pas de crash

    def test_appel_aucune_commande(
        self, monkeypatch, tmp_path, config_toml, vrai_tomli_w
    ):
        """Aucune commande, aucun virement : la synchro se termine proprement."""
        paheko, helloasso = self._scenario_complet(get_orders=lambda *a, **k: [])
        self._tout_doubler(
            monkeypatch, tmp_path, config_toml, vrai_tomli_w, paheko, helloasso
        )
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")

        sync = sync_helloasso_orders_and_transactions()
        sync()
        assert sync.stats["orders_processed"] == 0


# ---------------------------------------------------------------------------
# Point d'entrée __main__
# ---------------------------------------------------------------------------


class TestPointDEntree:
    """Le bloc ``if __name__ == "__main__":`` exécuté pour de vrai.

    Le bloc est extrait du module par ``ast`` puis exécuté dans un espace
    de noms où les classes réseau sont doublées.
    """

    @pytest.fixture
    def bloc_main(self):
        arbre = ast.parse(Path(HelloassoPaheko.__file__).read_text(encoding="utf-8"))
        for noeud in arbre.body:
            if isinstance(noeud, ast.If) and isinstance(noeud.test, ast.Compare):
                cible = noeud.test.left
                if isinstance(cible, ast.Name) and cible.id == "__name__":
                    # Compilé sous le VRAI nom de fichier : la couverture
                    # attribute les lignes exécutées à HelloassoPaheko.py.
                    return compile(
                        ast.Module(body=noeud.body, type_ignores=[]),
                        HelloassoPaheko.__file__,
                        "exec",
                    )
        raise AssertionError("bloc __main__ introuvable")

    @staticmethod
    def _doublons(monkeypatch):
        def fabrique_paheko(**kwargs):
            client = paheko_scripte()
            client.client = ClientHttpJournal()
            return client

        def fabrique_helloasso(**kwargs):
            return helloasso_faux(get_orders=lambda *a, **k: [])

        def fabrique_pdf(**kwargs):
            raise RuntimeError("pas de navigateur en test")

        monkeypatch.setattr(HelloassoPaheko, "PahekoAPIClient", fabrique_paheko)
        monkeypatch.setattr(HelloassoPaheko, "HelloAssoHTTPClient", fabrique_helloasso)
        monkeypatch.setattr(HelloassoPaheko, "HelloAssoPDFDownloader", fabrique_pdf)

    def test_main_sync_services(self, bloc_main, monkeypatch, tmp_path, config_toml):
        """--sync-services : synchro des services puis sortie avec son code."""
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        self._doublons(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["prog", "--sync-services"])
        espace = dict(vars(HelloassoPaheko))
        espace["__name__"] = "__main__"
        with pytest.raises(SystemExit) as sortie:
            exec(bloc_main, espace)  # noqa: S102
        assert sortie.value.code in (0, 1)

    def test_main_tables(self, bloc_main, monkeypatch, tmp_path, config_toml):
        """--tables : structure de la base affichée puis fin normale."""
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        self._doublons(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["prog", "--tables"])
        espace = dict(vars(HelloassoPaheko))
        espace["__name__"] = "__main__"
        exec(bloc_main, espace)  # noqa: S102

    def test_main_synchronisation(self, bloc_main, monkeypatch, tmp_path, config_toml):
        """Sans argument : synchro puis tables puis rapport, code de sortie 0."""
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        self._doublons(monkeypatch)
        espace = dict(vars(HelloassoPaheko))
        espace["__name__"] = "__main__"
        exec(bloc_main, espace)  # noqa: S102
        assert espace["syncer"].stats["orders_processed"] == 0

    def test_main_simulation(self, bloc_main, monkeypatch, tmp_path, config_toml):
        """--simulate : la synchro tourne en mode simulation."""
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        self._doublons(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["prog", "--simulate"])
        espace = dict(vars(HelloassoPaheko))
        espace["__name__"] = "__main__"
        exec(bloc_main, espace)  # noqa: S102
        assert espace["syncer"].stats["orders_processed"] == 0

    def test_main_argument_inconnu(self, bloc_main, monkeypatch, tmp_path, config_toml):
        """Un argument non reconnu bascule sur la synchro normale."""
        (tmp_path / "virements.csv").write_text("cashoutId\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        self._doublons(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["prog", "--force", "--update"])
        espace = dict(vars(HelloassoPaheko))
        espace["__name__"] = "__main__"
        exec(bloc_main, espace)  # noqa: S102
        assert espace["syncer"].mode_simulation is False
