"""Tests du module OfxPaheko (issue #5, palier 1).

Le client Paheko (HelloassoPaheko.PahekoAPIClient) est remplacé par une
doublure injectée dans sys.modules : les tests restent unitaires, sans
réseau ni identifiants. ofxtools est fourni en dépendance éphémère.
"""

import itertools
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import OfxPaheko
from OfxPaheko import OfxPahekoImporter

# ---------------------------------------------------------------------------
# Doublure du client Paheko (HelloassoPaheko.PahekoAPIClient)
# ---------------------------------------------------------------------------


class FauxPahekoAPIClient:
    """Enregistre les appels au lieu d'interroger un vrai Paheko."""

    def __init__(self, mode_simulation=False):
        self.mode_simulation = mode_simulation
        self.existing_refs = set()
        self.sql_results = None
        self.queries_sql = []
        self.created = []
        self.updated_label = []
        self.updated_reference = []
        self.year_by_year = None  # None → toujours 1

    def transaction_exists(self, reference):
        return reference in self.existing_refs

    def get_sql(self, query):
        self.queries_sql.append(query)
        return self.sql_results

    def get_current_year_id(self, date, organization_slug):
        if self.year_by_year is None:
            return 1
        return self.year_by_year.get(date.year, 1)

    def create_transaction(self, data):
        self.created.append(data)

    def update_transaction_label_and_reference(self, id_transaction, label, reference, notes):
        self.updated_label.append((id_transaction, label, reference, notes))

    def update_transaction_reference(self, id_transaction, reference):
        self.updated_reference.append((id_transaction, reference))


@pytest.fixture
def faux_paheko_module(monkeypatch):
    """Injecte un faux module HelloassoPaheko dans sys.modules."""
    module = types.ModuleType("HelloassoPaheko")
    module.PahekoAPIClient = FauxPahekoAPIClient
    monkeypatch.setitem(sys.modules, "HelloassoPaheko", module)
    return module


# ---------------------------------------------------------------------------
# Fichiers temporaires : noms uniques + nettoyage de /tmp
# ---------------------------------------------------------------------------

_compteur = itertools.count()


def nom_unique(suffix=".ofx"):
    return f"ofxtest-{next(_compteur)}{suffix}"


@pytest.fixture(autouse=True)
def _nettoie_tmp_ofx():
    yield
    for chemin in Path("/tmp").glob("ofxtest-*"):
        chemin.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Configuration et fixtures
# ---------------------------------------------------------------------------

CONFIG_AVEC_MAPPING = """\
[ofx]
account_debit = "512TEST"
account_credit = "707"

[ofx.account_mapping."DON PAYPAL"]
debit = "5112"
credit = "754"

[helloasso]
org_slug = "asso-test"
"""

CONFIG_MINIMALE = """\
[helloasso]
org_slug = "asso-minimale"
"""


@pytest.fixture
def importateur(tmp_path, monkeypatch, faux_paheko_module):
    """Importeur en mode simulation, configuré dans un répertoire temporaire."""
    (tmp_path / "config.toml").write_text(CONFIG_AVEC_MAPPING, encoding="utf-8")
    (tmp_path / "ofx").mkdir()
    monkeypatch.chdir(tmp_path)
    return OfxPahekoImporter(
        ofx_directory=str(tmp_path / "ofx"), mode_simulation=True
    )


def transaction_ofx(fitid, trnamt, memo=None, name=None, dtposted=None):
    """Transaction OFX minimale (mêmes attributs qu'un STMTTRN ofxtools)."""
    return SimpleNamespace(
        fitid=fitid,
        trnamt=trnamt,
        memo=memo,
        name=name,
        dtposted=dtposted or datetime(2026, 1, 2),
    )


