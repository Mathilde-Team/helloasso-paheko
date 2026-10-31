"""Palier 2 de couverture (issue #5) — clients Paheko et HelloAsso.

Deuxième palier de couverture de ``HelloassoPaheko.py`` : méthodes de
recherche de membres (email, nom, téléphone, homonymes), retry HTTP avec
backoff, catégories et journaux, transactions comptables, ``get_sql``
(mode API et mode direct sur SQLite réelle), sauvegardes, et la logique
de jeton OAuth2 de ``HelloAssoHTTPClient`` (chargement, expiration,
renouvellement, requêtes avec fusion d'en-têtes et relance sur 401).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest


def _pose_doublures_modules() -> None:
    """Remplace httpx, playwright, authlib… par des doublures avant l'import.

    La doublure httpx expose aussi les exceptions utilisées par
    ``_retry_request`` (ReadTimeout, ConnectTimeout, HTTPStatusError).
    """

    def pose(nom: str, **attributs) -> types.ModuleType:
        module = types.ModuleType(nom)
        for cle, valeur in attributs.items():
            setattr(module, cle, valeur)
        sys.modules[nom] = module
        return module

    class ErreurHttpx(Exception):
        """Exception de base de la doublure httpx."""

    class ReadTimeout(ErreurHttpx):
        """Dépassement de délai en lecture."""

    class ConnectTimeout(ErreurHttpx):
        """Dépassement de délai à la connexion."""

    class HTTPStatusError(ErreurHttpx):
        """Statut HTTP 4xx/5xx après raise_for_status()."""

        def __init__(self, response):
            super().__init__(f"statut {response.status_code}")
            self.response = response

    class Reponse:
        """Réponse HTTP minimale : code, json, raise_for_status, texte."""

        def __init__(self, code=200, donnees=None):
            self.status_code = code
            self._donnees = donnees
            self.text = json.dumps(donnees) if donnees is not None else ""

        def json(self):
            """Corps JSON de la réponse."""
            return self._donnees

        def raise_for_status(self):
            """Lève HTTPStatusError si le code est 4xx/5xx."""
            if self.status_code >= 400:
                raise HTTPStatusError(self)

    pose(
        "httpx",
        Client=object,
        Timeout=lambda *a, **k: None,
        ReadTimeout=ReadTimeout,
        ConnectTimeout=ConnectTimeout,
        HTTPStatusError=HTTPStatusError,
        HTTPError=ErreurHttpx,
        request=None,
        Reponse=Reponse,
    )
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

    # httpx.request est patché par les tests ; garder une valeur par défaut
    sys.modules["httpx"].request = lambda *a, **k: None


_pose_doublures_modules()

import HelloassoPaheko
from HelloassoPaheko import HelloAssoHTTPClient, PahekoAPIClient

# HelloassoPaheko a pu être importé avant (autre module de test) avec une
# doublure httpx minimale : rebrancher son global httpx sur la nôtre,
# qui expose ReadTimeout/ConnectTimeout/HTTPStatusError et request.
HelloassoPaheko.httpx = sys.modules["httpx"]

HTTPStatusError = sys.modules["httpx"].HTTPStatusError
ReponseFausse = sys.modules["httpx"].Reponse  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Aides : clients sans __init__, réponses et base SQLite de test
# ---------------------------------------------------------------------------


def fabrique_client_paheko() -> PahekoAPIClient:
    """Construit un PahekoAPIClient sans __init__ (config, réseau, sleep)."""
    client = object.__new__(PahekoAPIClient)
    client.mode_simulation = False
    client.activities_cache = {}
    client.base_url = "https://paheko.exemple/api"
    client.db_path = ""
    client.backup_sqlite_path = "."
    client.backup_zip_path = "."
    return client


def fabrique_client_helloasso() -> HelloAssoHTTPClient:
    """Construit un HelloAssoHTTPClient sans __init__ (config, OAuth)."""
    client = object.__new__(HelloAssoHTTPClient)
    client.base_url = "https://api.helloasso.example"
    client.token_url = "https://oauth.helloasso.example/token"
    client.token_file = "/tmp/token-helloasso-test.json"
    client.client_id = "cle"
    client.client_secret = "secret"
    client.token = None
    client.headers = {}
    return client


class ClientHttpFaux:
    """Client HTTP enregistreur : rejoue les réponses données par requête."""

    def __init__(self, reponses=None):
        self.reponses = list(reponses or [])
        self.appels: list[tuple] = []

    def get(self, url, **kwargs):
        """GET enregistré, réponse rejouée ou 404."""
        self.appels.append(("GET", url, kwargs))
        return self.reponses.pop(0) if self.reponses else ReponseFausse(404, [])

    def post(self, url, **kwargs):
        """POST enregistré, réponse rejouée ou 404."""
        self.appels.append(("POST", url, kwargs))
        return self.reponses.pop(0) if self.reponses else ReponseFausse(404, [])

    def stream(self, method, url):
        """Flux GET : itérateur d'octets minimal."""
        self.appels.append((method, url, None))

        class Flux:
            """Contexte de streaming : réponse avec raise_for_status et iter_bytes."""

            def __enter__(self):
                self.reponse = ReponseFausse(200, None)
                self.reponse.iter_bytes = self.iter_bytes
                return self.reponse

            def __exit__(self, *exc):
                return False

            def iter_bytes(self, chunk_size):
                """Yield un unique morceau."""
                yield b"donnees-de-sauvegarde"

        return Flux()


