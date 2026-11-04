# SPDX-License-Identifier: GPL-3.0-or-later
"""Palier 6 de couverture (issue #5) — chemins d'erreur de PahekoAPIClient.

Sixième palier : les branches restées non couvertes après le palier 5 dans
``PahekoAPIClient`` — retry HTTP (timeouts, 4xx, erreur inattendue),
``get_activity`` (activité trouvée d'emblée, repli sans id, erreur),
``ensure_member_exists`` (membre trouvé par ``nom2``, mise à jour refusée ou
en erreur, simulation, création en erreur), mise à jour et création de
transactions en erreur, ``transaction_exists`` en erreur et ``get_sql``
direct (paramètres, ``fetch_mode="one"``, erreur SQLite).

Aucun appel réseau : le client HTTP, ``get_sql`` et ``sleep`` sont des
doublures.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest
from test_palier2_clients import ClientHttpFaux, ReponseFausse, fabrique_client_paheko
from test_palier4_paheko import sql_scripte

import HelloassoPaheko

httpx_faux = sys.modules["httpx"]


@pytest.fixture(autouse=True)
def sans_attente(monkeypatch):
    """Le backoff du retry ne dort pas pendant les tests."""
    attentes: list[float] = []
    monkeypatch.setattr(HelloassoPaheko, "sleep", attentes.append)
    return attentes


# ---------------------------------------------------------------------------
# _retry_request
# ---------------------------------------------------------------------------


def _sequence(*issues):
    """Fonction HTTP factice : chaque appel rejoue l'issue suivante."""
    restantes = list(issues)
    appels = []

    def appel(*args, **kwargs):
        appels.append((args, kwargs))
        issue = restantes.pop(0)
        if isinstance(issue, Exception):
            raise issue
        return issue

    appel.appels = appels
    return appel


def test_retry_timeout_puis_succes(sans_attente):
    """Un timeout est réessayé avec backoff, puis la réponse est rendue."""
    client = fabrique_client_paheko()
    ok = ReponseFausse(200, {"ok": True})
    func = _sequence(httpx_faux.ReadTimeout("lent"), httpx_faux.ConnectTimeout("lent"), ok)

    assert client._retry_request(func, "url", max_retries=3) is ok
    assert sans_attente == [1, 2]
    assert len(func.appels) == 3


def test_retry_timeout_epuise_relance(sans_attente):
    """Après le dernier timeout, l'exception remonte."""
    client = fabrique_client_paheko()
    func = _sequence(httpx_faux.ReadTimeout("un"), httpx_faux.ReadTimeout("deux"))

    with pytest.raises(httpx_faux.ReadTimeout):
        client._retry_request(func, max_retries=2)
    assert sans_attente == [1]


def test_retry_erreur_client_sans_nouvelle_tentative(sans_attente):
    """Un 4xx lève HTTPStatusError immédiatement, sans retry."""
    client = fabrique_client_paheko()
    func = _sequence(ReponseFausse(404, {}), ReponseFausse(200, {}))

    with pytest.raises(httpx_faux.HTTPStatusError):
        client._retry_request(func)
    assert len(func.appels) == 1
    assert sans_attente == []


def test_retry_erreur_inattendue_relancee(sans_attente):
    """Une exception imprévue remonte telle quelle, sans retry."""
    client = fabrique_client_paheko()
    func = _sequence(RuntimeError("panne"), ReponseFausse(200, {}))

    with pytest.raises(RuntimeError, match="panne"):
        client._retry_request(func)
    assert len(func.appels) == 1


def test_retry_aucune_tentative():
    """max_retries=0 : aucune tentative, rien à relancer, None rendu."""
    client = fabrique_client_paheko()
    func = _sequence()

    assert client._retry_request(func, max_retries=0) is None
    assert func.appels == []


# ---------------------------------------------------------------------------
# get_activity
# ---------------------------------------------------------------------------


def test_get_activity_trouvee_sans_insertion():
    """L'activité existe déjà : rendue et mise en cache, aucun INSERT."""
    client = sql_scripte(fabrique_client_paheko(), [{"results": [{"id": 3, "label": "Yoga"}]}])

    activite = client.get_activity("Yoga")

    assert activite == {"id": 3, "label": "Yoga"}
    assert client.activities_cache["Yoga"] == activite
    assert not any(r.startswith("INSERT") for r in client.requetes)