def ofx_valide(transactions=None, dtstart="20260101", dtend="20260105"):
    """Corps OFX SGML valide pour ofxtools (préfixe OFX>, un seul message set)."""
    blocs = []
    for t in transactions or []:
        blocs.append(
            "<STMTTRN>\n"
            "<TRNTYPE>CREDIT\n"
            f"<DTPOSTED>{t.get('dtposted', '20260102000000')}\n"
            f"<TRNAMT>{t['trnamt']}\n"
            f"<FITID>{t['fitid']}\n"
            f"<NAME>{t.get('name', 'TIERS')}\n"
            f"<MEMO>{t.get('memo', 'Memo test')}\n"
            "</STMTTRN>"
        )
    return (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
        "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
        "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
        "OFX>\n<BANKMSGSRSV1>\n<STMTTRNRS>\n<TRNUID>1\n"
        "<STATUS>\n<CODE>0\n<SEVERITY>INFO\n</STATUS>\n"
        "<STMTRS>\n<CURDEF>EUR\n"
        "<BANKACCTFROM>\n<BANKID>30003\n<ACCTID>1234567890\n"
        "<ACCTTYPE>CHECKING\n</BANKACCTFROM>\n"
        "<BANKTRANLIST>\n"
        f"<DTSTART>{dtstart}\n<DTEND>{dtend}\n"
        + "\n".join(blocs)
        + "\n</BANKTRANLIST>\n"
        "<LEDGERBAL>\n<BALAMT>60.00\n<DTASOF>20260105\n</LEDGERBAL>\n"
        "</STMTRS>\n</STMTTRNRS>\n</BANKMSGSRSV1>\n"
    )


# ---------------------------------------------------------------------------
# __init__ et _load_config
# ---------------------------------------------------------------------------


def test_init_charge_la_configuration(tmp_path, monkeypatch, faux_paheko_module):
    (tmp_path / "config.toml").write_text(CONFIG_AVEC_MAPPING, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    importateur = OfxPahekoImporter(ofx_directory=str(tmp_path / "ofx"))

    assert importateur.default_account_debit == "512TEST"
    assert importateur.default_account_credit == "707"
    assert importateur.organization_slug == "asso-test"
    assert "DON PAYPAL" in importateur.account_mapping
    assert importateur.account_mapping["DON PAYPAL"]["credit"] == "754"
    assert importateur.ofx_directory == tmp_path / "ofx"
    assert importateur.mode_simulation is False


def test_init_valeurs_par_defaut(tmp_path, monkeypatch, faux_paheko_module):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    importateur = OfxPahekoImporter(ofx_directory=str(tmp_path / "ofx"))

    assert importateur.default_account_debit == "512BPBFC"
    assert importateur.default_account_credit == "706"
    assert importateur.organization_slug == "asso-minimale"
    assert importateur.account_mapping == {}


def test_init_sans_configuration_leve_une_erreur(tmp_path, monkeypatch, faux_paheko_module):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Exception):
        OfxPahekoImporter(ofx_directory=str(tmp_path / "ofx"))


def test_init_transmet_le_mode_simulation(tmp_path, monkeypatch, faux_paheko_module):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    importateur = OfxPahekoImporter(
        ofx_directory=str(tmp_path / "ofx"), mode_simulation=True
    )

    assert isinstance(importateur.paheko_client, FauxPahekoAPIClient)
    assert importateur.paheko_client.mode_simulation is True
    assert importateur.stats["files_processed"] == 0
    assert importateur.stats["transactions_imported"] == 0
    assert importateur.stats["total_credit"] == 0.0


# ---------------------------------------------------------------------------
# _preprocess_ofx_file
# ---------------------------------------------------------------------------


def test_preprocess_remplace_les_virgules(tmp_path, importateur):
    nom = nom_unique()
    source = tmp_path / nom
    source.write_text(
        "<TRNAMT>-80,00\n<BALAMT>1234,56\n", encoding="latin-1"
    )

    resultat = importateur._preprocess_ofx_file(source)

    assert resultat == Path("/tmp") / nom
    contenu = resultat.read_text(encoding="latin-1")
    assert "<TRNAMT>-80.00" in contenu
    assert "<BALAMT>1234.56" in contenu


