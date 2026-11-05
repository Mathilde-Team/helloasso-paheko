# SPDX-License-Identifier: GPL-3.0-or-later
"""Téléchargeur PDF : échec du lancement du navigateur (issue #38).

Journal de #37 : ``chromium.launch`` échoue (navigateur non installé), puis
chaque PDF suivant échoue avec « It looks like you are using Playwright Sync
API inside the asyncio loop », parce que l'instance Playwright du premier
essai n'était jamais arrêtée. La doublure ci-dessous reproduit cette règle de
Playwright : un second ``start()`` sans ``stop()`` préalable lève la même
erreur.
"""

from __future__ import annotations

import pytest
import test_palier2_clients  # noqa: F401  (doublures des modules externes)

import HelloassoPaheko
from HelloassoPaheko import HelloAssoPDFDownloader

ERREUR_BOUCLE = (
    "It looks like you are using Playwright Sync API inside the asyncio loop."
)
ERREUR_ABSENT = (
    "BrowserType.launch: Executable doesn't exist at "
    "/root/.cache/ms-playwright/chromium_headless_shell-1187/chrome-linux/headless_shell\n"
    "Looks like Playwright was just installed or updated."
)


class PlaywrightSansNavigateur:
    """Playwright dont le navigateur n'est pas installé."""

    def __init__(self):
        self.demarrages = 0
        self.arrets = 0
        self.actif = False
        self.chromium = self

    def __call__(self):
        return self

    def start(self):
        if self.actif:
            raise RuntimeError(ERREUR_BOUCLE)
        self.demarrages += 1
        self.actif = True
        return self

    def stop(self):
        self.arrets += 1
        self.actif = False

    def launch(self, headless):
        raise RuntimeError(ERREUR_ABSENT)


@pytest.fixture
def sans_navigateur(monkeypatch):
    faux = PlaywrightSansNavigateur()
    monkeypatch.setattr(HelloassoPaheko, "sync_playwright", faux)
    return faux


def test_echec_du_lancement_arrete_playwright_et_remet_a_zero(sans_navigateur):
    telechargeur = HelloAssoPDFDownloader("u", "p")

    with pytest.raises(RuntimeError, match="Executable doesn't exist"):
        telechargeur.start_browser()

    assert sans_navigateur.arrets == 1 and not sans_navigateur.actif
    assert telechargeur.playwright is None
    assert telechargeur.browser is None and telechargeur.page is None
    assert telechargeur.echec_lancement.startswith("BrowserType.launch: Executable")
    assert "\n" not in telechargeur.echec_lancement


def test_pas_de_nouvelle_tentative_apres_un_echec(sans_navigateur):
    telechargeur = HelloAssoPDFDownloader("u", "p")
    with pytest.raises(RuntimeError):
        telechargeur.start_browser()

    with pytest.raises(RuntimeError, match="Navigateur indisponible"):
        telechargeur.start_browser()

    assert sans_navigateur.demarrages == 1


def test_pdf_suivants_ignores_sans_erreur_de_boucle(
    sans_navigateur, tmp_path, monkeypatch
):
    """Cas du journal de #37 : 1 échec, puis plus aucune erreur par PDF."""
    erreurs = []
    monkeypatch.setattr(
        HelloassoPaheko.logger,
        "error",
        lambda msg, *a, **k: erreurs.append(msg.format(*a)),
    )
    telechargeur = HelloAssoPDFDownloader("u", "p")

    premier = telechargeur.download_pdf("https://h/c?cardId=1", "A", "B", str(tmp_path))
    suivants = [
        telechargeur.download_pdf(f"https://h/c?cardId={n}", "A", "B", str(tmp_path))
        for n in range(2, 13)
    ]

    assert premier is None and suivants == [None] * 11
    assert sans_navigateur.demarrages == 1
    assert not any("asyncio loop" in e for e in erreurs)
    assert sum("Lancement du navigateur impossible" in e for e in erreurs) == 1
    assert any("playwright install chromium" in e for e in erreurs)


def test_close_arrete_playwright_meme_si_la_fermeture_echoue():
    class NavigateurCasse:
        def close(self):
            raise RuntimeError("déjà fermé")

    class PlaywrightArretable:
        arrete = False

        def stop(self):
            self.arrete = True

    telechargeur = HelloAssoPDFDownloader("u", "p")
    telechargeur.browser = NavigateurCasse()
    telechargeur.playwright = playwright = PlaywrightArretable()
    telechargeur.is_logged_in = True

    telechargeur.close()

    assert playwright.arrete
    assert telechargeur.browser is None and telechargeur.playwright is None
    assert not telechargeur.is_logged_in


def test_close_tolere_un_arret_de_playwright_en_erreur():
    class PlaywrightCasse:
        def stop(self):
            raise RuntimeError("boucle fermée")

    telechargeur = HelloAssoPDFDownloader("u", "p")
    telechargeur.playwright = PlaywrightCasse()

    telechargeur.close()  # ne lève pas

    assert telechargeur.playwright is None


def test_echec_sans_message_garde_le_type_d_erreur(monkeypatch):
    class PlaywrightMuet(PlaywrightSansNavigateur):
        def launch(self, headless):
            raise TimeoutError()

    monkeypatch.setattr(HelloassoPaheko, "sync_playwright", PlaywrightMuet())
    telechargeur = HelloAssoPDFDownloader("u", "p")

    with pytest.raises(TimeoutError):
        telechargeur.start_browser()

    assert telechargeur.echec_lancement == "TimeoutError"