def test_get_activity_inseree_mais_introuvable():
    """INSERT fait mais relecture vide : repli sans id, mis en cache."""
    client = sql_scripte(fabrique_client_paheko(), [{"results": []}, {}, {"results": []}])

    activite = client.get_activity("Atelier")

    assert activite == {"id": None, "label": "Atelier"}
    assert client.activities_cache["Atelier"] == activite
    assert any(r.startswith("INSERT") for r in client.requetes)


def test_get_activity_erreur_sql():
    """Une erreur SQL donne le repli sans id, sans lever."""
    client = sql_scripte(fabrique_client_paheko(), [RuntimeError("base verrouillée")])

    activite = client.get_activity("Danse")

    assert activite == {"id": None, "label": "Danse"}
    assert client.activities_cache["Danse"] == activite


# ---------------------------------------------------------------------------
# ensure_member_exists
# ---------------------------------------------------------------------------


def _client_membres(membres, *, simulation=False, maj=None, creation=None):
    """Client avec recherche par nom, comparaison et écritures factices."""
    client = fabrique_client_paheko()
    client.mode_simulation = simulation
    client.recherches = []
    client.maj = []
    client.creations = []

    def get_member_by_name(nom):
        client.recherches.append(nom)
        return membres.get(nom)

    def compare_member_data(existant, hello, donnees):
        return True, dict(donnees)

    def update_member(member_id, donnees, categorie):
        client.maj.append((member_id, donnees, categorie))
        if maj is not None:
            raise maj

    def create_member(donnees):
        client.creations.append(donnees)
        if creation is not None:
            raise creation
        membres[donnees["nom"]] = {"id": 99, "nom": donnees["nom"]}

    client.get_member_by_name = get_member_by_name
    client.compare_member_data = compare_member_data
    client.update_member = update_member
    client.create_member = create_member
    return client


DONNEES = {"nom": "MARTIN Marie", "nom2": "MARTIN Marie Louise", "email": "marie@exemple.fr"}


def test_ensure_trouve_par_nom2_et_mis_a_jour():
    """Absent sous « nom », trouvé sous « nom2 » : mis à jour puis relu."""
    existant = {"id": 5, "nom": "MARTIN Marie Louise", "category": "Membres actifs"}
    client = _client_membres({"MARTIN Marie Louise": existant})

    client.ensure_member_exists(dict(DONNEES))

    assert client.recherches[:2] == ["MARTIN Marie", "MARTIN Marie Louise"]
    assert client.maj[0][0] == 5
    assert client.maj[0][2] == "Membres actifs"
    # Relecture après mise à jour, par le nom
    assert client.recherches[-1] == "MARTIN Marie"


@pytest.mark.parametrize("erreur", [ValueError("administrateur"), RuntimeError("HTTP 500")])
def test_ensure_mise_a_jour_en_erreur_rend_le_membre_existant(erreur):
    """Mise à jour refusée (ValueError) ou en panne : le membre existant est rendu."""
    existant = {"id": 6, "nom": "MARTIN Marie", "category": "Sympathisants"}
    client = _client_membres({"MARTIN Marie": existant}, maj=erreur)

    resultat = client.ensure_member_exists(dict(DONNEES))

    assert resultat is existant
    assert len(client.maj) == 1


def test_ensure_mise_a_jour_en_simulation():
    """En simulation, la mise à jour n'est qu'annoncée."""
    existant = {"id": 7, "nom": "MARTIN Marie", "category": None}
    client = _client_membres({"MARTIN Marie": existant}, simulation=True)

    resultat = client.ensure_member_exists(dict(DONNEES))

    assert resultat is existant
    assert client.maj == []


def test_ensure_aucune_mise_a_jour_necessaire():
    """Données identiques : ni mise à jour ni annonce, membre rendu."""
    existant = {"id": 8, "nom": "MARTIN Marie", "category": "Membres actifs"}
    client = _client_membres({"MARTIN Marie": existant})
    client.compare_member_data = lambda existant, hello, donnees: (False, dict(donnees))

    resultat = client.ensure_member_exists(dict(DONNEES))

    assert resultat is existant
    assert client.maj == []