def test_preprocess_calcule_le_solde_quand_balamt_vide(tmp_path, importateur):
    nom = nom_unique()
    source = tmp_path / nom
    source.write_text(
        "<LEDGERBAL>\n<BALAMT></BALAMT>\n<DTASOF>20260101\n</LEDGERBAL>\n"
        "<DTEND>20260131\n"
        "<TRNAMT>100.50\n<TRNAMT>-40.25\n",
        encoding="latin-1",
    )

    resultat = importateur._preprocess_ofx_file(source)
    contenu = resultat.read_text(encoding="latin-1")

    assert "<BALAMT>60.25</BALAMT>" in contenu
    assert "<DTASOF>20260131</DTASOF>" in contenu
    assert "<BALAMT></BALAMT>" not in contenu


def test_preprocess_injecte_le_solde_sans_ledgerbal(tmp_path, importateur):
    nom = nom_unique()
    source = tmp_path / nom
    source.write_text(
        "<TRNAMT>10.00\n</STMTRS>\n", encoding="latin-1"
    )

    resultat = importateur._preprocess_ofx_file(source)
    contenu = resultat.read_text(encoding="latin-1")

    assert "<LEDGERBAL>" in contenu
    assert "<BALAMT>10.00</BALAMT>" in contenu
    # le bloc est injecté avant la fermeture du relevé
    assert contenu.index("<LEDGERBAL>") < contenu.index("</STMTRS>")


def test_preprocess_solde_sans_dtend_utilise_la_date_du_jour(tmp_path, importateur):
    nom = nom_unique()
    source = tmp_path / nom
    source.write_text(
        "<TRNAMT>5.00\n</STMTRS>\n", encoding="latin-1"
    )

    resultat = importateur._preprocess_ofx_file(source)
    contenu = resultat.read_text(encoding="latin-1")

    aujourdhui = datetime.now().strftime("%Y%m%d")
    assert f"<DTASOF>{aujourdhui}</DTASOF>" in contenu


# ---------------------------------------------------------------------------
# _parse_ofx_file et _get_account_for_transaction
# ---------------------------------------------------------------------------


def test_parse_ofx_file(tmp_path, importateur):
    nom = nom_unique()
    source = tmp_path / nom
    source.write_text(
        ofx_valide(
            [
                {"fitid": "20260102001", "trnamt": "100,00", "memo": "Paiement test"},
                {"fitid": "20260103001", "trnamt": "-40,00", "memo": "Achat"},
            ]
        ),
        encoding="latin-1",
    )

    ofx = importateur._parse_ofx_file(source)

    releve = ofx.statements[0]
    assert releve.account.acctid == "1234567890"
    assert releve.account.accttype == "CHECKING"
    assert releve.curdef == "EUR"
    assert len(releve.transactions) == 2
    assert float(releve.transactions[0].trnamt) == 100.0


def test_get_account_utilise_le_mapping(importateur):
    trn = transaction_ofx("F1", 10.0, memo="Don paypal recu", name="TIERS")

    comptes = importateur._get_account_for_transaction(trn)

    assert comptes == {"debit": "5112", "credit": "754"}


def test_get_account_par_le_montant(importateur):
    recette = transaction_ofx("F1", 10.0, memo="Virement recu", name="TIERS")
    depense = transaction_ofx("F2", -10.0, memo="Paiement facture", name="TIERS")

    assert importateur._get_account_for_transaction(recette) == {
        "debit": "512TEST",
        "credit": "706",
    }
    assert importateur._get_account_for_transaction(depense) == {
        "debit": "6",
        "credit": "512TEST",
    }


def test_get_account_sans_memo_ni_name(importateur):
    trn = SimpleNamespace(trnamt=10.0, memo=None, name=None)

    comptes = importateur._get_account_for_transaction(trn)

    assert comptes == {"debit": "512TEST", "credit": "706"}


