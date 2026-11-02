# SPDX-License-Identifier: GPL-3.0-or-later
"""Palier 4 de couverture (issue #5) — PahekoAPIClient et téléchargeur PDF.

Quatrième palier : le reste de ``PahekoAPIClient`` (construction, libellés
d'activité, recherche par téléphone, création/mise à jour de membres,
inscription aux activités, extraction des champs personnalisés, catégories
depuis l'API et depuis ``config.toml``, catégorie selon payeur/cotisation,
création de membre depuis un item, structure de la base, sauvegardes en
erreur) et ``HelloAssoPDFDownloader`` (Playwright remplacé par une doublure).

Aucun appel réseau ni navigateur : ``get_sql``, le client HTTP et Playwright
sont des doublures enregistreuses.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from test_palier2_clients import ClientHttpFaux, ReponseFausse, fabrique_client_paheko

import HelloassoPaheko
from HelloassoPaheko import HelloAssoPDFDownloader, PahekoAPIClient

HTTPError = sys.modules["httpx"].HTTPError
HTTPStatusError = sys.modules["httpx"].HTTPStatusError


def sql_scripte(client, reponses):
    """Remplace get_sql : enregistre les requêtes, rejoue les réponses."""
    client.requetes = []

    def get_sql(query, params=None, mode="api", fetch_mode="all"):
        client.requetes.append(query)
        reponse = reponses.pop(0) if reponses else {"results": []}
        if isinstance(reponse, Exception):
            raise reponse
        return reponse

    client.get_sql = get_sql
    return client


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


CONFIG = """
[paheko]
base_url = "https://paheko.exemple/api"
data_base = "base.sqlite"

[helloasso]
org_slug = "asso"