def test_ensure_creation_relue_apres_creation():
    """Nouveau membre créé puis relu par son nom."""
    client = _client_membres({})

    resultat = client.ensure_member_exists(dict(DONNEES))

    assert resultat == {"id": 99, "nom": "MARTIN Marie"}
    assert client.creations[0]["email"] == "marie@exemple.fr"


def test_ensure_creation_en_erreur_rend_none():
    """Création en erreur : None, sans lever."""
    client = _client_membres({}, creation=RuntimeError("HTTP 422"))

    assert client.ensure_member_exists(dict(DONNEES)) is None
    assert len(client.creations) == 1


def test_ensure_creation_en_erreur_donnees_non_serialisables(monkeypatch):
    """Erreur de création et contexte impossible à sérialiser : None quand même."""
    client = _client_membres({}, creation=RuntimeError("HTTP 422"))
    vrai_dumps = HelloassoPaheko.jmodule.dumps
    appels = {"n": 0}

    def dumps(*args, **kwargs):
        # Les deux journaux de debug d'entrée passent, le contexte d'erreur échoue.
        appels["n"] += 1
        if appels["n"] > 2:
            raise TypeError("non sérialisable")
        return vrai_dumps(*args, **kwargs)

    monkeypatch.setattr(HelloassoPaheko.jmodule, "dumps", dumps)

    assert client.ensure_member_exists(dict(DONNEES)) is None


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def test_update_transaction_succes_echappe_les_apostrophes():
    """La requête UPDATE échappe libellé et notes, et rend True."""
    client = sql_scripte(fabrique_client_paheko(), [{}])

    assert client.update_transaction_label_and_reference(
        12, "Virement d'Alice", "OFX-1", notes="ancien l'libellé"
    )
    assert "Virement d''Alice" in client.requetes[0]
    assert "ancien l''libellé" in client.requetes[0]
    assert "WHERE id = 12" in client.requetes[0]


def test_update_transaction_sans_notes():
    """Sans notes, la requête porte des notes vides."""
    client = sql_scripte(fabrique_client_paheko(), [{}])

    assert client.update_transaction_label_and_reference(3, "Libellé", "OFX-2")
    assert "THEN ''" in client.requetes[0]


def test_update_transaction_erreur_rend_false():
    """Une erreur SQL rend False, sans lever."""
    client = sql_scripte(fabrique_client_paheko(), [sqlite3.OperationalError("verrou")])

    assert client.update_transaction_label_and_reference(3, "Libellé", "OFX-2") is False


def test_transaction_exists_erreur_rend_false():
    """Une erreur SQL : la transaction est considérée absente."""
    client = sql_scripte(fabrique_client_paheko(), [RuntimeError("base absente")])

    assert client.transaction_exists("HelloAsso 1") is False


def test_create_transaction_erreur_sans_reponse():
    """Erreur réseau sans réponse HTTP : None, sans lever."""
    client = fabrique_client_paheko()

    class ClientEnPanne:
        def post(self, url, **kwargs):
            raise httpx_faux.ConnectTimeout("injoignable")

    client.client = ClientEnPanne()

    assert client.create_transaction({"reference": "R1"}) is None


def test_create_transaction_erreur_dans_le_traitement():
    """Erreur pendant l'analyse de l'exception elle-même : None quand même."""
    client = fabrique_client_paheko()

    class ReponseHostile:
        @property
        def status_code(self):
            raise RuntimeError("réponse illisible")

    class Erreur(Exception):
        response = ReponseHostile()

    class Client:
        def post(self, url, **kwargs):
            raise Erreur("HTTP")

    client.client = Client()
    # getattr(resp, "status_code", "?") ne capture que AttributeError :
    # l'erreur part dans le second except.
    assert client.create_transaction({"reference": "R2"}) is None


def test_create_transaction_erreur_http_avec_reponse():
    """Erreur HTTP avec réponse : statut et corps journalisés, None rendu."""
    client = fabrique_client_paheko()
    client.client = ClientHttpFaux([ReponseFausse(400, {"erreur": "compte inconnu"})])

    assert client.create_transaction({"reference": "R3"}) is None


