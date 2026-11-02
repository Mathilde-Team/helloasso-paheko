# SPDX-License-Identifier: GPL-3.0-or-later
"""Palier 4 de couverture (issue #5) — utilitaires Paheko et téléchargeur PDF.

Complète le palier 3 (client ``HelloAssoHTTPClient``, PR #23) sans le
recouvrir :

- ``HelloAssoPDFDownloader`` : navigateur Playwright simulé (connexion,
  refus, noms de fichiers, signature PDF, commande complète, fermeture) ;
- ``PahekoAPIClient`` : initialisation (réelle et simulation, repli sur
  l'année précédente), libellé de service, extraction des champs
  personnalisés, catégories (API et ``config.toml``), catégorie selon payeur
  et adhésion, ``ensure_user_from_item_exists``, inscription aux activités
  (``services_users`` sur SQLite réelle), structure de la base avec clés
  étrangères, années comptables et journaux, création et mise à jour de
  membre, sauvegardes en erreur.

Les doublures de modules (httpx, playwright, authlib…) sont posées par
``test_palier2_clients`` : on réutilise ses fabriques.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from test_palier2_clients import ReponseFausse, fabrique_client_paheko

import HelloassoPaheko
from HelloassoPaheko import PahekoAPIClient


# ---------------------------------------------------------------------------
# Aides
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def sans_pause(monkeypatch):
    """Neutralise les ``sleep`` (pause après écriture SQLite, attente initiale)."""
    monkeypatch.setattr(HelloassoPaheko, "sleep", lambda *_: None)


CONFIG_TOML = """
[helloasso]
base_url = "https://api.helloasso.example/v5"
authorize_url = "https://auth.helloasso.example/authorize"
token_url = "https://api.helloasso.example/oauth2/token"
redirect_uri = "https://exemple.fr/retour"
org_slug = "transcende"

[files]
token_helloasso = "{token}"

[member_categories.actifs]
id = 1
name = "Membres actifs"

[member_categories.anciens]
id = 2
name = "Anciens membres"

[member_categories.non_adherents]
id = 4
name = "Non adhérents"