def fabrique_base_sqlite(chemin: Path) -> None:
    """Crée une base SQLite de test avec années et transactions."""
    conn = sqlite3.connect(chemin)
    conn.executescript(
        """
        CREATE TABLE acc_years (
            id INTEGER PRIMARY KEY, label TEXT,
            start_date TEXT, end_date TEXT);
        CREATE TABLE acc_transactions (
            id INTEGER PRIMARY KEY, reference TEXT, label TEXT, notes TEXT);
        INSERT INTO acc_years VALUES
            (1, 'Exercice transcende 2026', '2026-01-01', '2026-12-31'),
            (2, 'Exercice 2025', '2025-01-01', '2025-12-31');
        INSERT INTO acc_transactions VALUES
            (10, 'VIRMT-HELLOASSO-1', 'Cotisation', '');
        """
    )
    conn.commit()
    conn.close()


USERS = [
    {"nom": "Alice Martin", "email": "alice@exemple.fr", "telephone": "06 12 34 56 78"},
    {"nom": "Bob Dupont", "email": "BOB@exemple.fr", "telephone": "06 98 76 54 32"},
]


# ---------------------------------------------------------------------------
# Recherche de membres (email, nom, téléphone, homonymes)
# ---------------------------------------------------------------------------


class TestRechercheMembres:
    def test_get_member_by_email_insensible_a_la_casse(self):
        """Vérifie : recherche par email trouvée malgré la casse différente."""
        client = fabrique_client_paheko()
        client.get_all_users_by_categories = lambda: USERS
        assert client.get_member_by_email("ALICE@exemple.fr") == USERS[0]

    def test_get_member_by_email_absent_renvoie_none(self):
        """Vérifie : recherche par email infructueuse renvoie None."""
        client = fabrique_client_paheko()
        client.get_all_users_by_categories = lambda: USERS
        assert client.get_member_by_email("carole@exemple.fr") is None

    def test_get_member_by_name_sous_chaine(self):
        """Vérifie : recherche par nom en sous-chaîne, casse ignorée."""
        client = fabrique_client_paheko()
        client.get_all_users_by_categories = lambda: USERS
        assert client.get_member_by_name("alice martin") == USERS[0]

    def test_get_member_by_name_absent_renvoie_none(self):
        """Vérifie : recherche par nom infructueuse renvoie None."""
        client = fabrique_client_paheko()
        client.get_all_users_by_categories = lambda: USERS
        assert client.get_member_by_name("Zorba") is None

    def test_get_member_by_phone_normalise(self):
        """Vérifie : le téléphone est normalisé (espaces, +33) avant comparaison."""
        client = fabrique_client_paheko()
        client.get_all_users_by_categories = lambda: USERS
        assert client.get_member_by_phone("06.12.34.56.78") == USERS[0]
        assert client.get_member_by_phone("0698765432") == USERS[1]

    def test_get_member_by_phone_vide_ou_sans_chiffre(self):
        """Vérifie : numéro vide, None ou sans chiffre renvoie None sans chercher."""
        client = fabrique_client_paheko()
        appels = []

        def tous_les_utilisateurs():
            appels.append(1)
            return USERS

        client.get_all_users_by_categories = tous_les_utilisateurs
        assert client.get_member_by_phone("") is None
        assert client.get_member_by_phone(None) is None
        assert client.get_member_by_phone("aucun-chiffre") is None
        # "" et None ne déclenchent même pas le chargement des utilisateurs
        assert len(appels) == 1

    def test_get_member_by_name_and_phone_concordance(self):
        """Vérifie : homonyme identifié par nom ET téléphone concordants."""
        client = fabrique_client_paheko()
        client.get_all_users_by_categories = lambda: USERS
        assert client.get_member_by_name_and_phone("Alice", "0612345678") == USERS[0]

    def test_get_member_by_name_and_phone_sans_concordance(self):
        """Vérifie : nom trouvé mais téléphone différent renvoie None."""
        client = fabrique_client_paheko()
        client.get_all_users_by_categories = lambda: USERS
        assert client.get_member_by_name_and_phone("Alice", "0698765432") is None