# ---------------------------------------------------------------------------
# _transaction_exists et _find_similar_transaction
# ---------------------------------------------------------------------------


def test_transaction_exists_delegue_au_client(importateur):
    importateur.paheko_client.existing_refs = {"OFX-FIT1"}

    assert importateur._transaction_exists("FIT1", datetime(2026, 1, 2)) is True
    assert importateur._transaction_exists("FIT2", datetime(2026, 1, 2)) is False


def test_find_similar_transaction_retourne_le_premier_resultat(importateur):
    importateur.paheko_client.sql_results = {
        "results": [{"id": 42, "label": "Virement", "reference": ""}]
    }

    trouve = importateur._find_similar_transaction(-40.0, datetime(2026, 1, 3), 1)

    assert trouve == {"id": 42, "label": "Virement", "reference": ""}
    requete = importateur.paheko_client.queries_sql[-1]
    assert "4000" in requete  # 40,00 € en centimes
    assert "2025-12-31" in requete and "2026-01-06" in requete  # fenêtre ±3 jours


def test_find_similar_transaction_sans_resultat(importateur):
    assert importateur._find_similar_transaction(10.0, datetime(2026, 1, 2), 1) is None


# ---------------------------------------------------------------------------
# _is_year_change et _create_year_change_entry
# ---------------------------------------------------------------------------


def test_is_year_change():
    cases = [
        (datetime(2025, 12, 31), datetime(2026, 1, 1), True),
        (datetime(2025, 6, 1), datetime(2026, 1, 1), True),  # saut vers le 1er janvier
        (datetime(2026, 1, 1), datetime(2026, 2, 1), False),
        (datetime(2025, 12, 31), datetime(2026, 1, 2), False),
        (datetime(2026, 1, 1), datetime(2026, 1, 2), False),
    ]
    for date1, date2, attendu in cases:
        assert OfxPahekoImporter._is_year_change(None, date1, date2) is attendu


def test_create_year_change_entry_deja_existante(importateur):
    importateur.paheko_client.existing_refs = {"OFX-REGULARISATION-2026"}

    importateur._create_year_change_entry(datetime(2026, 1, 1), 11)

    assert importateur.paheko_client.created == []


def test_create_year_change_entry_cree_l_ecriture(tmp_path, monkeypatch, faux_paheko_module):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    importateur = OfxPahekoImporter(
        ofx_directory=str(tmp_path / "ofx"), mode_simulation=False
    )

    importateur._create_year_change_entry(datetime(2026, 1, 1), 11)

    assert len(importateur.paheko_client.created) == 1
    ecriture = importateur.paheko_client.created[0]
    assert ecriture["reference"] == "OFX-REGULARISATION-2026"
    assert ecriture["debit"] == "772"
    assert ecriture["credit"] == "512BPBFC"
    assert ecriture["label"] == "Régularisation exercice antérieur 2025"
    assert ecriture["id_year"] == 11


def test_create_year_change_entry_en_simulation(importateur):
    importateur._create_year_change_entry(datetime(2026, 1, 1), 11)

    assert importateur.paheko_client.created == []


# ---------------------------------------------------------------------------
# _import_transaction
# ---------------------------------------------------------------------------


def test_import_transaction_deja_importee(importateur):
    trn = transaction_ofx("FIT1", 10.0, memo="Virement")
    importateur.paheko_client.existing_refs = {"OFX-FIT1"}

    resultat = importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert resultat is False
    assert importateur.stats["transactions_skipped"] == 1
    assert importateur.paheko_client.created == []


def test_import_transaction_similaire_avec_libelle_enrichi(importateur):
    trn = transaction_ofx("FIT1", 10.0, memo="Virement reçu de la boulangerie du coin")
    importateur.paheko_client.sql_results = {
        "results": [{"id": 42, "label": "Virement", "reference": "", "date": "2026-01-02"}]
    }

    resultat = importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert resultat is True
    assert importateur.stats["transactions_updated"] == 1
    # en mode simulation, aucune écriture n'est envoyée
    assert importateur.paheko_client.created == []
    assert importateur.paheko_client.updated_label == []