[backups]
sqlite = "sauvegardes/sqlite"
zip = "sauvegardes/zip"
"""


@pytest.fixture
def environnement_paheko(tmp_path, monkeypatch):
    """config.toml, identifiants et appels initiaux neutralisés."""
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PAHEKO_USERNAME", "utilisateur")
    monkeypatch.setenv("PAHEKO_PASSWORD", "motdepasse")
    clients = []

    def client_http(**kwargs):
        clients.append(kwargs)
        return ClientHttpFaux()

    monkeypatch.setattr(HelloassoPaheko.httpx, "Client", client_http, raising=False)
    monkeypatch.setattr(HelloassoPaheko, "sleep", lambda s: None)
    appels = []
    for nom in ("download_sqlite_backup", "download_zip_backup"):
        monkeypatch.setattr(PahekoAPIClient, nom, lambda self, n=nom: appels.append(n))
    monkeypatch.setattr(PahekoAPIClient, "get_all_users_by_categories", lambda self: [{"id": 1}])
    monkeypatch.setattr(
        PahekoAPIClient, "get_journals_for_account_all_years", lambda self, account: [account]
    )
    return {"clients": clients, "appels": appels}


@pytest.mark.parametrize("simulation, nb_clients", [(False, 2), (True, 1)])
def test_init(environnement_paheko, monkeypatch, simulation, nb_clients):
    annees = iter([None, 7])  # exercice courant absent : repli sur l'année précédente
    monkeypatch.setattr(PahekoAPIClient, "get_current_year_id", lambda self, d, s: next(annees))

    client = PahekoAPIClient(mode_simulation=simulation)

    assert client.base_url == "https://paheko.exemple/api"
    assert client.organization_slug == "asso"
    assert client.db_path == "base.sqlite"
    assert Path("sauvegardes/sqlite").is_dir() and Path("sauvegardes/zip").is_dir()
    assert environnement_paheko["appels"] == ["download_sqlite_backup", "download_zip_backup"]
    assert client.users == [{"id": 1}]
    assert client.journaux == ["512HELLOASSO"]
    assert client.activities_cache == {}
    assert client.IDCURRENTYEAR == 7
    assert len(environnement_paheko["clients"]) == nb_clients
    assert environnement_paheko["clients"][0]["auth"] == ("utilisateur", "motdepasse")


def test_init_exige_les_identifiants(environnement_paheko, monkeypatch):
    monkeypatch.delenv("PAHEKO_PASSWORD")
    with pytest.raises(ValueError, match="PAHEKO_PASSWORD"):
        PahekoAPIClient()


# ---------------------------------------------------------------------------
# Libellés, membres
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "formulaire, attendu",
    [
        (
            {"formType": "Membership", "formSlug": "adh", "date": "2026-03-01T10:00"},
            "Adhésion - adh 2026",
        ),
        ({"formType": "Membership", "formSlug": "adh"}, "Adhésion - adh"),
        ({"formType": "Event", "formSlug": "fete"}, "Événement - fete"),
        ({"formType": "CrowdFunding", "formSlug": "c"}, "Financement participatif - c"),
        ({"formType": "Checkout", "formSlug": "k"}, "Paiement - k"),
        ({}, "Unknown - "),
    ],
)
def test_get_service_label(formulaire, attendu):
    assert fabrique_client_paheko()._get_serviceLabel(formulaire) == attendu


@pytest.mark.parametrize("telephone", [None, "   ", "pas de chiffre"])
def test_get_member_by_phone_numero_inexploitable(telephone):
    client = fabrique_client_paheko()
    client.get_all_users_by_categories = lambda: [{"telephone": "0102030405"}]
    assert client.get_member_by_phone(telephone) is None


def test_get_member_by_phone_normalise():
    client = fabrique_client_paheko()
    client.get_all_users_by_categories = lambda: [
        {"id": 1},
        {"id": 2, "telephone": "01 02 03 04 05"},
    ]
    assert client.get_member_by_phone("01.02.03.04.05")["id"] == 2
    assert client.get_member_by_phone("06 00 00 00 00") is None


def test_create_member():
    client = fabrique_client_paheko()
    client.client = ClientHttpFaux([ReponseFausse(200, {"id": 9})])
    assert client.create_member({"nom": "X"}) == {"id": 9}
    methode, url, options = client.client.appels[0]
    assert (methode, url) == ("POST", "https://paheko.exemple/api/user/new")
    assert options["json"] == {"nom": "X"}


def test_create_member_erreur_http():
    client = fabrique_client_paheko()
    client.client = ClientHttpFaux([ReponseFausse(400, {})])
    with pytest.raises(HTTPStatusError):
        client.create_member({"nom": "X"})


def test_update_member():
    client = fabrique_client_paheko()
    client.client = ClientHttpFaux([ReponseFausse(200, {"id": 4})])
    assert client.update_member(4, {"nom": "Y"}, existing_category="Membres actifs") == {"id": 4}
    assert client.client.appels[0][1] == "https://paheko.exemple/api/user/4"


def test_update_member_refuse_les_administrateurs():
    client = fabrique_client_paheko()
    client.client = ClientHttpFaux()
    with pytest.raises(ValueError):
        client.update_member(4, {"nom": "Y"}, existing_category="Administrateurs")
    assert client.client.appels == []


# ---------------------------------------------------------------------------
# Inscription aux activités
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("activite, membre, tarif", [(None, 1, 1), (1, None, 1), (1, 1, 0)])
def test_attach_donnees_manquantes(activite, membre, tarif):
    client = sql_scripte(fabrique_client_paheko(), [])
    client.attach_users_to_activity(activite, membre, tarif, 1500, "2026-01-02")
    assert client.requetes == []


def test_attach_deja_inscrit():
    client = sql_scripte(fabrique_client_paheko(), [{"results": [{"id": 1}]}])
    client.attach_users_to_activity(3, 4, 5, 1500, "2026-01-02")
    assert len(client.requetes) == 1


def test_attach_payant_avec_expiration():
    client = sql_scripte(
        fabrique_client_paheko(),
        [{"results": []}, {"results": []}, {"results": [{"id": 1}]}, {"results": [{"id": 1}]}],
    )
    client.attach_users_to_activity(3, 4, 5, 1500, "2026-01-02", "2026-12-31")
    insertion = client.requetes[1]
    assert "INSERT INTO services_users" in insertion
    assert "VALUES (4, 3, 5, 1, 1500, '2026-01-02', '2026-12-31')" in insertion


def test_attach_gratuit_sans_dates_verification_en_echec():
    client = sql_scripte(fabrique_client_paheko(), [{"results": []}, {}, {"results": []}, {}])
    client.attach_users_to_activity(3, 4, 5, 0, "")
    assert "VALUES (4, 3, 5, 0, NULL, 'NULL', 'NULL')" in client.requetes[1]


@pytest.mark.parametrize(
    "erreur",
    [
        sqlite3.IntegrityError("UNIQUE constraint failed: services_users"),
        sqlite3.IntegrityError("NOT NULL constraint failed"),
        RuntimeError("panne"),
    ],
)
def test_attach_erreurs_journalisees(erreur):
    client = sql_scripte(fabrique_client_paheko(), [erreur])
    client.attach_users_to_activity(3, 4, 5, 1500, "2026-01-02")  # ne lève pas


# ---------------------------------------------------------------------------
# Champs personnalisés HelloAsso
# ---------------------------------------------------------------------------


def champs(*paires):
    return {"customFields": [{"name": n, "answer": r} for n, r in paires]}


def test_extract_sans_champs():
    assert PahekoAPIClient.extract_all_from_custom_fields({}) == {}
    assert PahekoAPIClient.extract_all_from_custom_fields({"customFields": "texte"}) == {}


def test_extract_tous_les_champs():
    donnees = champs(
        ("Email", "Alice@@Exemple.fr"),
        ("Profession", "Infirmière"),
        ("Réseau", "Local"),
        ("Rue", "1 rue Haute"),
        ("Code postal", "25000"),
        ("Pays", "France"),
        ("Ville", "Besançon"),
        ("Téléphone", "0102030405"),
    )
    donnees["customFields"] += [{"name": "vide", "answer": ""}, "pas un dict", {"answer": 3}]
    resultat = PahekoAPIClient.extract_all_from_custom_fields(donnees)
    assert resultat == {
        "email": "alice@exemple.fr",
        "profession": "Infirmière",
        "network": "Local",
        "adresse": "1 rue Haute",
        "code_postal": "25000",
        "country": "France",
        "ville": "Besançon",
        "telephone": "0102030405",
    }


def test_extract_email_reconnu_par_sa_valeur():
    resultat = PahekoAPIClient.extract_all_from_custom_fields(champs(("Contact", "bob@exemple.fr")))
    assert resultat == {"email": "bob@exemple.fr"}


def test_extract_premiere_valeur_conservee():
    resultat = PahekoAPIClient.extract_all_from_custom_fields(
        champs(("Téléphone", "01"), ("Portable", "06"), ("Profession", "A"), ("Emploi", "B"))
    )
    assert resultat["telephone"] == "01"
    assert resultat["profession"] == "A"


# ---------------------------------------------------------------------------
# Catégories
# ---------------------------------------------------------------------------


CATEGORIES = {"1": {"id": 1, "name": "Membres actifs"}, "3": {"id": 3, "name": "Admins"}}


def test_categories_depuis_l_api():
    client = fabrique_client_paheko()
    client.get_member_categories = lambda: CATEGORIES
    assert client.get_category_name_by_id(3) == "Admins"
    assert client.get_category_name_by_id(9) is None
    assert client.get_category_id_by_name("Membres actifs") == 1
    assert client.get_category_id_by_name("Inconnue") is None


CONFIG_CATEGORIES = """
[member_categories.actifs]
id = 1
name = "Membres actifs"