# ---------------------------------------------------------------------------
# _retry_request : retry, backoff, erreurs 4xx/5xx
# ---------------------------------------------------------------------------


class TestRetryRequest:
    def test_succes_immediat(self):
        """Vérifie : une réponse 200 est renvoyée telle quelle."""
        client = fabrique_client_paheko()
        reponse = client._retry_request(
            lambda url: ReponseFausse(200, {"ok": True}), "u"
        )
        assert reponse.status_code == 200

    def test_erreur_500_puis_succes(self):
        """Vérifie : un 5xx est retenté jusqu'au succès."""
        client = fabrique_client_paheko()
        tentatives = iter([ReponseFausse(503, None), ReponseFausse(200, {"ok": 1})])

        def func(url):
            return next(tentatives)

        with patch("HelloassoPaheko.sleep"):
            reponse = client._retry_request(func, "u")
        assert reponse.status_code == 200

    def test_erreur_500_epuisee_leve(self):
        """Vérifie : 5xx sur toutes les tentatives finit par lever l'erreur."""
        client = fabrique_client_paheko()
        with (
            patch("HelloassoPaheko.sleep"),
            pytest.raises(HTTPStatusError),
        ):
            client._retry_request(
                lambda url: ReponseFausse(500, None), "u", max_retries=2
            )

    def test_erreur_400_immediate_sans_retry(self):
        """Vérifie : un 4xx lève immédiatement, sans re-tentative."""
        client = fabrique_client_paheko()
        appels = []

        def func(url):
            appels.append(url)
            return ReponseFausse(404, None)

        with pytest.raises(HTTPStatusError):
            client._retry_request(func, "u")
        assert len(appels) == 1

    def test_timeout_puis_succes(self):
        """Vérifie : un ReadTimeout est retenté jusqu'au succès."""
        client = fabrique_client_paheko()
        ReadTimeout = sys.modules["httpx"].ReadTimeout
        tentatives = iter([ReadTimeout("trop lent"), ReponseFausse(200, {"ok": 1})])

        def func(url):
            element = next(tentatives)
            if isinstance(element, Exception):
                raise element
            return element

        with patch("HelloassoPaheko.sleep"):
            reponse = client._retry_request(func, "u")
        assert reponse.status_code == 200

    def test_timeout_epuise_releve(self):
        """Vérifie : tous les essais en timeout finissent par lever l'erreur."""
        client = fabrique_client_paheko()
        ReadTimeout = sys.modules["httpx"].ReadTimeout

        def func(url):
            raise ReadTimeout("trop lent")

        with (
            patch("HelloassoPaheko.sleep"),
            pytest.raises(Exception, match="trop lent"),
        ):
            client._retry_request(func, "u", max_retries=2)