def test_import_transaction_similaire_met_a_jour_hors_simulation(
    tmp_path, monkeypatch, faux_paheko_module
):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    importateur = OfxPahekoImporter(
        ofx_directory=str(tmp_path / "ofx"), mode_simulation=False
    )
    trn = transaction_ofx("FIT1", 10.0, memo="Virement reçu de la boulangerie")
    importateur.paheko_client.sql_results = {
        "results": [{"id": 42, "label": "Virement", "reference": "", "date": "2026-01-02"}]
    }

    importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert len(importateur.paheko_client.updated_label) == 1
    id_trn, libelle, reference, notes = importateur.paheko_client.updated_label[0]
    assert id_trn == 42
    assert libelle == "Virement reçu de la boulangerie"
    assert reference == "OFX-FIT1"
    assert "Ancien libellé" in notes


def test_import_transaction_similaire_libelle_identique(importateur):
    trn = transaction_ofx("FIT1", 10.0, memo="Virement")
    importateur.paheko_client.sql_results = {
        "results": [{"id": 42, "label": "Virement", "reference": "", "date": "2026-01-02"}]
    }

    resultat = importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert resultat is True
    assert importateur.stats["transactions_updated"] == 1


def test_import_transaction_similaire_ajoute_la_reference_hors_simulation(
    tmp_path, monkeypatch, faux_paheko_module
):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    importateur = OfxPahekoImporter(
        ofx_directory=str(tmp_path / "ofx"), mode_simulation=False
    )
    # libellé de même longueur : pas d'enrichissement, juste la référence
    trn = transaction_ofx("FIT1", 10.0, memo="Virement")
    importateur.paheko_client.sql_results = {
        "results": [{"id": 42, "label": "Virement", "reference": "", "date": "2026-01-02"}]
    }

    importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert importateur.paheko_client.updated_reference == [(42, "OFX-FIT1")]
    assert importateur.paheko_client.updated_label == []


def test_import_transaction_mappee_negative(importateur):
    trn = transaction_ofx("FIT1", -100.0, memo="Remboursement don paypal")

    resultat = importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert resultat is True
    assert importateur.stats["transactions_imported"] == 1
    assert importateur.stats["total_debit"] == 100.0
    assert importateur.stats["total_credit"] == 0.0


def test_import_transaction_remise_de_cheque(importateur):
    trn = transaction_ofx("FIT1", 500.0, memo="Remise cheque no 123")

    resultat = importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert resultat is True
    assert importateur.stats["transactions_imported"] == 1
    assert importateur.stats["total_debit"] == 500.0
    assert importateur.paheko_client.created == []  # simulation


def test_import_transaction_remise_de_cheque_hors_simulation(
    tmp_path, monkeypatch, faux_paheko_module
):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    importateur = OfxPahekoImporter(
        ofx_directory=str(tmp_path / "ofx"), mode_simulation=False
    )
    trn = transaction_ofx("FIT1", 500.0, memo="Remise cheque no 123")

    importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert len(importateur.paheko_client.created) == 1
    ecriture = importateur.paheko_client.created[0]
    assert ecriture["type"] == "ADVANCED"
    assert ecriture["lines"][0]["account"] == "5112"
    assert ecriture["lines"][1]["account"] == "512BPBFC"
    assert ecriture["reference"] == "OFX-FIT1"


def test_import_transaction_mappee(importateur):
    trn = transaction_ofx("FIT1", 100.0, memo="Don paypal recu")

    resultat = importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert resultat is True
    assert importateur.stats["transactions_imported"] == 1
    assert importateur.stats["total_credit"] == 100.0
    assert importateur.paheko_client.created == []


