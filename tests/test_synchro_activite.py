# SPDX-License-Identifier: GPL-3.0-or-later
"""Issue #26 — is_order_fully_synced vérifie l'activité sans la créer.

Avant correction : ``get_activity`` était appelé avec trois arguments
(signature à un), ``TypeError`` rattrapée, donc toute commande portant un
formulaire était jugée non synchronisée. Le contrôle passe maintenant par
``find_activity`` (lecture seule) avec le libellé de ``_get_serviceLabel``.

Le vrai ``PahekoAPIClient`` est utilisé (seul ``get_sql`` est scripté) pour
que les signatures réelles soient exercées.
"""

from __future__ import annotations

import pytest
from test_palier2_clients import fabrique_client_helloasso, fabrique_client_paheko

COMMANDE = {
    "id": 100,
    "date": "2026-01-02T10:00:00",
    "payer": {"email": "alice@exemple.fr", "firstName": "alice", "lastName": "Martin"},
    "items": [{"id": 1000}],
    "formSlug": "adhesion-2026",
    "formType": "Membership",
}


def paheko_scripte(activites):
    """Vrai client Paheko : membres et transactions présents, SQL scripté."""
    client = fabrique_client_paheko()
    client.requetes = []

    def get_sql(query, params=None, mode="api", fetch_mode="all"):
        client.requetes.append(query)
        return {"results": activites}

    client.get_sql = get_sql
    client.get_member_by_name = lambda nom: {"id": 1}
    client.get_current_year_id = lambda date_commande, org_slug: 1
    client.transaction_exists = lambda reference, year_id: True
    return client


def helloasso():
    client = fabrique_client_helloasso()
    client.config = {"helloasso": {}}
    client.get_organization_slug = lambda: "asso"
    return client


def test_activite_existante_commande_synchronisee():
    paheko = paheko_scripte([{"id": 9, "label": "Adhésion - adhesion-2026 2026"}])
    assert helloasso().is_order_fully_synced(COMMANDE, paheko) is True
    assert paheko.requetes == [
        "SELECT id, label FROM services WHERE label LIKE '%Adhésion - adhesion-2026 2026%' LIMIT 1"
    ]


def test_activite_absente_ni_creee_ni_synchronisee():
    paheko = paheko_scripte([])
    assert helloasso().is_order_fully_synced(COMMANDE, paheko) is False
    assert not any("INSERT" in r for r in paheko.requetes), "un contrôle ne doit rien créer"


def test_find_activity_cache_et_erreur():
    paheko = fabrique_client_paheko()
    paheko.activities_cache["Atelier"] = {"id": 3}
    assert paheko.find_activity("Atelier") == {"id": 3}

    def get_sql(*args, **kwargs):
        raise RuntimeError("base indisponible")

    paheko.get_sql = get_sql
    assert paheko.find_activity("Autre") is None


@pytest.mark.parametrize("libelle", ["Atelier d'essai"])
def test_find_activity_echappe_les_apostrophes(libelle):
    paheko = paheko_scripte([{"id": 4, "label": libelle}])
    assert paheko.find_activity(libelle) == {"id": 4, "label": libelle}
    assert "Atelier d''essai" in paheko.requetes[0]
