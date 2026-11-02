# SPDX-License-Identifier: GPL-3.0-or-later
"""Palier 3 de couverture (issue #5) — client HelloAsso hors OAuth2.

Troisième palier de couverture de ``HelloassoPaheko.py`` : tout
``HelloAssoHTTPClient`` sauf la logique de jeton (couverte au palier 2) —
construction depuis ``config.toml`` et l'environnement, organisations,
paiements, commandes et items paginés, consolidation globale + par
formulaire, extraction des membres et des commandes, virements
(cash-outs), détails, téléchargement des PDF authentifié, vérification de
synchronisation d'une commande, formulaires et leurs détails.

Aucun appel réseau : ``_make_request``, ``httpx.Client`` et
``requests.get`` sont remplacés par des doublures enregistreuses.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Les doublures de modules (httpx, requests, authlib, playwright…) sont posées
# à l'import du palier 2 ; on réutilise ses fabriques pour rester cohérent.
from test_palier2_clients import ReponseFausse, fabrique_client_helloasso

import HelloassoPaheko
from HelloassoPaheko import HelloAssoHTTPClient

HTTPError = sys.modules["httpx"].HTTPError
HTTPStatusError = sys.modules["httpx"].HTTPStatusError


# ---------------------------------------------------------------------------
# Aides
# ---------------------------------------------------------------------------


class RequetesEnregistrees:
    """Remplace ``_make_request`` : rejoue des réponses et garde les appels."""

    def __init__(self, *reponses):
        self.reponses = list(reponses)
        self.appels: list[tuple] = []

    def __call__(self, method, url, **kwargs):
        self.appels.append((method, url, kwargs))
        reponse = self.reponses.pop(0)
        if isinstance(reponse, Exception):
            raise reponse
        return reponse


def page(donnees, jeton=""):
    """Réponse paginée HelloAsso (``data`` + ``pagination``)."""
    return ReponseFausse(200, {"data": donnees, "pagination": {"continuationToken": jeton}})


def client_avec(*reponses) -> tuple[HelloAssoHTTPClient, RequetesEnregistrees]:
    """Client HelloAsso dont ``_make_request`` rejoue ``reponses``."""
    client = fabrique_client_helloasso()
    client.config = {"helloasso": {"username": "u", "password": "p", "org_slug": "repli"}}
    requetes = RequetesEnregistrees(*reponses)
    client._make_request = requetes
    return client, requetes


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


CONFIG_TOML = """
[helloasso]
base_url = "https://api.helloasso.example/v5"
authorize_url = "https://auth.helloasso.example/authorize"
token_url = "https://api.helloasso.example/oauth2/token"
redirect_uri = "https://exemple/retour"