[member_categories.payeurs]
id = 5
name = "Payeurs non adhérents"
"""


@pytest.fixture
def config_toml(tmp_path, monkeypatch):
    """Écrit un config.toml de test dans un répertoire courant temporaire."""
    monkeypatch.chdir(tmp_path)
    chemin = tmp_path / "config.toml"
    chemin.write_text(CONFIG_TOML.replace("{token}", str(tmp_path / "jeton.json")))
    return chemin


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# PahekoAPIClient : libellé de service et champs personnalisés
# ---------------------------------------------------------------------------


class TestLibelleService:
    @pytest.mark.parametrize(
        ("formulaire", "attendu"),
        [
            (
                {"formType": "Membership", "formSlug": "adhesion", "date": "2026-02-03T10:00"},
                "Adhésion - adhesion 2026",
            ),
            ({"formType": "Membership", "formSlug": "adhesion"}, "Adhésion - adhesion"),
            ({"formType": "Event", "formSlug": "gala"}, "Événement - gala"),
            ({}, "Unknown - "),
        ],
    )
    def test_libelle(self, formulaire, attendu):
        assert fabrique_client_paheko()._get_serviceLabel(formulaire) == attendu


class TestChampsPersonnalises:
    def test_sans_champs(self):
        assert PahekoAPIClient.extract_all_from_custom_fields({}) == {}

    def test_champs_non_liste(self):
        assert PahekoAPIClient.extract_all_from_custom_fields({"customFields": "x"}) == {}

    def test_extraction_complete(self):
        champs = [
            "ignoré",
            {"name": "Email", "answer": " Alice@@Exemple.FR "},
            {"name": "Autre", "answer": "bob@exemple.fr"},
            {"name": "Profession", "answer": "Infirmière"},
            {"name": "Réseau", "answer": "Grand Est"},
            {"name": "Adresse professionnelle", "answer": "1 rue Haute"},
            {"name": "Code postal", "answer": "67500"},
            {"name": "Pays", "answer": "France"},
            {"name": "Ville", "answer": "Haguenau"},
            {"name": "Téléphone", "answer": "0612345678"},
            {"name": "Mobile", "answer": "0700000000"},
            {"name": "Vide", "answer": ""},
            {"name": "Nombre", "answer": 3},
            {"name": None, "answer": "sans nom"},
        ]
        resultat = PahekoAPIClient.extract_all_from_custom_fields({"customFields": champs})
        assert resultat == {
            "email": "alice@exemple.fr",
            "profession": "Infirmière",
            "network": "Grand Est",
            "adresse": "1 rue Haute",
            "code_postal": "67500",
            "country": "France",
            "ville": "Haguenau",
            "telephone": "0612345678",
        }

    def test_email_par_motif(self):
        champs = [{"name": "Contact", "answer": "carole@exemple.fr"}]
        assert PahekoAPIClient.extract_all_from_custom_fields({"customFields": champs}) == {
            "email": "carole@exemple.fr"
        }


# ---------------------------------------------------------------------------
# Catégories
# ---------------------------------------------------------------------------


CATEGORIES = {
    1: {"id": 1, "name": "Membres actifs"},
    3: {"id": 3, "name": "Administrateurs"},
}


class TestCategories:
    @pytest.fixture
    def client(self):
        client = fabrique_client_paheko()
        client.get_member_categories = lambda: CATEGORIES
        return client

    def test_nom_par_id(self, client):
        assert client.get_category_name_by_id("3") == "Administrateurs"
        assert client.get_category_name_by_id(99) is None

    def test_id_par_nom(self, client):
        assert client.get_category_id_by_name("Membres actifs") == 1
        assert client.get_category_id_by_name("Inconnue") is None

    def test_depuis_config(self, client, config_toml):
        client.config_path = str(config_toml)
        assert client.get_category_name_by_id_from_config("2") == "Anciens membres"
        assert client.get_category_name_by_id_from_config(99) is None
        assert client.get_category_id_by_name_from_config("Non adhérents") == 4
        assert client.get_category_id_by_name_from_config("Inconnue") is None

    def test_depuis_config_illisible(self, client, tmp_path):
        client.config_path = str(tmp_path / "absent.toml")
        assert client.get_category_name_by_id_from_config(1) is None
        assert client.get_category_id_by_name_from_config("Membres actifs") is None

    @pytest.mark.parametrize(
        ("payeur", "adhesion", "annee", "attendu"),
        [
            (True, False, None, "Payeurs non adhérents"),
            (False, False, None, "Non adhérents"),
            (False, True, 7, "Membres actifs"),
            (True, True, 6, "Anciens membres"),
        ],
    )
    def test_categorie_selon_payeur_et_adhesion(
        self, client, config_toml, payeur, adhesion, annee, attendu
    ):
        client.config_path = str(config_toml)
        client.organization_slug = "transcende"
        client.IDCURRENTYEAR = 7
        client.get_current_year_id = lambda date, slug: annee
        assert client.category_by_payer_and_hasmembership(payeur, adhesion, "2026-01-01") == attendu


# ---------------------------------------------------------------------------
# Inscription aux activités (services_users)
# ---------------------------------------------------------------------------


def base_services(chemin: Path) -> None:
    """Base SQLite avec services_users et une contrainte d'unicité."""
    conn = sqlite3.connect(chemin)
    conn.executescript(
        """
        CREATE TABLE services (id INTEGER PRIMARY KEY, label TEXT);
        CREATE TABLE services_users (
            id INTEGER PRIMARY KEY,
            id_user INTEGER, id_service INTEGER REFERENCES services(id) ON DELETE CASCADE,
            id_fee INTEGER NOT NULL, paid INTEGER, expected_amount INTEGER,
            date TEXT, expiry_date TEXT,
            UNIQUE (id_user, id_service, id_fee));
        INSERT INTO services VALUES (1, 'Gala');
        """
    )
    conn.commit()
    conn.close()


