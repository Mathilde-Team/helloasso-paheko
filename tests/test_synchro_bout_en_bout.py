# SPDX-License-Identifier: GPL-3.0-or-later
"""Issue #29 — is_order_fully_synced sur les vraies méthodes de PahekoAPIClient.

Seuls les accès externes sont scriptés : ``get_sql`` (base Paheko),
``get_all_users_by_categories`` (API membres) et ``get_accounting_years``
(exercices). Les méthodes appelées par le contrôle — ``get_member_by_name``,
``get_current_year_id``, ``transaction_exists``, ``_get_serviceLabel``,
``find_activity`` — sont les vraies : une signature divergente (#26, #29)
fait échouer ces tests au lieu d'être masquée par le ``except`` global.
"""

from __future__ import annotations

import pytest
from test_palier2_clients import fabrique_client_helloasso, fabrique_client_paheko

COMMANDE = {
    "id": 100,
    "date": "2026-01-02T10:00:00+01:00",
    "payer": {"email": "alice@exemple.fr", "firstName": "alice", "lastName": "Martin"},
    "items": [
        {"id": 1000, "user": {"email": "bob@exemple.fr", "firstName": "bob", "lastName": "Durand"}}
    ],
    "formSlug": "adhesion-2026",
    "formType": "Membership",
}

MEMBRES = [
    {"id": 1, "nom": "MARTIN Alice (alice@exemple.fr)"},
    {"id": 2, "nom": "DURAND Bob (bob@exemple.fr)"},
]
EXERCICES = [
    {"id": 5, "label": "Exercice asso 2026", "start_date": "2026-01-01", "end_date": "2026-12-31"}
]


def paheko(references, activites, membres=MEMBRES, exercices=EXERCICES):
    """Vrai client Paheko dont seuls les accès externes sont scriptés."""
    client = fabrique_client_paheko()
    client.requetes = []

    def get_sql(query, params=None, mode="api", fetch_mode="all"):
        client.requetes.append(query)
        if "acc_transactions" in query:
            trouvee = any(f"'%{r}%'" in query for r in references)
            return {"results": [{"count": 1 if trouvee else 0}]}
        if "FROM services" in query:
            return {"results": activites}
        raise AssertionError(f"requête inattendue : {query}")

    client.get_sql = get_sql
    client.get_all_users_by_categories = lambda: membres
    client.get_accounting_years = lambda: exercices
    return client


def helloasso():
    client = fabrique_client_helloasso()
    client.config = {"helloasso": {}}
    client.get_organization_slug = lambda: "asso"
    return client


ACTIVITE = [{"id": 9, "label": "Adhésion - adhesion-2026 2026"}]
TOUTES = {"HelloAsso 100", "HelloAsso 1000"}


def test_commande_complete_synchronisee():
    client = paheko(TOUTES, ACTIVITE)
    assert helloasso().is_order_fully_synced(COMMANDE, client) is True
    assert not any("INSERT" in r for r in client.requetes)


@pytest.mark.parametrize(
    "references, activites, membres, exercices",
    [
        (TOUTES, ACTIVITE, MEMBRES[:1], EXERCICES),  # bénéficiaire absent
        (TOUTES, ACTIVITE, MEMBRES, []),  # aucun exercice
        ({"HelloAsso 1000"}, ACTIVITE, MEMBRES, EXERCICES),  # transaction principale
        ({"HelloAsso 100"}, ACTIVITE, MEMBRES, EXERCICES),  # transaction d'item
        (TOUTES, [], MEMBRES, EXERCICES),  # activité absente
    ],
)
def test_commande_incomplete(references, activites, membres, exercices):
    client = paheko(references, activites, membres, exercices)
    assert helloasso().is_order_fully_synced(COMMANDE, client) is False