# ---------------------------------------------------------------------------
# Catégories, membres par catégorie, journaux
# ---------------------------------------------------------------------------


class TestCategoriesEtJournaux:
    def test_get_member_categories(self):
        """Vérifie : les catégories viennent de /user/categories."""
        client = fabrique_client_paheko()
        client.client = ClientHttpFaux([ReponseFausse(200, [1, 2])])
        assert client.get_member_categories() == [1, 2]

    def test_get_members_by_category(self):
        """Vérifie : les utilisateurs viennent de /user/category/<id>.json."""
        client = fabrique_client_paheko()
        client.client = ClientHttpFaux([ReponseFausse(200, [{"nom": "Alice"}])])
        assert client.get_members_by_category(3) == [{"nom": "Alice"}]

    def test_get_all_users_by_categories_agrege(self):
        """Vérifie : les utilisateurs de toutes les catégories sont agrégés."""
        client = fabrique_client_paheko()
        reponses = [
            ReponseFausse(200, [1, None, 2]),  # catégories
            ReponseFausse(200, [{"nom": "A"}]),
            ReponseFausse(200, [{"nom": "B"}]),
        ]
        client.client = ClientHttpFaux(reponses)
        assert client.get_all_users_by_categories() == [{"nom": "A"}, {"nom": "B"}]

    def test_get_journal_for_account(self):
        """Vérifie : le journal est demandé pour l'année et le compte."""
        client = fabrique_client_paheko()
        client.client = ClientHttpFaux([ReponseFausse(200, [{"ligne": 1}])])
        journal = client.get_journal_for_account(2026, "512")
        assert journal == [{"ligne": 1}]
        assert "accounting/years/2026/account/journal" in client.client.appels[0][1]

    def test_get_journals_for_account_all_years(self):
        """Vérifie : les journaux de toutes les années sont regroupés par année."""
        client = fabrique_client_paheko()
        client.get_accounting_years = lambda: [
            {"id": 1, "label": "2026", "start_date": "s", "end_date": "e"},
            {"id": 2, "label": "2025", "start_date": "s", "end_date": "e"},
        ]
        client.get_journal_for_account = lambda annee, compte: [f"j{annee}"]
        journals = client.get_journals_for_account_all_years("512")
        assert journals == [
            {"year_id": 1, "journal_entries": ["j1"]},
            {"year_id": 2, "journal_entries": ["j2"]},
        ]


# ---------------------------------------------------------------------------
# Transactions comptables
# ---------------------------------------------------------------------------