[files]
token_helloasso = "jeton-test.json"
"""


@pytest.fixture
def environnement_helloasso(tmp_path, monkeypatch):
    """Répertoire de travail avec config.toml et variables d'environnement."""
    (tmp_path / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for nom, valeur in {
        "HELLOASSO_API_KEY": "cle",
        "HELLOASSO_API_SECRET": "secret",
        "HELLOASSO_USERNAME": "utilisateur",
        "HELLOASSO_PASSWORD": "motdepasse",
    }.items():
        monkeypatch.setenv(nom, valeur)
    return tmp_path


def test_init_reutilise_un_jeton_valide(environnement_helloasso, monkeypatch):
    """Jeton chargé et non expiré : pas de nouvel appel OAuth."""
    monkeypatch.setattr(HelloAssoHTTPClient, "load_token", lambda self: {"access_token": "abc"})
    monkeypatch.setattr(HelloAssoHTTPClient, "is_token_expired", lambda self: False)
    appels = []
    monkeypatch.setattr(HelloAssoHTTPClient, "get_access_token", lambda self: appels.append(1))

    client = HelloAssoHTTPClient()

    assert appels == []
    assert client.base_url == "https://api.helloasso.example/v5"
    assert client.token_file == "jeton-test.json"
    assert client.config["helloasso"]["username"] == "utilisateur"
    assert client.headers == {"Authorization": "Bearer abc"}


def test_init_obtient_un_jeton_sans_fichier(environnement_helloasso, monkeypatch):
    """Aucun jeton sur disque : get_access_token est appelé."""
    monkeypatch.setattr(HelloAssoHTTPClient, "load_token", lambda self: None)

    def obtenir(self):
        self.token = {"access_token": "neuf"}

    monkeypatch.setattr(HelloAssoHTTPClient, "get_access_token", obtenir)

    client = HelloAssoHTTPClient()

    assert client.headers == {"Authorization": "Bearer neuf"}


def test_init_exige_les_variables_d_environnement(environnement_helloasso, monkeypatch):
    """Variable manquante : ValueError explicite (aucun secret affiché)."""
    monkeypatch.delenv("HELLOASSO_API_SECRET")
    with pytest.raises(ValueError, match="HELLOASSO_API_SECRET"):
        HelloAssoHTTPClient()


# ---------------------------------------------------------------------------
# Organisations et pagination
# ---------------------------------------------------------------------------


def test_get_organizations():
    client, requetes = client_avec(ReponseFausse(200, [{"organizationSlug": "asso"}]))
    assert client.get_organizations() == [{"organizationSlug": "asso"}]
    assert requetes.appels[0][1] == "https://api.helloasso.example/users/me/organizations"


def test_get_organization_slug():
    client, _ = client_avec(ReponseFausse(200, [{"organizationSlug": "asso"}]))
    assert client.get_organization_slug() == "asso"


@pytest.mark.parametrize("organisations", [[], [{"name": "sans slug"}]])
def test_get_organization_slug_refuse(organisations):
    client, _ = client_avec(ReponseFausse(200, organisations))
    with pytest.raises(ValueError):
        client.get_organization_slug()


@pytest.mark.parametrize(
    "methode, chemin",
    [
        ("get_payments", "payments"),
        ("get_orders", "orders"),
        ("get_items", "items"),
    ],
)
def test_pagination_par_jeton_de_suite(methode, chemin):
    """Suit le jeton de suite, s'arrête quand il est vide ou répété."""
    client, requetes = client_avec(
        page([{"id": 1}], "j1"),
        page([{"id": 2}], "j1"),  # jeton identique au précédent : fin
    )
    assert getattr(client, methode)("asso") == [{"id": 1}, {"id": 2}]
    assert requetes.appels[0][1].endswith(f"/organizations/asso/{chemin}")
    assert requetes.appels[1][2]["params"]["continuationToken"] == "j1"
    assert requetes.appels[0][2]["headers"] == {"Content-Type": "application/json"}


@pytest.mark.parametrize("methode", ["get_payments", "get_orders", "get_items"])
def test_pagination_page_unique(methode):
    client, requetes = client_avec(page([{"id": 1}]))
    assert getattr(client, methode)("asso", page_size=5) == [{"id": 1}]
    assert len(requetes.appels) == 1
    assert requetes.appels[0][2]["params"]["pageSize"] == 5


def test_get_payments_global_parcourt_les_pages():
    client, requetes = client_avec(page([{"id": 1}], "suite"), page([{"id": 2}]))
    assert client.get_payments_global("asso") == [{"id": 1}, {"id": 2}]
    assert [a[2]["params"]["pageIndex"] for a in requetes.appels] == [1, 2]


def test_get_forms_list():
    client, _ = client_avec(ReponseFausse(200, {"data": [{"formSlug": "f"}]}))
    assert client.get_forms_list("asso") == [{"formSlug": "f"}]


def test_get_payments_by_form_pages_puis_page_vide():
    client, requetes = client_avec(page([{"id": 1}], "suite"), page([]))
    assert client.get_payments_by_form("asso", "Event", "fete") == [{"id": 1}]
    assert requetes.appels[0][1].endswith("/forms/Event/fete/payments")


def test_get_payments_by_form_derniere_page():
    client, _ = client_avec(page([{"id": 1}]))
    assert client.get_payments_by_form("asso", "Event", "fete") == [{"id": 1}]


def test_get_payments_by_form_erreur_journalisee():
    client, _ = client_avec(ReponseFausse(500, {}))
    assert client.get_payments_by_form("asso", "Event", "fete") == []


# ---------------------------------------------------------------------------
# Consolidation et extraction
# ---------------------------------------------------------------------------


PAIEMENTS = [
    {
        "id": 10,
        "amount": 1500,
        "payer": {"email": "Alice@Exemple.fr", "firstName": "Alice", "lastName": "Martin"},
        "order": {"id": 100, "date": "2026-01-02", "formSlug": "adh", "formType": "Membership"},
        "items": [{"id": 1000}, {"id": 1001}],
    },
    {
        "id": 11,
        "amount": 500,
        "payer": {"email": "alice@exemple.fr"},
        "order": {"id": 100},
        "items": [{"id": 1001}],
    },
    {"id": 12, "amount": 700, "payer": {}, "order": {}},
]


def test_consolidate_payments_deduplique():
    globaux = [{"id": 1}, {"id": 2}, {"montant": "sans id"}]
    formulaires = [{"id": 2}, {"id": 3}, {}]
    resultat = HelloAssoHTTPClient.consolidate_payments(None, globaux, formulaires)
    assert [p["id"] for p in resultat] == [1, 2, 3]


def test_extract_members_regroupe_par_email_insensible_a_la_casse():
    membres = HelloAssoHTTPClient.extract_members(None, PAIEMENTS)
    assert len(membres) == 1
    assert membres[0]["firstName"] == "Alice"
    assert membres[0]["payments"] == [10, 11]
    assert membres[0]["total_amount"] == 2000
    assert membres[0]["payment_count"] == 2


def test_extract_orders_from_payments_deduplique_les_items():
    commandes = HelloAssoHTTPClient.extract_orders_from_payments(None, PAIEMENTS)
    assert len(commandes) == 1
    assert commandes[0]["payments"] == [10, 11]
    assert commandes[0]["total_amount"] == 2000
    assert [i["id"] for i in commandes[0]["items"]] == [1000, 1001]


def test_consolidate_all_et_consolidate(monkeypatch):
    client = fabrique_client_helloasso()
    monkeypatch.setattr(client, "get_organization_slug", lambda: "asso", raising=False)
    monkeypatch.setattr(client, "get_payments_global", lambda slug: PAIEMENTS[:1], raising=False)
    monkeypatch.setattr(
        client,
        "get_forms_list",
        lambda slug: [
            {"formType": "Event", "formSlug": "fete"},
            {"formType": "Event", "formSlug": "vide"},
            {"formType": None, "formSlug": "ignore"},
        ],
        raising=False,
    )
    par_formulaire = {"fete": PAIEMENTS[1:], "vide": []}
    monkeypatch.setattr(
        client,
        "get_payments_by_form",
        lambda slug, form_type, form_slug: par_formulaire[form_slug],
        raising=False,
    )

    resultat = client.consolidate()

    assert [p["id"] for p in resultat["payments"]] == [10, 11, 12]
    assert len(resultat["members"]) == 1
    assert len(resultat["orders"]) == 1


def test_consolidate_journalise_et_relance(monkeypatch):
    client = fabrique_client_helloasso()

    def echec():
        raise RuntimeError("panne")

    monkeypatch.setattr(client, "consolidate_all", echec, raising=False)
    with pytest.raises(RuntimeError):
        client.consolidate()


def test_get_cash_outs_regroupe_par_virement(monkeypatch):
    client = fabrique_client_helloasso()
    paiements = [
        {
            "id": 1,
            "amount": 1000,
            "cashOutState": "CashedOut",
            "idCashOut": "V1",
            "cashOutDate": "2026-02-01",
            "payer": {"firstName": "Alice", "lastName": "Martin"},
            "order": {"id": 100},
        },
        {"id": 2, "amount": 500, "cashOutState": "CashedOut", "idCashOut": "V1"},
        {"id": 3, "amount": 900, "cashOutState": "CashedOut"},  # sans idCashOut
        {"id": 4, "amount": 800, "cashOutState": "Transfered", "idCashOut": "V2"},
    ]
    monkeypatch.setattr(client, "get_payments", lambda slug, page_size: paiements, raising=False)

    virements = client.get_cash_outs("asso")

    assert len(virements) == 1
    assert virements[0]["total_amount_centimes"] == 1500
    assert virements[0]["payments"][0]["payer_name"] == "Alice Martin"
    assert virements[0]["payments"][1]["order_id"] is None


@pytest.mark.parametrize(
    "methode, chemin",
    [
        ("get_payment_details", "/payments/7"),
        ("get_item_details", "/items/7"),
        ("get_order_details", "/orders/7"),
    ],
)
def test_details(methode, chemin):
    client, requetes = client_avec(ReponseFausse(200, {"id": 7}))
    assert getattr(client, methode)("7") == {"id": 7}
    assert requetes.appels[0][1] == f"https://api.helloasso.example{chemin}"


def test_process_details_et_appel_global(monkeypatch):
    client = fabrique_client_helloasso()
    client.commandes = {}
    client.process_payment_details({"id": 1})
    client.process_item_details({"id": 2})
    client.process_order_details({"id": 3, "payer": {"email": "a@b.fr"}})
    assert client.commandes == {3: {"id": 3, "payer": {"email": "a@b.fr"}}}

    monkeypatch.setattr(
        client,
        "get_organizations",
        lambda: [{"name": "sans slug"}, {"organizationSlug": "asso"}],
        raising=False,
    )
    monkeypatch.setattr(client, "get_payments", lambda slug: [1, 2], raising=False)
    monkeypatch.setattr(client, "get_orders", lambda slug: [3], raising=False)
    monkeypatch.setattr(client, "get_items", lambda slug: [], raising=False)

    client()

    assert (client.payments, client.orders, client.items) == ([1, 2], [3], [])


# ---------------------------------------------------------------------------
# Téléchargement des PDF
# ---------------------------------------------------------------------------


class SessionHttpFausse:
    """Doublure de ``httpx.Client`` : connexion puis GET du PDF."""

    def __init__(self, code_connexion=200, type_contenu="application/pdf", erreur=None):
        self.code_connexion = code_connexion
        self.type_contenu = type_contenu
        self.erreur = erreur
        self.appels: list[tuple] = []

    def __call__(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, data=None):
        self.appels.append(("POST", url))
        if self.erreur:
            raise self.erreur
        return ReponseFausse(self.code_connexion, {})

    def get(self, url):
        self.appels.append(("GET", url))
        reponse = ReponseFausse(200, None)
        reponse.headers = {"content-type": self.type_contenu}
        reponse.content = b"%PDF-1.4"
        return reponse


@pytest.mark.parametrize(
    "url, attendu",
    [
        ("https://h/ticket?ticketId=T1&ag=x", "ticket_T1.pdf"),
        ("https://h/carte-adherent?cardId=C1&ag=x", "carte_membre_C1.pdf"),
        ("https://h/paiement-attestation/R1", "recu_paiement_R1.pdf"),
    ],
)
def test_download_pdf_nomme_d_apres_l_url(tmp_path, monkeypatch, url, attendu):
    session = SessionHttpFausse()
    monkeypatch.setattr(HelloassoPaheko.httpx, "Client", session, raising=False)
    client, _ = client_avec()

    chemin = client.download_pdf_with_auth(url, str(tmp_path / "pdf"))

    assert chemin == str(tmp_path / "pdf" / attendu)
    assert Path(chemin).read_bytes() == b"%PDF-1.4"
    assert session.appels[0] == ("POST", "https://auth.helloasso.com/login")


def test_download_pdf_nom_par_defaut_et_contenu_suspect(tmp_path, monkeypatch):
    monkeypatch.setattr(
        HelloassoPaheko.httpx, "Client", SessionHttpFausse(type_contenu="text/html"), raising=False
    )
    client, _ = client_avec()
    chemin = client.download_pdf_with_auth("https://h/autre", str(tmp_path))
    assert os.path.basename(chemin).startswith("document_")


def test_download_pdf_deja_present(tmp_path, monkeypatch):
    session = SessionHttpFausse()
    monkeypatch.setattr(HelloassoPaheko.httpx, "Client", session, raising=False)
    (tmp_path / "f.pdf").write_bytes(b"ancien")
    client, _ = client_avec()
    assert client.download_pdf_with_auth("https://h/x", str(tmp_path), "f.pdf") == str(
        tmp_path / "f.pdf"
    )
    assert session.appels == []


def test_download_pdf_connexion_refusee(tmp_path, monkeypatch):
    monkeypatch.setattr(
        HelloassoPaheko.httpx, "Client", SessionHttpFausse(code_connexion=403), raising=False
    )
    client, _ = client_avec()
    assert client.download_pdf_with_auth("https://h/x", str(tmp_path), "f.pdf") is None


@pytest.mark.parametrize("erreur", [HTTPError("réseau"), RuntimeError("autre")])
def test_download_pdf_erreurs_journalisees(tmp_path, monkeypatch, erreur):
    monkeypatch.setattr(
        HelloassoPaheko.httpx, "Client", SessionHttpFausse(erreur=erreur), raising=False
    )
    client, _ = client_avec()
    assert client.download_pdf_with_auth("https://h/x", str(tmp_path), "f.pdf") is None


def test_download_order_pdfs(tmp_path, monkeypatch):
    client = fabrique_client_helloasso()
    reponses = {"t1": "/p/t1.pdf", "c1": "/p/c1.pdf", "r1": "/p/r1.pdf", "t2": None, "r2": None}
    monkeypatch.setattr(
        client, "download_pdf_with_auth", lambda url, dossier: reponses[url], raising=False
    )
    commande = {
        "id": 5,
        "items": [{"ticketUrl": "t1", "membershipCardUrl": "c1"}, {"ticketUrl": "t2"}, {}],
        "payments": [{"paymentReceiptUrl": "r1"}, {"paymentReceiptUrl": "r2"}, {}],
    }

    resultat = client.download_order_pdfs(commande, str(tmp_path))

    assert resultat == {
        "tickets": ["/p/t1.pdf"],
        "membership_cards": ["/p/c1.pdf"],
        "payment_receipts": ["/p/r1.pdf"],
    }


def test_download_order_pdfs_sans_id():
    resultat = HelloAssoHTTPClient.download_order_pdfs(None, {})
    assert resultat == {"tickets": [], "membership_cards": [], "payment_receipts": []}


def test_download_order_pdfs_carte_en_echec(tmp_path, monkeypatch):
    client = fabrique_client_helloasso()
    monkeypatch.setattr(client, "download_pdf_with_auth", lambda url, dossier: None, raising=False)
    resultat = client.download_order_pdfs({"id": 6, "items": [{"membershipCardUrl": "c"}]})
    assert resultat["membership_cards"] == []


# ---------------------------------------------------------------------------
# Vérification de synchronisation d'une commande
# ---------------------------------------------------------------------------


class PahekoFaux:
    """Doublure du client Paheko pour is_order_fully_synced."""

    def __init__(self, membres=True, transactions=(), activite=None, annee_erreur=False):
        self.membres = membres
        self.transactions = set(transactions)
        self.activite = activite
        self.annee_erreur = annee_erreur
        self.noms: list[str] = []

    def get_member_by_name(self, nom):
        self.noms.append(nom)
        return {"id": 1} if self.membres else None

    def get_current_year_id(self, date_commande, org_slug):
        if self.annee_erreur:
            raise RuntimeError("exercice introuvable")
        assert isinstance(date_commande, datetime)
        self.org_slug = org_slug
        return 3

    def transaction_exists(self, reference, year_id):
        return reference in self.transactions

    def _get_serviceLabel(self, commande):
        return f"{commande['formType']} {commande['formSlug']}"

    def find_activity(self, libelle):
        self.libelle = libelle
        return self.activite


COMMANDE = {
    "id": 100,
    "date": "2026-01-02T10:00:00",
    "payer": {"email": "alice@exemple.fr", "firstName": "alice", "lastName": "Martin"},
    "items": [{"id": 1000}],
    "formSlug": "adh",
    "formType": "Membership",
}


def client_synchro(monkeypatch, slug="asso"):
    client = fabrique_client_helloasso()
    client.config = {"helloasso": {"org_slug": "repli"}}
    if slug is None:

        def echec():
            raise RuntimeError("pas d'organisation")

        monkeypatch.setattr(client, "get_organization_slug", echec, raising=False)
    else:
        monkeypatch.setattr(client, "get_organization_slug", lambda: slug, raising=False)
    return client


def test_commande_entierement_synchronisee(monkeypatch):
    client = client_synchro(monkeypatch)
    paheko = PahekoFaux(transactions={"HelloAsso 100", "HelloAsso 1000"}, activite={"id": 9})
    assert client.is_order_fully_synced(COMMANDE, paheko) is True
    assert paheko.noms == ["MARTIN Alice (alice@exemple.fr)"]
    assert paheko.org_slug == "asso"


def test_commande_sans_formulaire_et_slug_de_repli(monkeypatch):
    client = client_synchro(monkeypatch, slug=None)
    paheko = PahekoFaux(transactions={"HelloAsso 100"})
    commande = {"id": 100, "date": "2026-01-02T10:00:00", "items": []}
    assert client.is_order_fully_synced(commande, paheko) is True
    assert paheko.org_slug == "repli"


@pytest.mark.parametrize(
    "paheko",
    [
        PahekoFaux(membres=False),
        PahekoFaux(transactions=()),
        PahekoFaux(transactions={"HelloAsso 100"}),
        PahekoFaux(transactions={"HelloAsso 100", "HelloAsso 1000"}, activite={}),
    ],
)
def test_commande_incomplete(monkeypatch, paheko):
    client = client_synchro(monkeypatch)
    assert client.is_order_fully_synced(COMMANDE, paheko) is False


def test_commande_erreur_journalisee_avec_contexte(monkeypatch):
    client = client_synchro(monkeypatch)
    assert client.is_order_fully_synced(COMMANDE, PahekoFaux(annee_erreur=True)) is False


def test_commande_erreur_contexte_non_serialisable(monkeypatch):
    client = client_synchro(monkeypatch)

    def dumps_en_echec(*args, **kwargs):
        raise TypeError("non sérialisable")

    monkeypatch.setattr(HelloassoPaheko.jmodule, "dumps", dumps_en_echec)
    assert client.is_order_fully_synced(COMMANDE, PahekoFaux(annee_erreur=True)) is False


# ---------------------------------------------------------------------------
# Formulaires
# ---------------------------------------------------------------------------


class RequestsFaux:
    """Doublure de ``requests.get`` : réponse ou exception par URL."""

    def __init__(self, par_url):
        self.par_url = par_url
        self.appels: list[tuple] = []

    def __call__(self, url, headers=None, params=None):
        self.appels.append((url, params))
        reponse = self.par_url[url]
        if isinstance(reponse, Exception):
            raise reponse
        return reponse


BASE = "https://api.helloasso.example/organizations/asso/forms"


def test_get_all_forms_avec_et_sans_etats(monkeypatch):
    faux = RequestsFaux({BASE: ReponseFausse(200, {"data": [{"formSlug": "f"}]})})
    monkeypatch.setattr(HelloassoPaheko.requests, "get", faux, raising=False)
    client = fabrique_client_helloasso()
    assert client.get_all_forms("asso", states=["Public"]) == [{"formSlug": "f"}]
    assert client.get_all_forms("asso") == [{"formSlug": "f"}]
    assert faux.appels == [(BASE, {"states": ["Public"]}), (BASE, {})]


def test_get_all_forms_erreur(monkeypatch):
    faux = RequestsFaux({BASE: ReponseFausse(500, {})})
    monkeypatch.setattr(HelloassoPaheko.requests, "get", faux, raising=False)
    assert fabrique_client_helloasso().get_all_forms("asso") == []


def test_get_form_details(monkeypatch):
    erreur_404 = HTTPStatusError(ReponseFausse(404, {}))
    faux = RequestsFaux(
        {
            f"{BASE}/Event/ok/public": ReponseFausse(200, {"formSlug": "ok"}),
            f"{BASE}/Event/absent/public": erreur_404,
            f"{BASE}/Event/panne/public": ReponseFausse(500, {}),
            f"{BASE}/Event/brut/public": RuntimeError("sans réponse"),
        }
    )
    monkeypatch.setattr(HelloassoPaheko.requests, "get", faux, raising=False)
    client = fabrique_client_helloasso()
    assert client.get_form_details("asso", "Event", "ok") == {"formSlug": "ok"}
    assert client.get_form_details("asso", "Event", "absent") is None
    assert client.get_form_details("asso", "Event", "panne") is None
    assert client.get_form_details("asso", "Event", "brut") is None


def test_get_all_forms_with_details(monkeypatch):
    client = fabrique_client_helloasso()
    formulaires = [
        {"formSlug": "detaille", "formType": "Event", "state": "Public"},
        {"formSlug": "prive", "formType": "Event", "state": "Private"},
        {"formSlug": "public-404", "formType": "Event", "state": "Public"},
        {"formSlug": None, "formType": "Event"},
    ]
    etats_demandes = []

    def tous(slug, states):
        etats_demandes.append(states)
        return formulaires

    details = {
        "detaille": {
            "formSlug": "detaille",
            "title": "Fête",
            "tiers": [{"label": "Plein", "price": 1200}, {"minAmount": 500}, {}],
        },
    }
    monkeypatch.setattr(client, "get_all_forms", tous, raising=False)
    monkeypatch.setattr(
        client, "get_form_details", lambda slug, ft, fs: details.get(fs), raising=False
    )

    resultat = client.get_all_forms_with_details("asso")

    assert etats_demandes == [["Public", "Private"]]
    assert [f["formSlug"] for f in resultat] == ["detaille", "prive"]


def test_get_all_forms_with_details_aucun(monkeypatch):
    client = fabrique_client_helloasso()
    monkeypatch.setattr(client, "get_all_forms", lambda slug, states: [], raising=False)
    assert client.get_all_forms_with_details("asso", states=["Draft"]) == []


# ---------------------------------------------------------------------------
# _make_request : erreur HTTP levée par httpx
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 500])
def test_make_request_relance_les_erreurs_http(monkeypatch, code):
    """HTTPStatusError de httpx : journalisée (401) puis relancée telle quelle."""
    client = fabrique_client_helloasso()
    client.headers = {"Authorization": "Bearer x"}
    monkeypatch.setattr(client, "_ensure_valid_token", lambda: None, raising=False)

    def requete(*args, **kwargs):
        raise HTTPStatusError(ReponseFausse(code, {}))

    monkeypatch.setattr(HelloassoPaheko.httpx, "request", requete, raising=False)
    with pytest.raises(HTTPStatusError):
        client._make_request("GET", "https://api.helloasso.example/x")
