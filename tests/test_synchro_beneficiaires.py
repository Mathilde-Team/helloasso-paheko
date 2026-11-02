# SPDX-License-Identifier: GPL-3.0-or-later
"""Issue #22 — is_order_fully_synced lit le bénéficiaire de chaque item.

Avant correction, le nom était lu dans ``order["user"]`` (clé absente des
commandes HelloAsso) : ``KeyError`` rattrapée, et toute commande avec un
bénéficiaire était jugée non synchronisée.
"""

from __future__ import annotations

from test_palier2_clients import fabrique_client_helloasso


class PahekoFaux:
    """Client Paheko minimal : membres connus, tout le reste présent."""

    def __init__(self, connus):
        self.connus = connus
        self.noms: list[str] = []

    def get_member_by_name(self, nom):
        self.noms.append(nom)
        return {"id": 1} if nom in self.connus else None

    def get_current_year_id(self, date_commande, org_slug):
        return 1

    def transaction_exists(self, reference, year_id):
        return True

    def _get_serviceLabel(self, commande):
        return "Adhésion 2026"

    def find_activity(self, libelle):
        return {"id": 2}


COMMANDE = {
    "id": 100,
    "date": "2026-01-02T10:00:00",
    "payer": {"email": "alice@exemple.fr", "firstName": "alice", "lastName": "Martin"},
    "items": [
        {"id": 1, "user": {"email": "bob@exemple.fr", "firstName": "bob", "lastName": "Durand"}},
        {"id": 2, "user": {"email": "eve@exemple.fr", "firstName": "eve", "lastName": "Petit"}},
        {"id": 3, "user": {"firstName": "sans", "lastName": "Email"}},
        {"id": 4},
    ],
    "formSlug": "adh",
    "formType": "Membership",
}

PAYEUR = "MARTIN Alice (alice@exemple.fr)"


def client():
    instance = fabrique_client_helloasso()
    instance.config = {"helloasso": {}}
    instance.get_organization_slug = lambda: "asso"
    return instance


def test_commande_a_deux_beneficiaires_synchronisee():
    paheko = PahekoFaux({PAYEUR, "DURAND Bob", "PETIT Eve"})
    assert client().is_order_fully_synced(COMMANDE, paheko) is True
    assert sorted(paheko.noms) == ["DURAND Bob", PAYEUR, "PETIT Eve"]


def test_beneficiaire_absent_de_paheko():
    paheko = PahekoFaux({PAYEUR, "DURAND Bob"})
    assert client().is_order_fully_synced(COMMANDE, paheko) is False
    assert "PETIT Eve" in paheko.noms
