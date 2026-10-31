"""Tests des correctifs de l'issue #10 (HelloassoPaheko.py).

Le module HelloassoPaheko importe des dépendances applicatives absentes du
socle de test (httpx, playwright, authlib…) : elles sont remplacées par des
doublures injectées dans sys.modules avant l'import, le reste du module est
le vrai code. Le client Paheko est construit sans son __init__ (lourd :
config, téléchargements, sleep) via object.__new__.
"""

import sys
import types

import pytest


def _pose_doublures_modules() -> None:
    """Remplace httpx, playwright, authlib… par des doublures avant l'import.

    Le vrai code de HelloassoPaheko est ensuite importé ; seules les
    dépendances applicatives absentes du socle de test sont fausses.
    """

    def pose(nom: str, **attributs) -> types.ModuleType:
        module = types.ModuleType(nom)
        for cle, valeur in attributs.items():
            setattr(module, cle, valeur)
        sys.modules[nom] = module
        return module

    pose("httpx", Client=object, Timeout=lambda *a, **k: None)
    pose("requests")
    pose("dotenv", load_dotenv=lambda: None)
    pose("tomli_w", dumps=lambda *a, **k: "")

    authlib = pose("authlib")
    integrations = types.ModuleType("authlib.integrations")
    requetes_client = types.ModuleType("authlib.integrations.requests_client")
    requetes_client.OAuth2Session = object
    integrations.requests_client = requetes_client
    authlib.integrations = integrations
    sys.modules["authlib.integrations"] = integrations
    sys.modules["authlib.integrations.requests_client"] = requetes_client

    playwright = pose("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda *a, **k: None
    sync_api.Browser = object
    sync_api.BrowserContext = object
    sync_api.Page = object
    playwright.sync_api = sync_api
    sys.modules["playwright.sync_api"] = sync_api


_pose_doublures_modules()

import HelloassoPaheko  # noqa: E402
from HelloassoPaheko import PahekoAPIClient  # noqa: E402


# ---------------------------------------------------------------------------
# Construction du client sans __init__ et scripting des accès SQL
# ---------------------------------------------------------------------------


def fabrique_client(mode_simulation: bool = False) -> PahekoAPIClient:
    """Construit un PahekoAPIClient sans passer par __init__ (config, réseau)."""
    client = object.__new__(PahekoAPIClient)
    client.mode_simulation = mode_simulation
    client.activities_cache = {}
    client.base_url = "https://paheko.exemple/api"
    client.db_path = "/tmp/paheko-test.sqlite"
    client.appels_sql = []
    client.membres_crees = []
    client.membres_mis_a_jour = []
    return client


def scripte_sql(client: PahekoAPIClient, reponses: list) -> None:
    """Remplace get_sql : enregistre les requêtes, rejoue les réponses données."""

    def get_sql(query, params=None, mode="api", fetch_mode="all"):
        client.appels_sql.append(query)
        if reponses:
            return reponses.pop(0)
        return {"results": []}

    client.get_sql = get_sql


def fabrique_outils_membres(client: PahekoAPIClient, membres_par_nom: dict) -> None:
    """Branche get_member_by_name et les créations/mises à jour sur des enregistreurs."""

    def get_member_by_name(nom):
        return membres_par_nom.get(nom)

    def create_member(data):
        client.membres_crees.append(data)
        return {"id": 999, **data}

    def update_member(member_id, member_data, existing_category=None):
        client.membres_mis_a_jour.append((member_id, member_data))
        return {"ok": True}

    client.get_member_by_name = get_member_by_name
    client.create_member = create_member
    client.update_member = update_member


# ---------------------------------------------------------------------------
# compare_member_data (perte de données des users)
# ---------------------------------------------------------------------------


def test_compare_valeur_exploitable_different_mise_a_jour():
    """Une nouvelle valeur HelloAsso exploitante remplace l'ancienne."""
    client = fabrique_client()
    existant = {"id": 12, "email": "vieux@exemple.fr", "telephone": "0601020304"}
    hello = {"email": "jean@exemple.fr", "telephone": "0601020304"}
    nouveau = dict(hello)

    besoin, data = client.compare_member_data(existant, hello, nouveau)

    assert besoin is True
    assert data["email"] == "jean@exemple.fr"


def test_compare_valeur_vide_ne_degrade_pas_le_membre():
    """Valeur vide/None/placeholder : la donnée Paheko est conservée, rien dans le payload."""
    client = fabrique_client()
    existant = {"id": 12, "email": "vieux@exemple.fr", "telephone": "0601020304"}
    cas = [None, "", "None", "(non renseigné)"]

    for valeur in cas:
        hello = {"email": "vieux@exemple.fr", "telephone": valeur}
        nouveau = dict(hello)

        besoin, data = client.compare_member_data(existant, hello, nouveau)

        # Pas de mise à jour déclenchée, et la clé vide est retirée du
        # payload (sinon Paheko l'écraserait quand même).
        assert besoin is False, f"valeur {valeur!r} ne doit pas déclencher de mise à jour"
        assert "telephone" not in data, f"valeur {valeur!r} ne doit pas partir dans le payload"


def test_compare_aucune_difference_pas_de_mise_a_jour():
    """Données identiques : pas de mise à jour."""
    client = fabrique_client()
    existant = {"id": 12, "email": "jean@exemple.fr"}
    hello = {"email": "jean@exemple.fr"}

    besoin, data = client.compare_member_data(existant, hello, dict(hello))

    assert besoin is False
    assert data == {"email": "jean@exemple.fr"}


def test_compare_cles_ignorees():
    """Les clés ignorées (prénom, nom, catégorie…) sont retirées du payload."""
    client = fabrique_client()
    existant = {"id": 12, "firstName": "Jean", "id_category": 2, "lettre_infos": "oui"}
    hello = {"firstName": "Autre", "id_category": 5, "email": "jean@exemple.fr"}
    nouveau = {"firstName": "Autre", "id_category": 5, "lettre_infos": "non", "email": "jean@exemple.fr"}

    besoin, data = client.compare_member_data(existant, hello, nouveau)

    # Les clés ignorées sont retirées du payload ; les valeurs identiques
    # ne déclenchent rien ; lettre_infos est ignoré côté comparaison.
    assert besoin is False
    assert "firstName" not in data
    assert "id_category" not in data
    assert data["lettre_infos"] == "non"
    assert data["email"] == "jean@exemple.fr"


def test_compare_champ_absent_de_hello_ne_declenche_rien():
    """Un champ présent uniquement dans Paheko ne doit plus déclencher de mise à jour."""
    client = fabrique_client()
    existant = {"id": 12, "email": "jean@exemple.fr", "profession": "Boulanger"}
    hello = {"email": "jean@exemple.fr"}

    besoin, _data = client.compare_member_data(existant, hello, dict(hello))

    # Régression de l'ancien code : updateneed=True systématique ici.
    assert besoin is False


# ---------------------------------------------------------------------------
# get_activity (création des activités)
# ---------------------------------------------------------------------------


def test_get_activity_cache():
    """Une activité déjà en cache n'interroge pas la base."""
    client = fabrique_client()
    client.activities_cache["Atelier"] = {"id": 3, "label": "Atelier"}

    assert client.get_activity("Atelier") == {"id": 3, "label": "Atelier"}
    assert client.appels_sql == []


def test_get_activity_trouvee_en_base():
    """Une activité existante est renvoyée telle quelle."""
    client = fabrique_client()
    scripte_sql(client, [{"results": [{"id": 3, "label": "Atelier"}]}])

    resultat = client.get_activity("Atelier")

    assert resultat == {"id": 3, "label": "Atelier"}
    assert client.activities_cache["Atelier"] == {"id": 3, "label": "Atelier"}


def test_get_activity_absente_est_reellement_cree():
    """Une activité absente est INSÉRÉE puis relue avec son id (régression issue #10)."""
    client = fabrique_client()
    # 1. LIKE vide, 2. SELECT exact vide, 3. INSERT, 4. relecture avec l'id.
    scripte_sql(
        client,
        [
            {"results": []},
            {"results": []},
            {"results": []},
            {"results": [{"id": 7, "label": "Atelier"}]},
        ],
    )

    resultat = client.get_activity("Atelier")

    assert resultat == {"id": 7, "label": "Atelier"}
    requetes = client.appels_sql
    assert any("INSERT INTO services" in r for r in requetes), "l'INSERT doit être exécuté"
    assert any("SELECT" in r and "label = 'Atelier'" in r for r in requetes)


def test_get_activity_apostrophe_echappe():
    """Un libellé avec apostrophe est échappé dans toutes les requêtes."""
    client = fabrique_client()
    scripte_sql(
        client,
        [
            {"results": []},
            {"results": []},
            {"results": []},
            {"results": [{"id": 9, "label": "Atelier d'essai"}]},
        ],
    )

    resultat = client.get_activity("Atelier d'essai")

    assert resultat["id"] == 9
    assert all("d''essai" in r for r in client.appels_sql if "Atelier" in r)


# ---------------------------------------------------------------------------
# ensure_member_exists (création des membres)
# ---------------------------------------------------------------------------


def test_ensure_membre_existant_mis_a_jour_sans_ecraser():
    """Membre existant : mise à jour avec les valeurs exploitables uniquement."""
    client = fabrique_client()
    fabrique_outils_membres(client, {"DUPONT Jean": {"id": 12, "nom": "DUPONT Jean", "email": "vieux@exemple.fr", "category": "Membres actifs"}})
    scripte_sql(client, [])
    update_data = {
        "nom": "DUPONT Jean",
        "nom2": "DUPONT Jean",
        "email": "jean@exemple.fr",
        "telephone": "",
    }

    client.ensure_member_exists(dict(update_data))

    # Le téléphone vide n'est pas envoyé à Paheko ; l'email oui.
    assert len(client.membres_mis_a_jour) == 1
    _id, payload = client.membres_mis_a_jour[0]
    assert payload["email"] == "jean@exemple.fr"
    assert "telephone" not in payload


def test_ensure_membre_administrateur_intouche():
    """Un administrateur n'est jamais modifié."""
    client = fabrique_client()
    fabrique_outils_membres(
        client,
        {"ADMIN Alain": {"id": 1, "nom": "ADMIN Alain", "email": "alain@exemple.fr", "category": "Administrateurs"}},
    )
    scripte_sql(client, [])

    resultat = client.ensure_member_exists(
        {"nom": "ADMIN Alain", "nom2": "ADMIN Alain", "email": "nouveau@exemple.fr"}
    )

    assert resultat["id"] == 1
    assert client.membres_mis_a_jour == []
    assert client.membres_crees == []


def test_ensure_membre_nouveau_est_reellement_cree():
    """Nouveau membre : create_member est APPELÉ (régression issue #10 : appel commenté)."""
    client = fabrique_client()
    fabrique_outils_membres(client, {})
    scripte_sql(client, [])
    update_data = {"nom": "MARTIN Marie", "nom2": "MARTIN Marie", "email": "marie@exemple.fr"}

    client.ensure_member_exists(dict(update_data))

    assert len(client.membres_crees) == 1
    assert client.membres_crees[0]["email"] == "marie@exemple.fr"


def test_ensure_membre_nouveau_en_simulation_pas_de_creation():
    """En mode simulation, aucun membre n'est créé."""
    client = fabrique_client(mode_simulation=True)
    fabrique_outils_membres(client, {})
    scripte_sql(client, [])

    resultat = client.ensure_member_exists(
        {"nom": "MARTIN Marie", "nom2": "MARTIN Marie", "email": "marie@exemple.fr"}
    )

    assert client.membres_crees == []
    assert resultat["id"] is None