class TestTransactions:
    def test_transaction_exists_vrai(self):
        """Vérifie : la référence trouvée par LIKE renvoie True."""
        client = fabrique_client_paheko()
        client.get_sql = lambda *a, **k: {"results": [{"count": 2}]}
        assert client.transaction_exists("OFX-1") is True

    def test_transaction_exists_faux(self):
        """Vérifie : aucune occurrence de la référence renvoie False."""
        client = fabrique_client_paheko()
        client.get_sql = lambda *a, **k: {"results": [{"count": 0}]}
        assert client.transaction_exists("OFX-1") is False

    def test_transaction_exists_erreur_renvoie_faux(self):
        """Vérifie : une erreur SQL n'interrompt pas le traitement."""
        client = fabrique_client_paheko()

        def get_sql(*a, **k):
            raise RuntimeError("base injoignable")

        client.get_sql = get_sql
        assert client.transaction_exists("OFX-1") is False

    def test_update_transaction_reference_succes(self):
        """Vérifie : la référence est ajoutée et True renvoyé."""
        client = fabrique_client_paheko()
        requetes = []

        def get_sql(requete, **kwargs):
            requetes.append(requete)
            return {"results": []}

        client.get_sql = get_sql
        assert client.update_transaction_reference(10, "OFX-2") is True
        assert "acc_transactions" in requetes[0]

    def test_update_transaction_reference_erreur(self):
        """Vérifie : une erreur SQL renvoie False sans lever."""
        client = fabrique_client_paheko()

        def get_sql(*a, **k):
            raise RuntimeError("lecture seule")

        client.get_sql = get_sql
        assert client.update_transaction_reference(10, "OFX-2") is False

    def test_update_transaction_label_et_reference_echappe(self):
        """Vérifie : apostrophes du libellé échappées, True renvoyé."""
        client = fabrique_client_paheko()
        requetes = []

        def get_sql(requete, **kwargs):
            requetes.append(requete)
            return {"results": []}

        client.get_sql = get_sql
        ok = client.update_transaction_label_and_reference(
            10, "L'été", "OFX-3", "d'hiver"
        )
        assert ok is True
        assert "L''été" in requetes[0]

    def test_create_transaction_succes(self):
        """Vérifie : la transaction est créée et le JSON renvoyé."""
        client = fabrique_client_paheko()
        client.client = ClientHttpFaux([ReponseFausse(200, {"id": 7})])
        resultat = client.create_transaction({"reference": "OFX-4"})
        assert resultat == {"id": 7}

    def test_create_transaction_erreur_renvoie_none(self):
        """Vérifie : en cas d'erreur HTTP, None est renvoyé sans lever."""
        client = fabrique_client_paheko()
        client.client = ClientHttpFaux([ReponseFausse(500, None)])
        assert client.create_transaction({"reference": "OFX-4"}) is None


# ---------------------------------------------------------------------------
# get_sql : mode API et mode direct sur SQLite
# ---------------------------------------------------------------------------