# ---------------------------------------------------------------------------
# get_sql en mode direct
# ---------------------------------------------------------------------------


@pytest.fixture
def base(tmp_path):
    """Base SQLite minimale : une table de membres."""
    chemin = tmp_path / "base.sqlite"
    conn = sqlite3.connect(chemin)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, nom TEXT);"
        "INSERT INTO users (nom) VALUES ('Alice'), ('Bruno');"
    )
    conn.commit()
    conn.close()
    client = fabrique_client_paheko()
    client.db_path = str(chemin)
    return client


def test_get_sql_direct_avec_parametres_un_resultat(base):
    """Paramètres liés et fetch_mode="one" : une seule ligne."""
    resultat = base.get_sql("SELECT nom FROM users WHERE id > ?", (0,), mode="direct", fetch_mode="one")

    assert resultat == {"results": [{"nom": "Alice"}]}


def test_get_sql_direct_un_resultat_vide(base):
    """fetch_mode="one" sans ligne : liste vide."""
    resultat = base.get_sql("SELECT nom FROM users WHERE id = ?", (42,), mode="direct", fetch_mode="one")

    assert resultat == {"results": []}


def test_get_sql_direct_erreur_sqlite_relancee(base):
    """Requête invalide : sqlite3.Error remonte."""
    with pytest.raises(sqlite3.Error):
        base.get_sql("SELECT * FROM table_absente", mode="direct")


def test_get_sql_direct_base_absente():
    """Chemin de base inexistant : FileNotFoundError."""
    client = fabrique_client_paheko()
    client.db_path = "/chemin/inexistant.sqlite"

    with pytest.raises(FileNotFoundError):
        client.get_sql("SELECT 1", mode="direct")


# ---------------------------------------------------------------------------
# Synchronisation : création forcée et vérifications (chemins restants)
# ---------------------------------------------------------------------------

from test_palier5_synchro import (  # noqa: E402
    ClientHttpJournal,
    fabrique_synchro,
    helloasso_faux,
    paheko_scripte,
)


class ClientHttpData:
    """Client HTTP Paheko au format {"data": [...]} ; une année peut échouer."""

    def __init__(self, journal, annee_en_panne=None):
        self.journal = journal
        self.annee_en_panne = annee_en_panne

    def get(self, url, params=None):
        if url.endswith("/accounting/years"):
            return ReponseFausse(
                200, {"data": [{"id": 1, "label": "2025"}, {"id": 2, "label": "2026"}]}
            )
        if self.annee_en_panne and f"/years/{self.annee_en_panne}/" in url:
            raise RuntimeError("journal indisponible")
        return ReponseFausse(200, {"data": self.journal})


@pytest.fixture
def sync(tmp_path):
    return fabrique_synchro(tmp_path)


def _csv(chemin, lignes):
    chemin.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    return str(chemin)


class TestCreationForcee:
    def _preparer(self, sync, existants=(), resultat=True):
        crees = []

        def creer(cash_out):
            crees.append(cash_out)
            if isinstance(resultat, Exception):
                raise resultat
            return resultat

        sync.get_helloasso_cashouts_from_paheko = lambda year_id=None: set(existants)
        sync.create_bank_transfer_transaction = creer
        return crees

    def test_csv_vide(self, sync, tmp_path):
        """Fichier vide : rien à lire, statistiques nulles."""
        chemin = tmp_path / "vide.csv"
        chemin.write_text("", encoding="utf-8")

        assert sync.force_create_missing_cashouts_from_csv(str(chemin)) == {
            "success": 0,
            "errors": 0,
            "total_amount": 0,
        }

    def test_csv_latin1_doublon_date_et_montant_invalides(self, sync, tmp_path):
        """Encodage latin-1, virement déjà présent, date et montant invalides."""
        chemin = tmp_path / "virements.csv"
        chemin.write_bytes(
            "cashoutId,Date,Montant,Libellé\n"
            "1,2026-02-01,10.00,déjà là\n"
            "2,01/02/2026,20.50,date française\n"
            "3,2026-02-03,vingt,montant illisible\n".encode("latin-1")
        )
        crees = self._preparer(sync, existants={1})

        resultat = sync.force_create_missing_cashouts_from_csv(str(chemin))

        assert resultat["success"] == 1
        assert resultat["errors"] == 1
        assert resultat["total_amount"] == pytest.approx(20.5)
        # La date invalide est remplacée par la date du jour (chaîne ISO).
        assert [c["id"] for c in crees] == [2]
        assert isinstance(crees[0]["date"], str)
        assert crees[0]["total_amount_centimes"] == 2050

    def test_creation_refusee_ou_en_exception(self, sync, tmp_path):
        """Création refusée (None) puis exception : deux erreurs."""
        chemin = _csv(tmp_path / "v.csv", ["cashoutId,Date,Montant", "4,2026-02-01,5"])
        self._preparer(sync, resultat=None)
        assert sync.force_create_missing_cashouts_from_csv(chemin)["errors"] == 1

        self._preparer(sync, resultat=RuntimeError("HTTP 500"))
        assert sync.force_create_missing_cashouts_from_csv(chemin)["errors"] == 1