[member_categories.anciens]
id = 2
name = "Anciens membres"
"""


@pytest.fixture
def client_config(tmp_path):
    chemin = tmp_path / "config.toml"
    chemin.write_text(CONFIG_CATEGORIES, encoding="utf-8")
    client = fabrique_client_paheko()
    client.config_path = str(chemin)
    return client


def test_categories_depuis_la_config(client_config):
    assert client_config.get_category_name_by_id_from_config("2") == "Anciens membres"
    assert client_config.get_category_name_by_id_from_config(9) is None
    assert client_config.get_category_id_by_name_from_config("Membres actifs") == 1
    assert client_config.get_category_id_by_name_from_config("Inconnue") is None


def test_categories_config_illisible(tmp_path):
    client = fabrique_client_paheko()
    client.config_path = str(tmp_path / "absent.toml")
    assert client.get_category_name_by_id_from_config(1) is None
    assert client.get_category_id_by_name_from_config("x") is None


@pytest.mark.parametrize(
    "payeur, cotisation, annee, attendu",
    [
        (True, False, None, 5),
        (False, False, None, 4),
        (False, True, 7, 1),  # exercice courant : membre actif
        (True, True, 6, 2),  # exercice passé : ancien membre
    ],
)
def test_category_by_payer_and_hasmembership(payeur, cotisation, annee, attendu):
    client = fabrique_client_paheko()
    client.organization_slug = "asso"
    client.IDCURRENTYEAR = 7
    client.get_current_year_id = lambda date, slug: annee
    client.get_category_name_by_id_from_config = lambda identifiant: identifiant
    assert client.category_by_payer_and_hasmembership(payeur, cotisation, "2026-01-02") == attendu


# ---------------------------------------------------------------------------
# Création d'un membre depuis un item
# ---------------------------------------------------------------------------


@pytest.fixture
def client_item():
    client = fabrique_client_paheko()
    client.recus = []

    def ensure(donnees):
        client.recus.append(donnees)
        return {"id": 1, **donnees}

    client.ensure_member_exists = ensure
    client.category_by_payer_and_hasmembership = lambda **kw: "categorie-payeur"
    client.get_category_name_by_id_from_config = lambda identifiant: f"categorie-{identifiant}"
    return client


def test_item_avec_champs_du_payeur(client_item):
    payeur = {
        "email": "alice@exemple.fr",
        "firstName": "alice",
        "lastName": "martin",
        "customFields": [{"name": "Ville", "answer": "Dole"}],
    }
    resultat = client_item.ensure_user_from_item_exists({"firstName": "bob"}, payeur, True)
    donnees = client_item.recus[0]
    assert resultat["id"] == 1
    assert donnees["nom"] == "MARTIN Alice (alice@exemple.fr)"
    assert donnees["ville"] == "Dole"
    assert donnees["code_postal"] == "25000"
    assert donnees["category"] == "categorie-payeur"


def test_item_beneficiaire_avec_son_email(client_item):
    item = {
        "firstName": "bob",
        "lastName": "durand",
        "customFields": [{"name": "Email", "answer": "Bob@Exemple.fr"}],
        "phone": "0607080910",
    }
    client_item.ensure_user_from_item_exists(item, {"email": "alice@exemple.fr"}, True)
    donnees = client_item.recus[0]
    assert donnees["email"] == "bob@exemple.fr"
    assert donnees["nom"] == "DURAND Bob (bob@exemple.fr)"
    assert donnees["id_category"] == 1
    assert donnees["telephone"] == "0607080910"
    assert donnees["category"] == "categorie-4"


def test_item_sans_email_repli_sur_le_payeur_et_nom_impose(client_item):
    item = {"firstName": "bob", "lastName": "durand", "nom": "Nom imposé"}
    client_item.ensure_user_from_item_exists(item, {"email": " Alice@Exemple.fr "})
    donnees = client_item.recus[0]
    assert donnees["email"] == "alice@exemple.fr"
    assert donnees["nom"] == "Nom imposé"
    assert donnees["id_category"] == 4


def test_item_erreur_de_creation(client_item):
    def echec(donnees):
        raise RuntimeError("panne")

    client_item.ensure_member_exists = echec
    assert client_item.ensure_user_from_item_exists({}, {"email": "a@b.fr"}) is None


# ---------------------------------------------------------------------------
# Structure de la base
# ---------------------------------------------------------------------------


def test_tables_et_definitions():
    client = sql_scripte(
        fabrique_client_paheko(),
        [
            {"results": [{"name": "users"}, {"name": "services"}]},
            {"results": [{"name": "id"}]},
            {"results": [{"name": "label"}]},
        ],
    )
    assert client.get_all_tables_and_definitions() == {
        "users": [{"name": "id"}],
        "services": [{"name": "label"}],
    }
    assert client.requetes[1] == "PRAGMA table_info(users);"


def test_get_tablenames_and_rows():
    client = sql_scripte(
        fabrique_client_paheko(),
        [
            {"results": [{"name": "services_users"}, {"name": "vide"}, {"name": "panne"}]},
            {
                "results": [
                    {"name": "id", "type": "INTEGER", "pk": 1},
                    {"name": "id_user", "type": "INTEGER", "notnull": 1},
                    {"name": "note", "type": "TEXT"},
                ]
            },
            {
                "results": [
                    {
                        "from": "id_user",
                        "table": "users",
                        "to": "id",
                        "on_delete": "CASCADE",
                        "on_update": "SET NULL",
                    },
                    {"from": "absente", "table": "x", "to": "y"},
                ]
            },
            {},  # table sans résultat exploitable
            RuntimeError("table illisible"),
        ],
    )

    structure = client.get_tablenames_and_rows(mode="api")

    colonnes = structure["services_users"]["columns"]
    assert colonnes["id"] == "INTEGER (PRIMARY KEY)"
    assert colonnes["id_user"] == (
        "INTEGER (NOT NULL) | FK → users.id (ON DELETE CASCADE) (ON UPDATE SET NULL)"
    )
    assert colonnes["note"] == "TEXT"
    assert set(structure["services_users"]["foreign_keys"]) == {"id_user", "absente"}
    assert structure["vide"] == {}
    assert structure["panne"] == {"error": "table illisible"}


@pytest.mark.parametrize("reponse", [None, {}, RuntimeError("panne")])
def test_get_tablenames_and_rows_sans_tables(reponse):
    client = sql_scripte(fabrique_client_paheko(), [reponse])
    assert client.get_tablenames_and_rows() == {}


# ---------------------------------------------------------------------------
# Sauvegardes en erreur
# ---------------------------------------------------------------------------


class FluxEnErreur:
    def stream(self, method, url):
        raise HTTPError("réseau")


@pytest.mark.parametrize("methode", ["download_sqlite_backup", "download_zip_backup"])
def test_sauvegarde_erreur_relancee(methode):
    client = fabrique_client_paheko()
    client.client = FluxEnErreur()
    with pytest.raises(HTTPError):
        getattr(client, methode)()


# ---------------------------------------------------------------------------
# Téléchargeur PDF (Playwright)
# ---------------------------------------------------------------------------


class PageFausse:
    """Doublure de page Playwright : navigation et téléchargement."""

    def __init__(self, url_apres_connexion="https://www.helloasso.com/", contenu=b"%PDF-1.7"):
        self.url = "about:blank"
        self.url_apres_connexion = url_apres_connexion
        self.contenu = contenu
        self.actions: list[tuple] = []
        self.erreur = None

    def goto(self, url, **kwargs):
        self.actions.append(("goto", url))

    def wait_for_timeout(self, ms):
        pass

    def wait_for_selector(self, selecteur, **kwargs):
        pass

    def fill(self, selecteur, valeur):
        self.actions.append(("fill", selecteur, valeur))

    def press(self, selecteur, touche):
        self.url = self.url_apres_connexion

    def wait_for_load_state(self, etat, **kwargs):
        pass

    def evaluate(self, script):
        self.actions.append(("evaluate", script))
        if self.erreur:
            raise self.erreur

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


class PlaywrightFaux:
    def __init__(self, page):
        self.page = page
        self.arrete = False
        self.ferme = False
        self.chromium = self

    def start(self):
        return self

    def stop(self):
        self.arrete = True

    def launch(self, headless):
        return self

    def new_context(self, **kwargs):
        return self

    def new_page(self):
        return self.page

    def close(self):
        self.ferme = True


@pytest.fixture
def playwright_faux(monkeypatch):
    page = PageFausse()
    faux = PlaywrightFaux(page)
    monkeypatch.setattr(HelloassoPaheko, "sync_playwright", lambda: faux)
    return faux


def test_pdf_connexion_puis_telechargement(tmp_path, playwright_faux):
    telechargeur = HelloAssoPDFDownloader("moi@exemple.fr", "secret", org_slug="asso")
    url = "https://www.helloasso.com/ticket?ticketId=T1&x=1"

    chemin = telechargeur.download_pdf(url, "Jean-Luc", "D'Arc", str(tmp_path))

    assert chemin == str(tmp_path / "ticket_T1_(Jean-Luc_DArc).pdf")
    actions = playwright_faux.page.actions
    assert ("fill", '[data-testid="login-input-email"]', "moi@exemple.fr") in actions
    assert ("goto", "https://admin.helloasso.com/asso/accueil") in actions
    assert telechargeur.is_logged_in

    # Navigateur déjà lancé, déjà connecté, fichier déjà présent
    telechargeur.start_browser()
    telechargeur.login()
    assert telechargeur.download_pdf(url, "Jean-Luc", "D'Arc", str(tmp_path)) == chemin

    telechargeur.close()
    assert playwright_faux.ferme and playwright_faux.arrete


@pytest.mark.parametrize(
    "url, debut",
    [
        ("https://h/carte?cardId=C1&x", "carte_membre_C1_"),
        ("https://h/paiement-attestation/R1", "recu_paiement_R1_"),
        ("https://h/autre", "document_"),
    ],
)
def test_pdf_nommage(tmp_path, playwright_faux, url, debut):
    telechargeur = HelloAssoPDFDownloader("u", "p")
    chemin = telechargeur.download_pdf(url, "Ana", "Lee", str(tmp_path))
    assert Path(chemin).name.startswith(debut)


def test_pdf_signature_invalide(tmp_path, playwright_faux):
    playwright_faux.page.contenu = b"<html>"
    assert (
        HelloAssoPDFDownloader("u", "p").download_pdf("https://h/x", "A", "B", str(tmp_path))
        is None
    )


def test_pdf_connexion_refusee(tmp_path, playwright_faux):
    playwright_faux.page.url_apres_connexion = "https://ailleurs.exemple/"
    assert (
        HelloAssoPDFDownloader("u", "p").download_pdf("https://h/x", "A", "B", str(tmp_path))
        is None
    )


def test_pdf_erreur_de_telechargement(tmp_path, playwright_faux):
    playwright_faux.page.erreur = RuntimeError("délai dépassé")
    assert (
        HelloAssoPDFDownloader("u", "p").download_pdf("https://h/x", "A", "B", str(tmp_path))
        is None
    )


def test_pdf_close_sans_navigateur():
    HelloAssoPDFDownloader("u", "p").close()  # ne lève pas


def test_download_order_pdfs_noms_par_beneficiaire(monkeypatch):
    telechargeur = HelloAssoPDFDownloader("u", "p")
    appels = []

    def telecharger(url, prenom, nom, dossier):
        appels.append((url, prenom, nom))
        return f"/pdf/{url}"

    monkeypatch.setattr(telechargeur, "download_pdf", telecharger)
    commande = {
        "payer": {"firstName": "Alice", "lastName": "Martin"},
        "items": [
            {"id": 1, "ticketUrl": "t1", "user": {"firstName": "Bob", "lastName": "Durand"}},
            {"id": 2, "membershipCardUrl": "c2"},
            {},
        ],
        "payments": [{"id": 9, "paymentReceiptUrl": "r9"}, {}],
    }

    resultat = telechargeur.download_order_pdfs(commande)

    assert resultat == {
        "ticket_1": "/pdf/t1",
        "membership_card_2": "/pdf/c2",
        "payment_receipt_9": "/pdf/r9",
    }
    assert appels == [("t1", "Bob", "Durand"), ("c2", "Alice", "Martin"), ("r9", "Alice", "Martin")]