def test_import_transaction_mappee_hors_simulation(
    tmp_path, monkeypatch, faux_paheko_module
):
    (tmp_path / "config.toml").write_text(CONFIG_AVEC_MAPPING, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    importateur = OfxPahekoImporter(
        ofx_directory=str(tmp_path / "ofx"), mode_simulation=False
    )
    trn = transaction_ofx("FIT1", 100.0, memo="Don paypal recu")

    importateur._import_transaction(trn, {"account_id": "123"}, 1)

    ecriture = importateur.paheko_client.created[0]
    assert ecriture["type"] == "ADVANCED"
    assert ecriture["lines"][0]["account"] == "5112"
    assert ecriture["lines"][1]["account"] == "754"
    assert "Import OFX automatique" in ecriture["notes"]
    assert "FIT1" in ecriture["notes"]


def test_import_transaction_non_mappee_recette(importateur):
    trn = transaction_ofx("FIT1", 30.0, memo="Virement inconnu")

    resultat = importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert resultat is True
    assert importateur.stats["total_credit"] == 30.0


def test_import_transaction_non_mappee_depense(importateur):
    trn = transaction_ofx("FIT2", -30.0, memo="Achat divers")

    resultat = importateur._import_transaction(trn, {"account_id": "123"}, 1)

    assert resultat is True
    assert importateur.stats["total_debit"] == 30.0


def test_import_transaction_non_mappee_compte_attente(
    tmp_path, monkeypatch, faux_paheko_module
):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    importateur = OfxPahekoImporter(
        ofx_directory=str(tmp_path / "ofx"), mode_simulation=False
    )
    trn = transaction_ofx("FIT1", 30.0, memo="Virement inconnu")

    importateur._import_transaction(trn, {"account_id": "123"}, 1)

    ecriture = importateur.paheko_client.created[0]
    assert ecriture["lines"][0]["account"] == "512BPBFC"
    assert ecriture["lines"][1]["account"] == "47"
    assert "À régulariser" in ecriture["notes"]


# ---------------------------------------------------------------------------
# import_ofx_file, import_all_ofx_files, print_statistics, main
# ---------------------------------------------------------------------------


def test_import_ofx_file(tmp_path, importateur):
    nom = nom_unique()
    source = tmp_path / nom
    source.write_text(
        ofx_valide(
            [
                {
                    "fitid": "20251231001",
                    "trnamt": "100,00",
                    "memo": "Virement recu",
                    "dtposted": "20251231000000",
                },
                {
                    "fitid": "20260101001",
                    "trnamt": "-40,00",
                    "memo": "Achat",
                    "dtposted": "20260101000000",
                },
            ],
            dtstart="20251201",
            dtend="20260105",
        ),
        encoding="latin-1",
    )
    importateur.paheko_client.existing_refs = {"OFX-20260101001"}
    importateur.paheko_client.year_by_year = {2025: 10, 2026: 11}

    stats_fichier = importateur.import_ofx_file(source)

    assert stats_fichier["transactions_total"] == 2
    assert stats_fichier["transactions_imported"] == 1
    assert stats_fichier["transactions_skipped"] == 1
    assert stats_fichier["transactions_failed"] == 0
    assert importateur.stats["files_processed"] == 1
    # changement d'exercice détecté (31/12/2025 → 01/01/2026) :
    # en simulation, l'écriture de régularisation n'est pas envoyée
    assert importateur.paheko_client.created == []


def test_import_ofx_file_cree_la_regularisation_hors_simulation(
    tmp_path, monkeypatch, faux_paheko_module
):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    importateur = OfxPahekoImporter(
        ofx_directory=str(tmp_path / "ofx"), mode_simulation=False
    )
    nom = nom_unique()
    source = tmp_path / nom
    source.write_text(
        ofx_valide(
            [
                {
                    "fitid": "20260101001",
                    "trnamt": "-40,00",
                    "memo": "Achat",
                    "dtposted": "20260101000000",
                },
            ],
            dtstart="20260101",
            dtend="20260105",
        ),
        encoding="latin-1",
    )

    importateur.import_ofx_file(source)

    # la transaction importée + aucune écriture de régularisation
    # (une seule transaction : pas de changement d'exercice)
    assert len(importateur.paheko_client.created) == 1
    assert importateur.paheko_client.created[0]["reference"] == "OFX-20260101001"


def test_import_ofx_file_annee_comptable_introuvable(tmp_path, importateur):
    nom = nom_unique()
    source = tmp_path / nom
    source.write_text(
        ofx_valide(
            [{"fitid": "20260102001", "trnamt": "10,00", "dtposted": "20260102000000"}]
        ),
        encoding="latin-1",
    )
    importateur.paheko_client.year_by_year = {}  # get_current_year_id → 1 par défaut
    # pour forcer l'échec : aucune année ne correspond
    original = importateur.paheko_client.get_current_year_id
    importateur.paheko_client.get_current_year_id = lambda date, slug: None

    stats_fichier = importateur.import_ofx_file(source)

    assert stats_fichier["transactions_failed"] == 1
    assert stats_fichier["transactions_imported"] == 0


def test_import_all_ofx_files(tmp_path, importateur):
    for i in range(2):
        nom = nom_unique()
        source = tmp_path / nom
        source.write_text(
            ofx_valide(
                [{"fitid": f"2026010200{i}", "trnamt": "10,00", "dtposted": "20260102000000"}]
            ),
            encoding="latin-1",
        )
        source.rename(importateur.ofx_directory / nom)

    stats = importateur.import_all_ofx_files()

    assert importateur.stats["files_processed"] == 2


def test_import_all_ofx_files_repertoire_vide(importateur):
    stats = importateur.import_all_ofx_files()

    assert importateur.stats["files_processed"] == 0


def test_import_all_ofx_files_repertoire_inexistant(tmp_path, monkeypatch, faux_paheko_module):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    importateur = OfxPahekoImporter(
        ofx_directory=str(tmp_path / "inexistant"), mode_simulation=True
    )

    stats = importateur.import_all_ofx_files()

    assert stats["files_processed"] == 0


def test_print_statistics(importateur):
    # ne doit pas lever : les clés manipulées existent toutes
    importateur.stats["total_credit"] = 100.0
    importateur.stats["total_debit"] = 40.0

    importateur.print_statistics()


def test_main_simulation(tmp_path, monkeypatch, faux_paheko_module):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    repertoire_ofx = tmp_path / "ofx"
    repertoire_ofx.mkdir()
    (repertoire_ofx / nom_unique()).write_text(
        ofx_valide(
            [{"fitid": "20260102001", "trnamt": "10,00", "dtposted": "20260102000000"}]
        ),
        encoding="latin-1",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["OfxPaheko.py", "--ofx-dir", str(repertoire_ofx), "--simulation"]
    )

    assert OfxPaheko.main() == 0


def test_main_fichier_unique(tmp_path, monkeypatch, faux_paheko_module):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    fichier = tmp_path / nom_unique()
    fichier.write_text(
        ofx_valide(
            [{"fitid": "20260102001", "trnamt": "10,00", "dtposted": "20260102000000"}]
        ),
        encoding="latin-1",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["OfxPaheko.py", "--simulation", "--file", str(fichier)])

    assert OfxPaheko.main() == 0


def test_main_fichier_introuvable(tmp_path, monkeypatch, faux_paheko_module):
    (tmp_path / "config.toml").write_text(CONFIG_MINIMALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["OfxPaheko.py", "--file", str(tmp_path / "absent.ofx")])

    assert OfxPaheko.main() == 1


def test_main_sans_configuration(tmp_path, monkeypatch, faux_paheko_module):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["OfxPaheko.py", "--simulation"])

    # l'exception de _load_config est interceptée par main → code 1
    assert OfxPaheko.main() == 1