class TestInscriptionActivite:
    @pytest.fixture
    def client(self, tmp_path):
        chemin = tmp_path / "paheko.sqlite"
        base_services(chemin)
        client = fabrique_client_paheko()
        client.db_path = str(chemin)
        return client

    def lignes(self, client):
        conn = sqlite3.connect(client.db_path)
        lignes = conn.execute(
            "SELECT id_user, paid, expected_amount, date, expiry_date FROM services_users"
        ).fetchall()
        conn.close()
        return lignes

    def test_donnees_manquantes(self, client):
        client.attach_users_to_activity(None, 1, 1, 0, "2026-01-01")
        assert self.lignes(client) == []

    def test_inscription_payante(self, client):
        client.attach_users_to_activity(1, 7, 2, 1500, "2026-01-01", "2026-12-31")
        assert self.lignes(client) == [(7, 1, 1500, "2026-01-01", "2026-12-31")]

    def test_inscription_gratuite_et_doublon_ignore(self, client):
        client.attach_users_to_activity(1, 7, 2, 0, "2026-01-01")
        client.attach_users_to_activity(1, 7, 2, 0, "2026-01-01")
        assert self.lignes(client) == [(7, 0, None, "2026-01-01", "NULL")]

    def test_sans_date(self, client):
        client.attach_users_to_activity(1, 8, 2, None, "")
        assert self.lignes(client) == [(8, 0, None, "NULL", "NULL")]

    def test_contrainte_unique(self, client):
        client.attach_users_to_activity(1, 7, 2, 100, "2026-01-01")
        client.attach_users_to_activity(1, 7, 2, 100, "2026-02-01")
        assert len(self.lignes(client)) == 1

    def test_verification_echouee(self, client):
        reponses = iter([{"results": []}, {}, {"results": []}, {}])
        client.get_sql = lambda *a, **k: next(reponses)
        client.attach_users_to_activity(1, 7, 2, 100, "2026-01-01")

    @pytest.mark.parametrize(
        "erreur", [sqlite3.IntegrityError("NOT NULL constraint failed"), RuntimeError("panne")]
    )
    def test_autres_erreurs(self, client, erreur):
        def echoue(*a, **k):
            raise erreur

        client.get_sql = echoue
        client.attach_users_to_activity(1, 7, 2, 100, "2026-01-01")


# ---------------------------------------------------------------------------
# Structure de la base, années comptables et journaux
# ---------------------------------------------------------------------------


class TestStructureBase:
    @pytest.fixture
    def client(self, tmp_path):
        chemin = tmp_path / "paheko.sqlite"
        base_services(chemin)
        client = fabrique_client_paheko()
        client.db_path = str(chemin)
        return client

    def test_noms_et_definitions(self, client):
        assert client.get_table_names() == ["services", "services_users"]
        colonnes = [c["name"] for c in client.get_table_definition("services")]
        assert colonnes == ["id", "label"]
        assert set(client.get_all_tables_and_definitions()) == {"services", "services_users"}

    def test_structure_avec_cles_etrangeres(self, client):
        structure = client.get_tablenames_and_rows()
        colonnes = structure["services_users"]["columns"]
        assert colonnes["id"] == "INTEGER (PRIMARY KEY)"
        assert colonnes["id_fee"] == "INTEGER (NOT NULL)"
        assert colonnes["id_service"] == "INTEGER | FK → services.id (ON DELETE CASCADE)"
        assert (
            structure["services_users"]["foreign_keys"]["id_service"][0]["references_table"]
            == "services"
        )
        assert "foreign_keys" not in structure["services"]

    def test_cle_etrangere_sur_colonne_inconnue_et_on_update(self, client):
        reponses = {
            "SELECT name": {"results": [{"name": "t"}]},
            "PRAGMA table_info": {"results": [{"name": "a", "type": "TEXT"}]},
            "PRAGMA foreign_key_list": {
                "results": [
                    {"from": "a", "table": "u", "to": "id", "on_update": "CASCADE"},
                    {"from": "b", "table": "v", "to": "id"},
                ]
            },
        }
        client.get_sql = lambda sql, mode: next(v for k, v in reponses.items() if k in sql)
        structure = client.get_tablenames_and_rows(mode="api")
        assert structure["t"]["columns"]["a"] == "TEXT | FK → u.id (ON UPDATE CASCADE)"
        assert set(structure["t"]["foreign_keys"]) == {"a", "b"}

    def test_sans_resultat(self, client):
        client.get_sql = lambda *a, **k: {}
        assert client.get_tablenames_and_rows() == {}

    def test_table_illisible_ou_en_erreur(self, client):
        def get_sql(sql, mode):
            if sql.startswith("SELECT"):
                return {"results": [{"name": "vide"}, {"name": "cassee"}]}
            if "vide" in sql:
                return {}
            raise RuntimeError("PRAGMA refusé")

        client.get_sql = get_sql
        assert client.get_tablenames_and_rows() == {
            "vide": {},
            "cassee": {"error": "PRAGMA refusé"},
        }

    def test_erreur_globale(self, client):
        def echoue(*a, **k):
            raise RuntimeError("base absente")

        client.get_sql = echoue
        assert client.get_tablenames_and_rows() == {}