class TestVerificationsFormatData:
    JOURNAL = [  # noqa: RUF012
        {"id": 1, "reference": "HelloAsso 1", "debit": 10.0, "credit": 0.0},
        {"id": 2, "reference": "Virement HelloAsso abc", "debit": 0.0, "credit": 0.0},
        {"id": 3, "reference": "HelloAsso xyz", "debit": 0.0, "credit": 0.0},
    ]

    def test_doublons_csv_absent(self, sync, tmp_path):
        resultat = sync.verify_no_duplicate_references(str(tmp_path / "absent.csv"))
        assert resultat == {"duplicates": [], "unique_count": 0, "duplicate_count": 0}

    def test_doublons_format_data_et_annee_en_panne(self, sync, tmp_path):
        """Années et journaux au format {"data": …}, une année en erreur."""
        chemin = _csv(tmp_path / "v.csv", ["cashoutId", "1"])
        sync.paheko_client = paheko_scripte()
        sync.paheko_client.client = ClientHttpData(self.JOURNAL, annee_en_panne=2)

        resultat = sync.verify_no_duplicate_references(chemin)

        assert resultat["duplicate_count"] == 0
        assert resultat["total_helloasso_refs"] == 3

    def test_doublons_erreur_globale(self, sync, tmp_path, monkeypatch):
        """Erreur hors des blocs protégés : résultat vide."""
        chemin = _csv(tmp_path / "v.csv", ["cashoutId", "1"])
        monkeypatch.setattr(HelloassoPaheko, "Path", _path_en_panne)

        resultat = sync.verify_no_duplicate_references(chemin)

        assert resultat == {"duplicates": [], "unique_count": 0, "duplicate_count": 0}

    def test_virements_csv_absent_ou_vide(self, sync, tmp_path):
        vide = {"missing": [], "found": [], "total": 0, "completion_rate": 0}
        assert sync.verify_all_cashouts_created(str(tmp_path / "absent.csv")) == vide
        chemin = tmp_path / "vide.csv"
        chemin.write_text("", encoding="utf-8")
        assert sync.verify_all_cashouts_created(str(chemin)) == vide

    def test_virements_latin1_format_data_reference_illisible(self, sync, tmp_path):
        """CSV latin-1, réponses {"data": …}, référence sans identifiant."""
        chemin = tmp_path / "v.csv"
        chemin.write_bytes("cashoutId,Date,Montant,Libellé\n1,2026-02-01,10.00,é\n".encode("latin-1"))
        sync.paheko_client = paheko_scripte()
        sync.paheko_client.client = ClientHttpData(self.JOURNAL, annee_en_panne=2)

        resultat = sync.verify_all_cashouts_created(str(chemin))

        assert resultat["total"] == 1

    def test_virements_erreur_globale(self, sync, tmp_path, monkeypatch):
        monkeypatch.setattr(HelloassoPaheko, "Path", _path_en_panne)
        assert sync.verify_all_cashouts_created("x.csv") == {
            "missing": [],
            "found": [],
            "total": 0,
            "completion_rate": 0,
        }

    def test_commandes_aucune(self, sync):
        sync.paheko_client = paheko_scripte()
        sync.helloasso_client = helloasso_faux(get_orders=lambda *a, **k: [])

        assert sync.verify_orders_created()["total_orders"] == 0

    def test_commandes_format_data_montant_simple(self, sync):
        """Réponses {"data": …}, année en panne, montant non structuré."""
        sync.paheko_client = paheko_scripte()
        sync.paheko_client.client = ClientHttpData(self.JOURNAL, annee_en_panne=2)
        sync.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [
                {"id": 1, "date": "2026-01-02T10:00:00Z", "amount": 1000},
                {"id": 5, "date": "2026-01-03T10:00:00Z", "amount": {"total": 500}},
            ]
        )

        resultat = sync.verify_orders_created()

        assert resultat["total_orders"] == 2
        assert [m["id"] for m in resultat["missing"]] == [5]

    def test_commandes_erreur_globale(self, sync):
        def get_orders(*a, **k):
            raise RuntimeError("HelloAsso indisponible")

        sync.paheko_client = paheko_scripte()
        sync.helloasso_client = helloasso_faux(get_orders=get_orders)

        resultat = sync.verify_orders_created()

        assert resultat["missing"] == []

    def test_membres_liste_doublons_et_categorie_en_panne(self, sync):
        """Catégories en liste, un membre dans deux catégories, une catégorie en erreur."""
        sync.paheko_client = paheko_scripte()
        sync.paheko_client.client = ClientHttpJournal()
        sync.paheko_client.client.get = lambda url, **k: ReponseFausse(
            200,
            [
                {"id": 1, "name": "Actifs", "count": 1},
                {"id": 2, "name": "Bureau", "count": 1},
                {"id": 3, "name": "Archives", "count": 4},
            ],
        )
        alice = {"nom": "Martin", "prenom": "Alice", "email": "alice@exemple.fr"}

        def membres(cid):
            if cid == 3:
                raise RuntimeError("catégorie illisible")
            return [dict(alice), {"nom": "Sans", "prenom": "Mail", "email": ""}]

        sync.paheko_client.get_members_by_category = membres
        sync.helloasso_client = helloasso_faux()

        resultat = sync.verify_members_by_category()

        assert resultat["total_paheko"] == 6
        assert resultat["total_unique_members"] == 1

    def test_membres_reponse_inattendue(self, sync):
        """Réponse ni dict ni liste : aucune catégorie."""
        sync.paheko_client = paheko_scripte()
        sync.paheko_client.client = ClientHttpJournal()
        sync.paheko_client.client.get = lambda url, **k: ReponseFausse(200, "texte")
        sync.helloasso_client = helloasso_faux()

        assert sync.verify_members_by_category()["total_paheko"] == 0

    def test_membres_erreur_globale(self, sync, monkeypatch):
        """Compteur non numérique : la synthèse lève, dictionnaire d'erreur."""
        sync.paheko_client = paheko_scripte()
        sync.paheko_client.client = ClientHttpJournal()
        sync.paheko_client.client.get = lambda url, **k: ReponseFausse(
            200, [{"id": 1, "name": "Actifs", "count": "beaucoup"}]
        )
        sync.paheko_client.get_members_by_category = lambda cid: []
        sync.helloasso_client = helloasso_faux()

        resultat = sync.verify_members_by_category()

        assert resultat["completion_rate"] == 0

    def test_comparaison_payeurs_vides_invalides_et_extra(self, sync):
        """Payeur absent, email invalide, membre Paheko en extra."""
        sync.verify_members_by_category = lambda: {
            "all_members": {
                "alice@exemple.fr": {"name": "Martin Alice", "categories": [{"name": "Actifs"}]},
                "bruno@exemple.fr": {"name": "Durand Bruno", "categories": [{"name": "Actifs"}]},
            }
        }
        sync.helloasso_client = helloasso_faux(
            get_orders=lambda *a, **k: [
                {"id": 1},
                {"id": 2, "payer": {"firstName": "X", "email": "pas-un-email"}},
                {"id": 3, "payer": {"firstName": "Alice", "lastName": "Martin", "email": "alice@exemple.fr"}},
            ]
        )

        resultat = sync.verify_helloasso_paheko_members_comparison()

        assert "bruno@exemple.fr" in str(resultat)


def _path_en_panne(*args, **kwargs):
    raise OSError("système de fichiers indisponible")
