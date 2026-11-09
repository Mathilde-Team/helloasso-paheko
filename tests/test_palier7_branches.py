# SPDX-License-Identifier: GPL-3.0-or-later
"""Palier 7 : cas limites des données, références, services et statistiques (#5)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from test_palier2_clients import fabrique_client_paheko
from test_palier4_paheko import playwright_faux
from test_palier5_synchro import (
    config_toml,
    fabrique_synchro,
    helloasso_faux,
    paheko_scripte,
    synchro,
)

from HelloassoPaheko import HelloAssoPDFDownloader, PahekoAPIClient, logger


def test_champs_personnalises_conservent_premiere_valeur():
    champs = [
        ("email", "premier@example.invalid"),
        ("email", "second@example.invalid"),
        ("email (par mail)", "texte"),
        ("profession", "Métier"),
        ("profession liste publique des professionnels", "Autre"),
        ("réseau", "Premier"),
        ("réseau", "Second"),
        ("rue", "Adresse initiale"),
        ("rue", "Autre adresse"),
        ("code postal", "25000"),
        ("code postal", "75000"),
        ("pays", "France"),
        ("pays", "Belgique"),
    ]
    resultat = PahekoAPIClient.extract_all_from_custom_fields(
        {"customFields": [{"name": nom, "answer": valeur} for nom, valeur in champs]}
    )
    assert resultat["email"] == "premier@example.invalid"
    assert resultat["profession"] == "Métier"
    assert resultat["network"] == "Premier"
    assert resultat["adresse"] == "Adresse initiale"
    assert resultat["code_postal"] == "25000"
    assert resultat["country"] == "France"


def test_champs_paheko_reserves_ne_sont_pas_ecrases():
    client = fabrique_client_paheko()
    nouvelles = {"lettre_infos": 0, "numero": 12}
    besoin, donnees = client.compare_member_data(
        {"lettre_infos": 1, "numero": 10}, nouvelles.copy(), nouvelles.copy()
    )
    assert besoin is False
    assert donnees == nouvelles


@pytest.mark.parametrize("nombre", [0, 12])
def test_references_vides_et_listes_longues(synchro, nombre):
    synchro.paheko_client = paheko_scripte(
        {
            "reference": [{"reference": None}, {"reference": ""}]
            + [{"reference": f"HelloAsso {n}"} for n in range(nombre)]
        }
    )
    assert synchro.get_helloasso_references_from_paheko() == set(range(nombre))


def test_liste_longue_de_virements(synchro):
    synchro.paheko_client = paheko_scripte(
        {"reference": [{"reference": f"Virement HelloAsso {n}"} for n in range(12)]}
    )
    assert synchro.get_helloasso_cashouts_from_paheko() == set(range(12))


def test_affichage_services_sans_description_ni_tarif(synchro):
    synchro.paheko_client = paheko_scripte(
        {
            "FROM services ORDER": [{"id": 5, "label": "Sans description", "description": None}],
        }
    )
    synchro._display_existing_services()
    assert any("id_service = 5" in q for q, *_ in synchro.paheko_client.requetes)


def test_actions_planifiees_annee_absente_et_tarif_a_creer(synchro):
    synchro.paheko_client = paheko_scripte({"WHERE label": [{"id": 5}]})
    synchro.paheko_client.get_accounting_years = lambda: [{"label": "Sans année"}]
    synchro._display_planned_actions(
        [
            {"formType": "Membership", "formSlug": "adhesion", "tiers": [{"label": "A"}]},
            {"formType": "Event", "formSlug": "soiree", "tiers": [{"label": "Place"}]},
        ]
    )
    assert any("services_fees" in q for q, *_ in synchro.paheko_client.requetes)


def test_services_ignorent_exercices_sans_annee(synchro):
    synchro.paheko_client = paheko_scripte()
    synchro.paheko_client.get_accounting_years = lambda: [{"label": "Sans année"}]
    assert synchro._sync_services(
        {
            "formType": "Membership",
            "formSlug": "adhesion",
            "tiers": [{"label": "A"}],
        }
    ) == ([], [])


@pytest.mark.parametrize("simulation", [False, True])
def test_services_et_tarifs_description_longue(synchro, simulation):
    synchro.mode_simulation = simulation
    synchro.paheko_client = paheko_scripte(
        {
            "WHERE label": [
                {"id": 5, "description": "ancienne", "start_date": None, "end_date": None}
            ],
            "services_fees": [{"id": 8, "description": "ancienne", "amount": 100}],
        }
    )
    synchro.paheko_client.get_accounting_years = lambda: [{"label": "Exercice 2026"}]
    synchro._update_service = Mock()
    synchro._update_tarif = Mock()
    synchro._sync_services(
        {
            "formType": "Membership",
            "formSlug": "a",
            "description": "x" * 600,
            "tiers": [{"label": "A"}],
        }
    )
    synchro._sync_services(
        {
            "formType": "Event",
            "formSlug": "b",
            "description": "x" * 600,
            "tiers": [{"label": "B"}],
        }
    )
    synchro._sync_tarifs(
        5,
        "B",
        {
            "tiers": [
                {"label": "T", "price": 200, "description": "x" * 600},
            ]
        },
    )
    if simulation:
        synchro._update_service.assert_not_called()
        synchro._update_tarif.assert_not_called()
    else:
        assert synchro._update_service.call_count == 2
        synchro._update_tarif.assert_called_once()
        assert len(synchro._update_tarif.call_args.args[-1]) == 500


def test_creation_services_et_tarifs_simules_n_ecrivent_pas(synchro):
    synchro.mode_simulation = True
    synchro.paheko_client = paheko_scripte()
    synchro._create_service = Mock()
    synchro._create_tarif = Mock()
    _, ids = synchro._sync_services(
        {
            "formType": "Event",
            "formSlug": "a",
            "tiers": [{"label": "A"}],
        }
    )
    synchro._sync_tarifs(5, "A", {"tiers": [{"label": "T", "price": 200}]})
    assert ids == [-1]
    synchro._create_service.assert_not_called()
    synchro._create_tarif.assert_not_called()
    assert synchro.stats["tarifs_created"] == 1


@pytest.mark.parametrize("avec_commandes", [False, True])
@pytest.mark.parametrize("longueur_retraitement", [0, 1, 12])
def test_statistiques_vides_et_listes_longues(synchro, avec_commandes, longueur_retraitement):
    synchro.paheko_client = paheko_scripte(
        {
            "COUNT": [{"count": 1}],
        }
    )
    synchro.paheko_client.get_member_categories = lambda: {
        "1": {"id": 1, "name": "Autre catégorie"},
    }
    synchro.paheko_client.get_members_by_category = lambda _: []
    commandes = (
        [
            {"payer": {}, "items": []},
            {"payer": {"email": "personne@example.invalid"}, "items": [{"type": "Donation"}]},
        ]
        if avec_commandes
        else []
    )
    synchro.helloasso_client = helloasso_faux(
        get_orders=lambda *a, **k: commandes,
        get_cash_outs=lambda *a: [],
    )
    synchro.verify_items_service_inscriptions = lambda: {
        "total_items": 1,
        "found_inscriptions": 0,
        "missing_inscriptions": 1,
        "completion_rate": 0,
        "orders_to_reprocess": list(range(longueur_retraitement)),
        "items_to_reprocess": list(range(longueur_retraitement)),
    }
    stats = synchro.print_global_statistics()
    assert stats["helloasso"]["commandes"] == len(commandes)
    assert stats["helloasso"]["virements"] == 0


def test_statistiques_completes(synchro):
    synchro.paheko_client = paheko_scripte({"COUNT": [{"count": 1}]})
    synchro.paheko_client.get_member_categories = lambda: {
        "1": {"id": 1, "name": "Membres actifs"},
    }
    synchro.paheko_client.get_members_by_category = lambda _: [{"id": 1}]
    synchro.helloasso_client = helloasso_faux(
        get_orders=lambda *a, **k: [
            {
                "payer": {"email": "personne@example.invalid"},
                "items": [{"type": "Membership"}],
            }
        ],
        get_cash_outs=lambda *a: [{"id": 1}],
    )
    synchro.verify_items_service_inscriptions = lambda: {
        "total_items": 1,
        "completion_rate": 100,
        "found_inscriptions": 1,
    }
    stats = synchro.print_global_statistics()
    assert stats["taux"] == {"commandes": 100, "virements": 100, "membres": 100}


@pytest.mark.parametrize(
    "cles",
    [
        [],
        [
            {"from": "parent", "table": "users", "to": "id"},
            {"from": "parent", "table": "autres", "to": "id"},
        ],
    ],
)
def test_structure_cles_sans_action_et_colonne_partagee(cles):
    client = paheko_scripte(
        {
            "sqlite_master": [{"name": "users"}],
            "table_info": [{"name": "parent", "type": "INTEGER"}],
            "foreign_key_list": cles,
        }
    )
    structure = client.get_tablenames_and_rows()
    assert structure["users"]["columns"]["parent"].startswith("INTEGER")
    if cles:
        assert len(structure["users"]["foreign_keys"]["parent"]) == 2
        assert "ON DELETE" not in structure["users"]["columns"]["parent"]
    else:
        assert "foreign_keys" not in structure["users"]


@pytest.mark.parametrize("type_export", ["orders", "members", "cashouts"])
def test_statistiques_exports_partiels_ignorent_fichiers_texte(synchro, type_export):
    messages = []
    for nom in ("orders", "members", "cashouts"):
        dossier = Path(synchro.non_synced_dir) / nom
        dossier.mkdir(parents=True)
        (dossier / "note.txt").write_text("hors rapport", encoding="utf-8")
    (Path(synchro.non_synced_dir) / type_export / "1.json").write_text("{}", encoding="utf-8")
    identifiant = logger.add(messages.append, format="{message}")
    try:
        synchro.print_sync_statistics()
    finally:
        logger.remove(identifiant)
    texte = "".join(messages)
    assert "EXPORTS JSON" in texte
    assert any(
        f"{type_export.capitalize()}:" in message and "1 fichiers" in message
        for message in messages
    )


def test_pdf_nouveau_document_dans_session_deja_connectee(tmp_path, playwright_faux):
    telechargeur = HelloAssoPDFDownloader("u", "p")
    telechargeur.start_browser()
    telechargeur.login()
    chemin = telechargeur.download_pdf("https://h/nouveau", "A", "B", str(tmp_path))
    assert Path(chemin).read_bytes().startswith(b"%PDF")
    telechargeur.close()
    assert playwright_faux.ferme


def test_statistiques_membres_non_adherents_et_virements_manquants(synchro):
    synchro.paheko_client = paheko_scripte({"COUNT": [{"count": 0}]})
    synchro.paheko_client.get_member_categories = lambda: {
        "2": {"name": "Non adhérents"},
    }
    synchro.paheko_client.get_members_by_category = lambda _: []
    synchro.helloasso_client = helloasso_faux(
        get_orders=lambda *a, **k: [
            {
                "payer": {"email": "personne@example.invalid"},
                "items": [{"type": "Membership"}],
            }
        ],
        get_cash_outs=lambda *a: [{"id": 1}],
    )
    synchro.verify_items_service_inscriptions = lambda: {}
    stats = synchro.print_global_statistics()
    assert stats["taux"] == {"commandes": 0, "virements": 0, "membres": 0}
    assert stats["paheko"]["non_adherents"] == 0


def test_csv_virements_vide_ne_cree_rien(synchro, tmp_path):
    chemin = tmp_path / "vide.csv"
    chemin.write_text("", encoding="utf-8")
    synchro.helloasso_client = helloasso_faux(get_payments=lambda *a, **k: [])
    assert synchro.force_create_missing_cashouts_from_csv(str(chemin)) == {
        "success": 0,
        "errors": 0,
        "total_amount": 0,
    }


def test_transaction_inconnue_sans_lignes():
    assert paheko_scripte().transaction_exists("inconnue") is False


def test_actions_planifiees_services_absents_et_formulaires_sans_tarif(synchro):
    synchro.paheko_client = paheko_scripte()
    synchro.paheko_client.get_accounting_years = lambda: [{"label": "Exercice 2026"}]
    synchro._display_planned_actions(
        [
            {"formType": "Membership", "formSlug": "a", "tiers": [{"label": "A"}]},
            {"formType": "Event", "formSlug": "b", "tiers": []},
        ]
    )
    assert any("services" in q for q, *_ in synchro.paheko_client.requetes)


def test_affichage_tarifs_gratuits_et_payants(synchro):
    synchro.paheko_client = paheko_scripte(
        {
            "FROM services ORDER": [{"id": 5, "label": "Service"}],
            "services_fees": [
                {"label": "Offert", "amount": 0},
                {"label": "Payant", "amount": 1250},
            ],
        }
    )
    messages = []
    identifiant = logger.add(messages.append, format="{message}")
    try:
        synchro._display_existing_services()
    finally:
        logger.remove(identifiant)
    texte = "".join(messages)
    assert "Offert: 0.00" in texte
    assert "Payant: 12.50" in texte


def test_actions_planifiees_adhesion_existante(synchro):
    synchro.paheko_client = paheko_scripte({"WHERE label": [{"id": 5}]})
    synchro.paheko_client.get_accounting_years = lambda: [{"label": "Exercice 2026"}]
    synchro._display_planned_actions(
        [
            {
                "formType": "Membership",
                "formSlug": "a",
                "tiers": [{"label": "A"}],
            }
        ]
    )
    assert len(synchro.paheko_client.requetes) == 2


def test_synchronisation_services_simulee_sans_formulaires(synchro):
    synchro.mode_simulation = True
    synchro.paheko_client = paheko_scripte()
    synchro.helloasso_client = helloasso_faux(get_all_forms_with_details=lambda *a: [])
    assert synchro.sync_all() == synchro.stats
    assert synchro.stats["services_created"] == 0


def test_actions_planifiees_tarif_absent_du_service_existant(synchro):
    synchro.paheko_client = paheko_scripte(
        {
            "FROM services_fees": [],
            "WHERE label": [{"id": 5}],
        }
    )
    synchro._display_planned_actions(
        [
            {
                "formType": "Event",
                "formSlug": "a",
                "tiers": [{"label": "A"}],
            }
        ]
    )
    assert any("services_fees" in q for q, *_ in synchro.paheko_client.requetes)


def test_journaux_ignorent_exercices_sans_identifiant():
    client = fabrique_client_paheko()
    client.get_accounting_years = lambda: [{}, {"id": None}]
    client.get_journal_for_account = Mock()
    assert client.get_journals_for_account_all_years("512") == []
    client.get_journal_for_account.assert_not_called()