class TestGetSql:
    def test_mode_api(self):
        """Vérifie : le mode api passe par POST /sql/ et lève sur erreur."""
        client = fabrique_client_paheko()
        client.client = ClientHttpFaux([ReponseFausse(200, {"results": [{"n": 1}]})])
        resultat = client.get_sql("SELECT 1")
        assert resultat == {"results": [{"n": 1}]}
        assert client.client.appels[0][1].endswith("/sql/")

    def test_mode_direct_select(self, tmp_path: Path):
        """Vérifie : SELECT en mode direct renvoie les lignes en dictionnaires."""
        base = tmp_path / "paheko.sqlite"
        fabrique_base_sqlite(base)
        client = fabrique_client_paheko()
        client.db_path = str(base)
        resultat = client.get_sql("SELECT id, label FROM acc_years", mode="direct")
        assert resultat["results"][0]["label"] == "Exercice transcende 2026"

    def test_mode_direct_fetch_one(self, tmp_path: Path):
        """Vérifie : fetch_mode='one' renvoie au plus une ligne."""
        base = tmp_path / "paheko.sqlite"
        fabrique_base_sqlite(base)
        client = fabrique_client_paheko()
        client.db_path = str(base)
        resultat = client.get_sql(
            "SELECT id FROM acc_years", mode="direct", fetch_mode="one"
        )
        assert len(resultat["results"]) == 1

    def test_mode_direct_insert_committe(self, tmp_path: Path):
        """Vérifie : un INSERT est committé et lisible juste après."""
        base = tmp_path / "paheko.sqlite"
        fabrique_base_sqlite(base)
        client = fabrique_client_paheko()
        client.db_path = str(base)
        with patch("HelloassoPaheko.sleep"):
            client.get_sql(
                "INSERT INTO acc_transactions (id, reference, label, notes) "
                "VALUES (11, 'OFX-9', 'Test', '')",
                mode="direct",
            )
        count = client.get_sql(
            "SELECT COUNT(*) AS n FROM acc_transactions", mode="direct"
        )
        assert count["results"][0]["n"] == 2

    def test_mode_direct_base_absente_leve(self, tmp_path: Path):
        """Vérifie : une base inexistante lève FileNotFoundError."""
        client = fabrique_client_paheko()
        client.db_path = str(tmp_path / "absente.sqlite")
        with pytest.raises(FileNotFoundError):
            client.get_sql("SELECT 1", mode="direct")

    def test_get_table_names(self, tmp_path: Path):
        """Vérifie : les noms de tables viennent de sqlite_master."""
        base = tmp_path / "paheko.sqlite"
        fabrique_base_sqlite(base)
        client = fabrique_client_paheko()
        client.db_path = str(base)
        with patch("HelloassoPaheko.sleep"):
            noms = client.get_table_names()
        assert "acc_years" in noms

    def test_get_table_definition(self, tmp_path: Path):
        """Vérifie : PRAGMA table_info renvoie les colonnes."""
        base = tmp_path / "paheko.sqlite"
        fabrique_base_sqlite(base)
        client = fabrique_client_paheko()
        client.db_path = str(base)
        with patch("HelloassoPaheko.sleep"):
            definition = client.get_table_definition("acc_years")
        colonnes = {colonne["name"] for colonne in definition}
        assert "label" in colonnes

    def test_get_accounting_years(self, tmp_path: Path):
        """Vérifie : les années comptables sont lues en mode direct."""
        base = tmp_path / "paheko.sqlite"
        fabrique_base_sqlite(base)
        client = fabrique_client_paheko()
        client.db_path = str(base)
        with patch("HelloassoPaheko.sleep"):
            annees = client.get_accounting_years()
        assert annees[0]["id"] == 1
        assert annees[0]["label"] == "Exercice transcende 2026"

    def test_get_current_year_id_avec_slug(self, tmp_path: Path):
        """Vérifie : l'année correspondant à la date et au slug est choisie."""
        client = fabrique_client_paheko()
        client.get_accounting_years = lambda: [
            {
                "id": 1,
                "label": "Exercice transcende 2026",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
            {
                "id": 2,
                "label": "Exercice 2025",
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
            },
        ]
        assert client.get_current_year_id("2026-06-15", "transcende") == 1

    def test_get_current_year_id_datetime_conscient(self, tmp_path: Path):
        """Vérifie : une date avec fuseau horaire est convertie en naïve."""
        client = fabrique_client_paheko()
        client.get_accounting_years = lambda: [
            {
                "id": 1,
                "label": None,
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        ]
        date_consciente = datetime(2026, 6, 15, tzinfo=UTC)
        assert client.get_current_year_id(date_consciente, "x") == 1

    def test_get_current_year_id_aucune_correspondance(self):
        """Vérifie : aucune année ne correspond lève ValueError."""
        client = fabrique_client_paheko()
        client.get_accounting_years = lambda: [
            {
                "id": 1,
                "label": "Exercice 2025",
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
            },
        ]
        with pytest.raises(ValueError, match="Aucune année comptable"):
            client.get_current_year_id("2026-06-15", "transcende")

    def test_download_sqlite_backup(self, tmp_path: Path):
        """Vérifie : la sauvegarde SQLite horodatée est écrite dans le répertoire prévu."""
        client = fabrique_client_paheko()
        client.client = ClientHttpFaux()
        client.backup_sqlite_path = str(tmp_path)
        client.download_sqlite_backup()
        ecrits = list(tmp_path.glob("backup_sqlite_*.sqlite"))
        assert len(ecrits) == 1
        assert ecrits[0].read_bytes() == b"donnees-de-sauvegarde"

    def test_download_zip_backup(self, tmp_path: Path):
        """Vérifie : la sauvegarde ZIP horodatée est écrite dans le répertoire prévu."""
        client = fabrique_client_paheko()
        client.client = ClientHttpFaux()
        client.backup_zip_path = str(tmp_path)
        client.download_zip_backup()
        ecrits = list(tmp_path.glob("backup_files_*.zip"))
        assert len(ecrits) == 1
        assert ecrits[0].read_bytes() == b"donnees-de-sauvegarde"


# ---------------------------------------------------------------------------
# HelloAssoHTTPClient : jetons OAuth2 et requêtes
# ---------------------------------------------------------------------------


class TestHelloAssoJetons:
    def test_convert_to_datetime_et_extract_year(self):
        """Vérifie : conversion ISO en datetime puis extraction de l'année."""
        client = fabrique_client_helloasso()
        date = client.convert_to_datetime("2026-10-31T10:00:00")
        assert client.extract_year(date) == 2026

    def test_load_token_fichier_valide(self, tmp_path: Path):
        """Vérifie : le jeton est lu depuis le fichier JSON."""
        client = fabrique_client_helloasso()
        jeton = {"access_token": "a", "expires_in": 3600, "created_at": 1.0}
        client.token_file = str(tmp_path / "token.json")
        Path(client.token_file).write_text(json.dumps(jeton), encoding="utf-8")
        assert client.load_token() == jeton

    def test_load_token_absent_ou_invalide(self, tmp_path: Path):
        """Vérifie : fichier absent ou JSON invalide renvoie None."""
        client = fabrique_client_helloasso()
        client.token_file = str(tmp_path / "absent.json")
        assert client.load_token() is None
        Path(client.token_file).write_text("{invalide", encoding="utf-8")
        assert client.load_token() is None

    def test_save_token_ecrit_en_0600(self, tmp_path: Path):
        """Vérifie : la sauvegarde ajoute created_at et verrouille à 0600."""
        client = fabrique_client_helloasso()
        client.token_file = str(tmp_path / "sous" / "token.json")
        client.token = {"access_token": "a", "expires_in": 3600}
        client.save_token()
        contenu = json.loads(Path(client.token_file).read_text(encoding="utf-8"))
        assert contenu["created_at"] > 0
        assert Path(client.token_file).stat().st_mode & 0o777 == 0o600

    def test_is_token_expired(self):
        """Vérifie : expiration calculée depuis created_at et expires_in."""
        client = fabrique_client_helloasso()
        client.token = {"created_at": 1000.0, "expires_in": 10}
        assert client.is_token_expired() is True
        client.token = {
            "created_at": datetime.now().timestamp(),
            "expires_in": 3600,
        }
        assert client.is_token_expired() is False
        client.token = None
        assert client.is_token_expired() is True

    def test_get_access_token_via_oauth2(self, tmp_path: Path):
        """Vérifie : le jeton client_credentials est récupéré puis sauvegardé."""
        client = fabrique_client_helloasso()
        client.token_file = str(tmp_path / "token.json")
        sessions = []

        class SessionFausse:
            def __init__(self, client_id, client_secret):
                pass

            def fetch_token(self, url, grant_type):
                sessions.append((url, grant_type))
                return {"access_token": "nouveau", "expires_in": 3600}

        with patch.object(HelloassoPaheko, "OAuth2Session", SessionFausse):
            client.get_access_token()
        assert client.token["access_token"] == "nouveau"
        assert sessions == [
            ("https://oauth.helloasso.example/token", "client_credentials")
        ]
        assert Path(client.token_file).exists()

    def test_refresh_access_token(self, tmp_path: Path):
        """Vérifie : le refresh_token est utilisé pour renouveler le jeton."""
        client = fabrique_client_helloasso()
        client.token_file = str(tmp_path / "token.json")
        client.token = {"refresh_token": "r1", "expires_in": 3600}

        class SessionFausse:
            def __init__(self, client_id, client_secret):
                pass

            def refresh_token(self, url, refresh_token):
                assert refresh_token == "r1"
                return {"access_token": "r2", "expires_in": 3600}

        with patch.object(HelloassoPaheko, "OAuth2Session", SessionFausse):
            client.refresh_access_token()
        assert client.token["access_token"] == "r2"

    def test_ensure_valid_token_renouvelle(self, tmp_path: Path):
        """Vérifie : un jeton expiré déclenche le renouvellement et le header."""
        client = fabrique_client_helloasso()
        client.token_file = str(tmp_path / "token.json")
        client.token = {"created_at": 1000.0, "expires_in": 10, "access_token": "vieux"}
        renouvellements = []

        def get_access_token():
            renouvellements.append(1)
            client.token = {
                "access_token": "neuf",
                "expires_in": 3600,
                "created_at": datetime.now().timestamp(),
            }

        client.get_access_token = get_access_token
        client._ensure_valid_token()
        assert renouvellements
        assert client.headers["Authorization"] == "Bearer neuf"

    def test_ensure_valid_token_jeton_valide_ne_fait_rien(self):
        """Vérifie : un jeton valide ne déclenche aucun renouvellement."""
        client = fabrique_client_helloasso()
        client.token = {
            "access_token": "ok",
            "expires_in": 3600,
            "created_at": datetime.now().timestamp(),
        }
        client.headers = {"Authorization": "Bearer ok"}
        client._ensure_valid_token()
        assert client.headers == {"Authorization": "Bearer ok"}

    def test_make_request_fusionne_les_entetes(self):
        """Vérifie : les en-têtes d'authentification sont fusionnés aux nôtres."""
        client = fabrique_client_helloasso()
        client.headers = {"Authorization": "Bearer ok"}
        client._ensure_valid_token = lambda: None
        requetes = []

        def requete_fausse(methode, url, **kwargs):
            requetes.append((methode, url, kwargs))
            return ReponseFausse(200, {"ok": 1})

        with patch.object(sys.modules["httpx"], "request", requete_fausse):
            reponse = client._make_request("GET", "https://api/x", headers={"X": "1"})
        assert reponse.status_code == 200
        assert requetes[0][2]["headers"] == {"Authorization": "Bearer ok", "X": "1"}

    def test_make_request_renouvelle_sur_401(self):
        """Vérifie : un 401 déclenche un renouvellement puis une seconde requête."""
        client = fabrique_client_helloasso()
        client.headers = {"Authorization": "Bearer vieux"}
        client._ensure_valid_token = lambda: None
        statuts = iter([401, 200])

        def requete_fausse(methode, url, **kwargs):
            return ReponseFausse(next(statuts), {"ok": 1})

        renouvellements = []

        def get_access_token():
            renouvellements.append(1)
            client.headers = {"Authorization": "Bearer neuf"}

        client.get_access_token = get_access_token
        with patch.object(sys.modules["httpx"], "request", requete_fausse):
            reponse = client._make_request("GET", "https://api/x")
        assert reponse.status_code == 200
        assert renouvellements

    def test_get_organizations(self):
        """Vérifie : les organisations viennent de /users/me/organizations."""
        client = fabrique_client_helloasso()
        client._make_request = lambda m, u: ReponseFausse(200, [{"slug": "transcende"}])
        assert client.get_organizations() == [{"slug": "transcende"}]


# ---------------------------------------------------------------------------
# _required_env : variable obligatoire
# ---------------------------------------------------------------------------


class TestRequiredEnv:
    def test_variable_presente(self, monkeypatch):
        """Vérifie : la variable d'environnement est renvoyée telle quelle."""
        monkeypatch.setenv("TEST_HP_CLE", "valeur")
        assert HelloassoPaheko._required_env("TEST_HP_CLE") == "valeur"

    def test_variable_absente_leve(self, monkeypatch):
        """Vérifie : une variable absente lève RuntimeError avec son nom."""
        monkeypatch.delenv("TEST_HP_ABSENTE", raising=False)
        with pytest.raises(ValueError, match="TEST_HP_ABSENTE"):
            HelloassoPaheko._required_env("TEST_HP_ABSENTE")