class TestAnneesComptables:
    @pytest.fixture
    def client(self):
        client = fabrique_client_paheko()
        client.get_accounting_years = lambda: [
            {
                "id": 1,
                "label": "Exercice transcende 2026",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
            {"id": 2, "label": None, "start_date": "2025-01-01", "end_date": "2025-12-31"},
            {"id": None, "label": "", "start_date": "2020-01-01", "end_date": "2020-12-31"},
        ]
        return client

    def test_annee_avec_fuseau(self, client):
        assert client.get_current_year_id("2026-05-01T10:00:00+02:00", "Transcende") == 1

    def test_annee_sans_label(self, client):
        assert client.get_current_year_id(datetime(2025, 5, 1), "transcende") == 2

    def test_aucune_annee(self, client):
        with pytest.raises(ValueError, match="Aucune année"):
            client.get_current_year_id(datetime.now() + timedelta(days=3650), "transcende")

    def test_journaux_toutes_annees(self, client):
        client.get_journal_for_account = lambda year_id, account: [f"{account}-{year_id}"]
        assert client.get_journals_for_account_all_years("512") == [
            {"year_id": 1, "journal_entries": ["512-1"]},
            {"year_id": 2, "journal_entries": ["512-2"]},
        ]


# ---------------------------------------------------------------------------
# HelloAssoPDFDownloader (navigateur simulé)
# ---------------------------------------------------------------------------


class PageFausse:
    """Page Playwright simulée : navigation, formulaire et téléchargement."""

    def __init__(self, url_apres_login="https://www.helloasso.com/", contenu=b"%PDF-1.7"):
        self.url = ""
        self.url_apres_login = url_apres_login
        self.contenu = contenu
        self.actions: list[tuple] = []

    def goto(self, url, **kwargs):
        self.actions.append(("goto", url))
        self.url = url

    def wait_for_timeout(self, ms):
        self.actions.append(("attente", ms))

    def wait_for_selector(self, selecteur, **kwargs):
        self.actions.append(("selecteur", selecteur))

    def fill(self, selecteur, valeur):
        self.actions.append(("saisie", selecteur, valeur))

    def press(self, selecteur, touche):
        self.actions.append(("touche", touche))
        self.url = self.url_apres_login

    def wait_for_load_state(self, *args, **kwargs):
        self.actions.append(("chargement",))

    def expect_download(self, **kwargs):
        page = self

        class Attente:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            @property
            def value(self):
                class Telechargement:
                    def save_as(self, chemin):
                        Path(chemin).write_bytes(page.contenu)

                return Telechargement()

        return Attente()

    def evaluate(self, script):
        self.actions.append(("script", script))


class PlaywrightFaux:
    """``sync_playwright()`` simulé : chromium → navigateur → contexte → page."""

    def __init__(self, page):
        self.page = page
        self.arrete = False
        self.ferme = False

    def start(self):
        return self

    def stop(self):
        self.arrete = True

    @property
    def chromium(self):
        return self

    def launch(self, headless):
        return self

    def new_context(self, **kwargs):
        return self

    def new_page(self):
        return self.page

    def close(self):
        self.ferme = True


class TestTelechargeurPdf:
    @pytest.fixture
    def navigateur(self, monkeypatch):
        page = PageFausse()
        faux = PlaywrightFaux(page)
        monkeypatch.setattr(HelloassoPaheko, "sync_playwright", lambda: faux)
        return faux

    def telechargeur(self):
        return HelloassoPaheko.HelloAssoPDFDownloader("moi@exemple.fr", "mdp", "transcende")

    def test_demarrage_unique(self, navigateur):
        telechargeur = self.telechargeur()
        telechargeur.start_browser()
        telechargeur.start_browser()
        assert telechargeur.page is navigateur.page

    def test_connexion_puis_page_admin(self, navigateur):
        telechargeur = self.telechargeur()
        telechargeur.start_browser()
        telechargeur.login()
        telechargeur.login()
        assert telechargeur.is_logged_in
        assert ("saisie", '[data-testid="login-input-email"]', "moi@exemple.fr") in (
            navigateur.page.actions
        )
        assert navigateur.page.url == "https://admin.helloasso.com/transcende/accueil"

    def test_connexion_refusee(self, navigateur):
        navigateur.page.url_apres_login = "https://auth.autre.example/erreur"
        telechargeur = self.telechargeur()
        telechargeur.start_browser()
        with pytest.raises(Exception, match="Échec de la connexion"):
            telechargeur.login()

    @pytest.mark.parametrize(
        ("url", "debut"),
        [
            ("https://h.example/t?ticketId=T1&ag=1", "ticket_T1_(Éloïse_De_La_Tour)"),
            ("https://h.example/c?cardId=C1&ag=1", "carte_membre_C1_(Éloïse_De_La_Tour)"),
            ("https://h.example/paiement-attestation/R1", "recu_paiement_R1_(Éloïse_De_La_Tour)"),
            ("https://h.example/autre", "document_"),
        ],
    )
    def test_telechargement(self, navigateur, tmp_path, url, debut):
        chemin = self.telechargeur().download_pdf(url, "Éloïse!", "De La Tour", str(tmp_path))
        assert Path(chemin).name.startswith(debut)
        assert Path(chemin).read_bytes() == b"%PDF-1.7"

    def test_deja_present(self, navigateur, tmp_path):
        (tmp_path / "ticket_T1_(A_B).pdf").write_bytes(b"ancien")
        chemin = self.telechargeur().download_pdf("x?ticketId=T1", "A", "B", str(tmp_path))
        assert Path(chemin).read_bytes() == b"ancien"
        assert navigateur.page.actions == []

    def test_signature_invalide(self, navigateur, tmp_path):
        navigateur.page.contenu = b"<html>"
        assert self.telechargeur().download_pdf("x?ticketId=T", "A", "B", str(tmp_path)) is None

    def test_erreur_de_telechargement(self, navigateur, tmp_path):
        navigateur.page.url_apres_login = "https://ailleurs.example/"
        assert self.telechargeur().download_pdf("x?ticketId=T", "A", "B", str(tmp_path)) is None

    def test_commande_complete(self, tmp_path):
        telechargeur = self.telechargeur()
        demandes = []

        def faux(url, prenom, nom, dossier):
            demandes.append((url, prenom, nom))
            return f"{dossier}/{url}"

        telechargeur.download_pdf = faux
        resultat = telechargeur.download_order_pdfs(
            {
                "payer": {"firstName": "Paul", "lastName": "Payeur"},
                "items": [
                    {
                        "id": 1,
                        "ticketUrl": "t1",
                        "membershipCardUrl": "c1",
                        "user": {"firstName": "Ugo", "lastName": "Usager"},
                    },
                    {"ticketUrl": "t2"},
                    {},
                ],
                "payments": [{"id": 9, "paymentReceiptUrl": "r9"}, {}],
            },
            str(tmp_path),
        )
        assert set(resultat) == {
            "ticket_1",
            "membership_card_1",
            "ticket_unknown",
            "payment_receipt_9",
        }
        assert ("t2", "Paul", "Payeur") in demandes
        assert ("c1", "Ugo", "Usager") in demandes

    def test_fermeture(self, navigateur):
        telechargeur = self.telechargeur()
        telechargeur.close()
        telechargeur.start_browser()
        telechargeur.close()
        assert navigateur.ferme and navigateur.arrete


# ---------------------------------------------------------------------------
# PahekoAPIClient : initialisation
# ---------------------------------------------------------------------------


class TestInitPaheko:
    @pytest.fixture
    def environnement(self, config_toml, tmp_path, monkeypatch):
        monkeypatch.setenv("PAHEKO_USERNAME", "admin")
        monkeypatch.setenv("PAHEKO_PASSWORD", "secret")
        with open(config_toml, "a") as f:
            f.write(
                '\n[paheko]\nbase_url = "https://paheko.exemple/api"\n'
                f'data_base = "{tmp_path / "base.sqlite"}"\n'
                f'\n[backups]\nsqlite = "{tmp_path / "sauvegardes" / "sqlite"}"\n'
                f'zip = "{tmp_path / "sauvegardes" / "zip"}"\n'
            )
        clients = []

        class ClientHttpx:
            def __init__(self, **kwargs):
                clients.append(kwargs)

        monkeypatch.setattr(HelloassoPaheko.httpx, "Client", ClientHttpx, raising=False)
        monkeypatch.setattr(HelloassoPaheko.httpx, "Timeout", lambda *a, **k: None, raising=False)
        appels = []
        for nom, valeur in {
            "download_sqlite_backup": None,
            "download_zip_backup": None,
            "get_all_users_by_categories": ["utilisateurs"],
            "get_journals_for_account_all_years": ["journaux"],
        }.items():
            monkeypatch.setattr(
                PahekoAPIClient,
                nom,
                lambda self, *a, _n=nom, _v=valeur, **k: appels.append(_n) or _v,
            )
        return clients, appels

    def test_init_reel(self, environnement, tmp_path, monkeypatch):
        clients, appels = environnement
        monkeypatch.setattr(PahekoAPIClient, "get_current_year_id", lambda self, d, slug: 7)
        client = PahekoAPIClient()
        assert client.base_url == "https://paheko.exemple/api"
        assert client.organization_slug == "transcende"
        assert client.users == ["utilisateurs"] and client.journaux == ["journaux"]
        assert client.IDCURRENTYEAR == 7
        assert (tmp_path / "sauvegardes" / "zip").is_dir()
        assert len(clients) == 2 and clients[0]["auth"] == ("admin", "secret")
        assert appels[:2] == ["download_sqlite_backup", "download_zip_backup"]

    def test_init_simulation_et_annee_precedente(self, environnement, monkeypatch):
        clients, _ = environnement
        annees = iter([None, 6])
        monkeypatch.setattr(
            PahekoAPIClient, "get_current_year_id", lambda self, d, slug: next(annees)
        )
        client = PahekoAPIClient(mode_simulation=True)
        assert client.mode_simulation is True
        assert client.IDCURRENTYEAR == 6
        assert len(clients) == 1


# ---------------------------------------------------------------------------
# PahekoAPIClient.ensure_user_from_item_exists
# ---------------------------------------------------------------------------


class TestMembreDepuisItem:
    @pytest.fixture
    def client(self, config_toml):
        client = fabrique_client_paheko()
        client.config_path = str(config_toml)
        client.organization_slug = "transcende"
        client.IDCURRENTYEAR = 7
        client.get_current_year_id = lambda date, slug: 7
        client.recus = []

        def ensure_member_exists(donnees):
            client.recus.append(donnees)
            return {"id": 1, **donnees}

        client.ensure_member_exists = ensure_member_exists
        return client

    def test_payeur_avec_champs_personnalises(self, client):
        payeur = {
            "firstName": "paul",
            "lastName": "payeur",
            "email": "paul@exemple.fr",
            "customFields": [{"name": "Téléphone", "answer": "0611"}],
        }
        membre = client.ensure_user_from_item_exists(
            {"firstName": "ugo"}, payeur, has_membership=True, date_inscription="2026-01-01"
        )
        assert membre["nom"] == "PAYEUR Paul (paul@exemple.fr)"
        assert membre["telephone"] == "0611"
        assert membre["ville"] == "Besançon"
        assert membre["category"] == "Membres actifs"

    def test_utilisateur_avec_email_des_champs(self, client):
        item = {
            "firstName": "ugo",
            "lastName": "usager",
            "customFields": [
                {"name": "Email", "answer": "Ugo@Exemple.fr"},
                {"name": "Adresse professionnelle", "answer": "2 rue Basse"},
            ],
        }
        membre = client.ensure_user_from_item_exists(item, {"email": "paul@exemple.fr"})
        assert membre["email"] == "ugo@exemple.fr"
        assert membre["nom"] == "USAGER Ugo (ugo@exemple.fr)"
        assert membre["adresse"] == "2 rue Basse"
        assert membre["id_category"] == 4
        assert membre["category"] == "Non adhérents"

    def test_utilisateur_sans_email_et_nom_impose(self, client):
        item = {"firstName": "ugo", "lastName": "usager", "nom": "Nom imposé"}
        membre = client.ensure_user_from_item_exists(
            item, {"email": " Paul@Exemple.fr "}, has_membership=True
        )
        assert membre["email"] == "paul@exemple.fr"
        assert membre["nom"] == "Nom imposé"
        assert membre["id_category"] == 1

    def test_erreur_de_creation(self, client):
        def echoue(donnees):
            raise RuntimeError("API indisponible")

        client.ensure_member_exists = echoue
        assert client.ensure_user_from_item_exists({}, {"email": "p@e.fr"}) is None


# ---------------------------------------------------------------------------
# Compléments : création et mise à jour de membre, sauvegardes, 401 persistant
# ---------------------------------------------------------------------------


class ClientPost:
    """Client HTTP Paheko minimal : POST et flux en erreur."""

    def __init__(self, reponse=None):
        self.reponse = reponse
        self.appels: list[tuple] = []

    def post(self, url, json, headers):
        self.appels.append((url, json))
        return self.reponse

    def stream(self, method, url):
        raise HelloassoPaheko.httpx.HTTPError("sauvegarde indisponible")


class TestMembresEtSauvegardes:
    def test_create_member(self):
        client = fabrique_client_paheko()
        client.client = ClientPost(ReponseFausse(200, {"id": 12}))
        assert client.create_member({"nom": "MARTIN Alice"}) == {"id": 12}
        assert client.client.appels[0][0].endswith("/user/new")

    def test_update_member(self):
        client = fabrique_client_paheko()
        client.client = ClientPost(ReponseFausse(200, {"id": 12}))
        assert client.update_member(12, {"nom": "X"}, "Membres actifs") == {"id": 12}
        assert client.client.appels[0][0].endswith("/user/12")

    def test_update_member_administrateur_refuse(self):
        client = fabrique_client_paheko()
        client.client = ClientPost()
        with pytest.raises(ValueError, match="administrateurs"):
            client.update_member(1, {}, "Administrateur")
        assert client.client.appels == []

    @pytest.mark.parametrize("methode", ["download_sqlite_backup", "download_zip_backup"])
    def test_sauvegarde_en_erreur(self, methode):
        client = fabrique_client_paheko()
        client.client = ClientPost()
        with pytest.raises(HelloassoPaheko.httpx.HTTPError):
            getattr(client, methode)()
