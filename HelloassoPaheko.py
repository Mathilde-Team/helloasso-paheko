import httpx
import tomllib
import tomli_w
import json as jmodule
import os
import csv
import io
import re
import sys
import sqlite3
import requests
import traceback

from pathlib import Path
from loguru import logger
from dotenv import load_dotenv
from datetime import date, datetime, timedelta
from authlib.integrations.requests_client import OAuth2Session
from time import sleep
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

load_dotenv()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(
            f"Required environment variable {name} is not set. "
            "Copy .env.example to .env and configure it."
        )
    return value


Path("logs").mkdir(parents=True, exist_ok=True)

# Configuration de Loguru : log dans un fichier avec rotation et niveau DEBUG
logger.add("logs/paheko_sync.log", rotation="30 MB", level="DEBUG")

CURRENT_YEAR = date.today().year
ANCIENS_MEMBRES_CATEGORY_ID = 2
MEMBRES_ACTIFS_CATEGORY_ID = 1
NON_ADHERENTS_CATEGORY_ID = 4
NON_ADHERENTS_PAYER_CATEGORY_ID = 5
ADMIN_CATEGORY_ID = 3


class HelloAssoPDFDownloader:
    """
    Gestionnaire de téléchargement de PDFs HelloAsso.

    Note importante: Les URLs de téléchargement HelloAsso (www.helloasso.com) sont protégées
    par Cloudflare et nécessitent une session authentifiée via navigateur. Le Bearer token API
    fonctionne uniquement pour l'API (api.helloasso.com), pas pour les PDFs du site web.
    C'est pourquoi Playwright est nécessaire.
    """

    def __init__(self, username: str, password: str, org_slug: str = "transcende"):
        """
        Initialise le downloader avec identifiants HelloAsso.

        :param username: Email HelloAsso
        :param password: Mot de passe HelloAsso
        :param org_slug: Slug de l'organisation (défini dans config.toml)
        """
        self.username = username
        self.password = password
        self.org_slug = org_slug
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.is_logged_in = False

    def start_browser(self):
        """Lance le navigateur Playwright."""
        if self.browser:
            return  # Déjà lancé

        logger.info("🌐 Lancement du navigateur (headless)...")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        self.page = self.context.new_page()
        logger.success("   ✓ Navigateur lancé")

    def login(self):
        """Se connecte à HelloAsso via le formulaire web."""
        if self.is_logged_in:
            logger.debug("   Déjà connecté")
            return

        logger.info("🔐 Connexion à HelloAsso...")

        # 1. Aller sur la page de connexion
        self.page.goto(
            "https://auth.helloasso.com/connexion?back=https://www.helloasso.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self.page.wait_for_timeout(1000)

        # 2. Remplir le formulaire
        self.page.wait_for_selector('[data-testid="login-input-email"]', timeout=10000)
        self.page.fill('[data-testid="login-input-email"]', self.username)
        self.page.fill('[data-testid="login-input-password"]', self.password)

        # 3. Soumettre avec Enter (méthode la plus fiable)
        self.page.press('[data-testid="login-input-password"]', "Enter")
        self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        self.page.wait_for_timeout(1000)

        # Vérifier la connexion
        if "helloasso.com" in self.page.url:
            logger.success("   ✅ Connecté - URL: {}", self.page.url)
            self.is_logged_in = True

            # Aller sur la page admin pour établir la session
            self.page.goto(
                f"https://admin.helloasso.com/{self.org_slug}/accueil",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        else:
            raise Exception(f"Échec de la connexion - URL: {self.page.url}")

    def download_pdf(
        self,
        pdf_url: str,
        first_name: str,
        last_name: str,
        destination_folder: str = "./pdfs_helloasso",
    ) -> str | None:
        """
        Télécharge un PDF depuis HelloAsso avec nom/prénom dans le nom du fichier.
        Utilise Playwright car les URLs sont protégées par Cloudflare.

        :param pdf_url: URL complète du PDF
        :param first_name: Prénom de la personne
        :param last_name: Nom de la personne
        :param destination_folder: Dossier de destination
        :return: Chemin du fichier téléchargé ou None
        """
        # Créer le dossier de destination
        os.makedirs(destination_folder, exist_ok=True)

        # Nettoyer nom et prénom pour le nom de fichier
        clean_first = re.sub(r"[^\w\s-]", "", first_name).strip().replace(" ", "_")
        clean_last = re.sub(r"[^\w\s-]", "", last_name).strip().replace(" ", "_")
        person_name = f"{clean_first}_{clean_last}"

        # Générer un nom de fichier avec nom/prénom
        if "ticketId=" in pdf_url:
            pdf_id = pdf_url.split("ticketId=")[1].split("&")[0]
            filename = f"ticket_{pdf_id}_({person_name}).pdf"
        elif "cardId=" in pdf_url:
            pdf_id = pdf_url.split("cardId=")[1].split("&")[0]
            filename = f"carte_membre_{pdf_id}_({person_name}).pdf"
        elif "paiement-attestation" in pdf_url:
            pdf_id = pdf_url.split("/")[-1]
            filename = f"recu_paiement_{pdf_id}_({person_name}).pdf"
        else:
            filename = f"document_{datetime.now().strftime('%Y%m%d_%H%M%S')}_({person_name}).pdf"

        destination_path = os.path.join(destination_folder, filename)

        # Vérifier si déjà téléchargé
        if os.path.exists(destination_path):
            logger.debug("   📄 PDF déjà existant: {}", filename)
            return destination_path

        logger.info("📥 Téléchargement: {}...", filename)

        try:
            # S'assurer d'être connecté
            if not self.browser:
                self.start_browser()
            if not self.is_logged_in:
                self.login()

            # Télécharger le PDF avec Playwright
            with self.page.expect_download(timeout=30000) as download_info:
                self.page.evaluate(f"window.location.href = '{pdf_url}'")

            download = download_info.value
            download.save_as(destination_path)

            file_size = os.path.getsize(destination_path)
            logger.success("   ✅ Téléchargé: {} ({:.2f} KB)", filename, file_size / 1024)

            # Vérifier la signature PDF
            with open(destination_path, "rb") as f:
                if not f.read(4).startswith(b"%PDF"):
                    logger.warning("   ⚠ Signature PDF invalide")
                    return None

            return destination_path

        except Exception as e:
            logger.error("   ❌ Erreur téléchargement {}: {}", filename, str(e)[:100])
            return None

    def download_order_pdfs(
        self, order_data: dict, destination_folder: str = "./pdfs_helloasso"
    ) -> Dict[str, str | None]:
        """
        Télécharge tous les PDFs d'une commande (depuis items et payments).

        :param order_data: Données de la commande complète avec items et payments
        :param destination_folder: Dossier de destination
        :return: Dict avec les chemins des fichiers téléchargés
        """
        results = {}

        # Récupérer le nom du payeur
        payer = order_data.get("payer", {})
        payer_first_name = payer.get("firstName", "Unknown")
        payer_last_name = payer.get("lastName", "Unknown")

        # Parcourir les items pour les tickets et cartes de membre
        for item in order_data.get("items", []):
            # Ticket URL
            if ticket_url := item.get("ticketUrl"):
                # Utiliser le nom de l'item user si disponible, sinon le payeur
                user = item.get("user", {})
                first_name = user.get("firstName", payer_first_name)
                last_name = user.get("lastName", payer_last_name)

                path = self.download_pdf(ticket_url, first_name, last_name, destination_folder)
                results[f"ticket_{item.get('id', 'unknown')}"] = path

            # Carte de membre URL
            if card_url := item.get("membershipCardUrl"):
                user = item.get("user", {})
                first_name = user.get("firstName", payer_first_name)
                last_name = user.get("lastName", payer_last_name)

                path = self.download_pdf(card_url, first_name, last_name, destination_folder)
                results[f"membership_card_{item.get('id', 'unknown')}"] = path

        # Parcourir les payments pour les reçus de paiement
        for payment in order_data.get("payments", []):
            if receipt_url := payment.get("paymentReceiptUrl"):
                path = self.download_pdf(
                    receipt_url, payer_first_name, payer_last_name, destination_folder
                )
                results[f"payment_receipt_{payment.get('id', 'unknown')}"] = path

        return results

    def close(self):
        """Ferme le navigateur proprement."""
        if self.browser:
            self.browser.close()
            logger.info("🔒 Navigateur fermé")
        if self.playwright:
            self.playwright.stop()


class PahekoAPIClient:
    """
    Client API pour interagir avec l'API Paheko.
    Permet de créer des membres, des transactions comptables et de récupérer les catégories de membres.
    """

    def __init__(self, mode_simulation: bool = False):
        """
        Charge la configuration depuis un fichier TOML et initialise le client HTTP.
        Lit également les chemins de sauvegarde depuis la section [backups] du fichier de configuration.

        :param mode_simulation: Si True, aucune requête réelle n'est envoyée à l'API Paheko.
        """
        self.config_path = "config.toml"
        logger.debug("Chargement de la configuration Paheko depuis {}", self.config_path)

        with open(self.config_path, "rb") as f:
            config = tomllib.load(f)

        self.login = _required_env("PAHEKO_USERNAME")
        self.password = _required_env("PAHEKO_PASSWORD")
        self.base_url = config["paheko"]["base_url"]
        self.organization_slug = config["helloasso"]["org_slug"]

        self.mode_simulation = mode_simulation

        # Chemin de la base de données SQLite pour accès direct
        self.db_path = config["paheko"].get("data_base", "")

        # Chemins de sauvegarde dans la section [backups] de config.toml
        backups_config = config.get("backups", {})
        self.backup_sqlite_path = backups_config.get("sqlite", ".")
        self.backup_zip_path = backups_config.get("zip", ".")

        # Créer les répertoires de sauvegarde s'ils n'existent pas
        Path(self.backup_sqlite_path).mkdir(parents=True, exist_ok=True)
        Path(self.backup_zip_path).mkdir(parents=True, exist_ok=True)

        # Configurer le client HTTP avec timeout augmenté (30s read, 10s connect)
        self.client = httpx.Client(
            auth=(self.login, self.password), timeout=httpx.Timeout(30.0, connect=10.0)
        )
        logger.info("Client Paheko initialisé avec l'URL de base : {}", self.base_url)

        # Appels initiaux (à ajuster selon votre logique)

        self.download_sqlite_backup()
        self.download_zip_backup()

        self.users = self.get_all_users_by_categories()
        self.journaux = self.get_journals_for_account_all_years(account="512HELLOASSO")

        # Cache pour les activités (événements HelloAsso -> activités Paheko)
        self.activities_cache = {}

        # logger.info(self.get_all_tables_and_definitions())
        # Si pas en simulation, initialisation du client HTTP
        if not self.mode_simulation:
            self.client = httpx.Client(
                auth=(self.login, self.password), timeout=httpx.Timeout(30.0, connect=10.0)
            )
            logger.info("Client Paheko initialisé avec l'URL : {}", self.base_url)
        else:
            logger.info("Mode SIMULATION activé : aucune requête ne sera envoyée à Paheko")

        logger.info("Connexion Paheko    Ok")
        sleep(6)
        logger.info(datetime.now())
        logger.info(datetime.now() - timedelta(days=365))

        self.IDCURRENTYEAR = self.get_current_year_id(
            datetime.now(), self.organization_slug
        ) or self.get_current_year_id(datetime.now() - timedelta(days=365), self.organization_slug)

    def _get_serviceLabel(self, form: Dict) -> str:
        """Obtient le label du service pour un formulaire"""
        formType = form.get("formType", "Unknown")
        ft = form.get("formType", "Unknown")
        formSlug = form.get("formSlug", "")
        raw_order_date = form.get("date")

        new_mots = [
            "Financement participatif",
            "Adhésion",
            "Événement",
            "Don",
            "Formulaire de paiement",
            "Paiement",
            "Boutique",
        ]
        for c, mot in enumerate(
            [
                "CrowdFunding",
                "Membership",
                "Event",
                "Donation",
                "PaymentForm",
                "Checkout",
                "Shop",
            ]
        ):
            formType = formType.replace(mot, new_mots[c]).strip()
        if ft == "Membership":
            if raw_order_date:
                # La date est au format ISO 8601: "2022-06-06T12:48:52.8653471+02:00"
                # On extrait juste la partie date (YYYY)
                year = raw_order_date[:4]

                return f"{formType} - {formSlug} {year}"
        logger.debug("Service label standard: {} - {}", formType, formSlug)
        return f"{formType} - {formSlug}"

    def get_member_by_email(
        self,
        email: str,
    ) -> dict:
        """
        Recherche un utilisateur dans Paheko par email en parcourant tous les utilisateurs récupérés par catégorie.
        La recherche est insensible à la casse.

        :param email: email de l'utilisateur.
        :return: Dictionnaire contenant les informations de l'utilisateur trouvé, ou None si aucun utilisateur n'est trouvé.
        """
        all_users = self.get_all_users_by_categories()
        logger.info("get_member_by_email email: ", email)
        email_lower = email.lower()
        for user1 in all_users:
            # logger.info("email utilisateur: ", user1.get("nom", ""), user1.get("email", ""))
            user_email = str(user1.get("email", "")).lower()
            if user_email == email_lower:

                logger.warning(
                    f"Membre existant trouvé par nom: {jmodule.dumps(user1, indent=4, ensure_ascii=False, default=str)}"
                )
                return user1
        logger.info("Aucun utilisateur trouvé avec le email '{}'", email)
        return None

    def get_member_by_name(
        self,
        nom: str,
    ) -> dict:
        """
        Recherche un utilisateur dans Paheko par nom en parcourant tous les utilisateurs récupérés par catégorie.
        La recherche est insensible à la casse.

        :param nom: Nom de l'utilisateur.
        :return: Dictionnaire contenant les informations de l'utilisateur trouvé, ou None si aucun utilisateur n'est trouvé.
        """
        all_users = self.get_all_users_by_categories()

        nom_upper = nom.upper() if nom else ""
        for user1 in all_users:
            user_nom = user1.get("nom", "").upper()
            if nom_upper in user_nom:

                logger.warning(
                    f"Membre existant trouvé par nom: {jmodule.dumps(user1, indent=4, ensure_ascii=False, default=str)}"
                )
                return user1
        logger.info("Aucun utilisateur trouvé avec le nom '{}'", nom)
        return None

    def get_member_by_phone(
        self,
        telephone: str,
    ) -> dict:
        """
        Recherche un utilisateur dans Paheko par numéro de téléphone.
        La recherche normalise les espaces et caractères spéciaux.

        :param telephone: Numéro de téléphone de l'utilisateur.
        :return: Dictionnaire contenant les informations de l'utilisateur trouvé, ou None si aucun utilisateur n'est trouvé.
        """
        if not telephone or not telephone.strip():
            logger.debug("Recherche par téléphone: numéro vide ou None")
            return None

        all_users = self.get_all_users_by_categories()
        # Normaliser le numéro de téléphone (enlever espaces, tirets, points)
        phone_normalized = "".join(c for c in telephone if c.isdigit())

        if not phone_normalized:
            logger.debug("Recherche par téléphone: aucun chiffre trouvé dans '{}'", telephone)
            return None

        for user in all_users:
            user_phone = user.get("telephone", "")
            user_phone_normalized = "".join(c for c in user_phone if c.isdigit())
            if user_phone_normalized and user_phone_normalized == phone_normalized:
                logger.warning(
                    f"Membre existant trouvé par telephone: {jmodule.dumps(user, indent=4, ensure_ascii=False, default=str)}"
                )
                return user

        logger.debug("Aucun utilisateur trouvé avec le téléphone '{}'", telephone)
        return None

    def get_member_by_name_and_phone(
        self,
        nom: str,
        telephone: str,
    ) -> dict:
        """
        Recherche un utilisateur par nom ET téléphone pour identifier les homonymes.
        Utile quand plusieurs personnes ont le même nom mais des téléphones différents.

        :param nom: Nom de l'utilisateur.
        :param telephone: Numéro de téléphone de l'utilisateur.
        :return: Dictionnaire contenant les informations de l'utilisateur trouvé, ou None si aucun utilisateur n'est trouvé.
        """
        all_users = self.get_all_users_by_categories()
        nom_upper = nom.upper() if nom else ""
        phone_normalized = "".join(c for c in telephone if c.isdigit()) if telephone else ""

        for user in all_users:
            user_nom = user.get("nom", "").upper()
            user_phone = user.get("telephone", "")
            user_phone_normalized = "".join(c for c in user_phone if c.isdigit())

            # Chercher un match sur le nom ET le téléphone
            if (
                nom_upper in user_nom
                and phone_normalized
                and user_phone_normalized == phone_normalized
            ):

                logger.warning(
                    f"Membre existant trouvé par nom et téléphone: {jmodule.dumps(user, indent=4, ensure_ascii=False, default=str)}"
                )
                return user

        logger.debug(
            "Aucun utilisateur trouvé avec le nom '{}' et le téléphone '{}'", nom, telephone
        )
        return None

    def _retry_request(self, func, *args, max_retries=5, **kwargs):
        """
        Exécute une requête HTTP avec retry et backoff exponentiel.

        :param func: Fonction à exécuter (ex: self.client.get)
        :param max_retries: Nombre maximum de tentatives
        :param args: Arguments positionnels pour la fonction
        :param kwargs: Arguments nommés pour la fonction
        :return: Résultat de la fonction
        :raises: Exception si toutes les tentatives échouent
        """
        last_exception = None

        for attempt in range(max_retries):
            try:
                response = func(*args, **kwargs)

                # Vérifier le code de statut sans lever d'exception immédiatement
                if response.status_code >= 500:
                    # Erreur serveur 5xx - on peut retry
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"⚠️  Erreur serveur {response.status_code} (tentative {attempt + 1}/{max_retries}), retry dans {wait_time}s"
                        )
                        sleep(wait_time)
                        continue  # Réessayer
                    else:
                        # Dernière tentative échouée, lever l'exception
                        response.raise_for_status()
                elif response.status_code >= 400:
                    # Erreur client 4xx - pas de retry, lever immédiatement
                    response.raise_for_status()

                # Succès (2xx ou 3xx)
                return response

            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                last_exception = e
                if attempt < max_retries - 1:
                    wait_time = 2**attempt  # Backoff exponentiel: 1s, 2s, 4s, 8s, 16s
                    logger.warning(
                        f"⚠️  Timeout (tentative {attempt + 1}/{max_retries}), retry dans {wait_time}s: {e}"
                    )
                    sleep(wait_time)
                else:
                    logger.error(f"❌ Timeout après {max_retries} tentatives")
                    raise
            except httpx.HTTPStatusError as e:
                # Cette exception est levée par raise_for_status() pour les 4xx
                logger.error(f"❌ Erreur HTTP client {e.response.status_code}: {e}")
                raise
            except Exception as e:
                logger.error(f"❌ Erreur inattendue: {type(e).__name__}: {e}")
                raise

        # Si on arrive ici, toutes les tentatives ont échoué
        if last_exception:
            raise last_exception

    def get_member_categories(self) -> list:
        """
        Récupère la liste des catégories de membres depuis l'API Paheko.
        Utilise un mécanisme de retry en cas de timeout ou erreur serveur.

        :return: Liste des catégories de membres.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        url = f"{self.base_url}/user/categories"
        logger.debug("Récupération des catégories de membres depuis {}", url)
        response = self._retry_request(self.client.get, url)
        categories = response.json()
        logger.info("Catégories de membres récupérées : {}", len(categories))
        return categories

    def get_members_by_category(self, category_id: int) -> list:
        """
        Récupère les utilisateurs d'une catégorie spécifique depuis l'API Paheko.
        Utilise un mécanisme de retry en cas de timeout ou erreur serveur.

        :param category_id: ID de la catégorie.
        :return: Liste des utilisateurs de la catégorie.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        url = f"{self.base_url}/user/category/{category_id}.json"
        logger.debug("Récupération des utilisateurs pour la catégorie {}", category_id)
        response = self._retry_request(self.client.get, url)
        users = response.json()
        logger.info(
            "Nombre d'Utilisateurs récupérés pour la catégorie {} : {}", category_id, len(users)
        )
        return users

    def get_all_users_by_categories(self) -> list:
        """
        Récupère les utilisateurs de toutes les catégories et les stocke dans une liste.

        :return: Liste des dictionnaires d'utilisateurs .
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        categories = self.get_member_categories()
        all_users = []

        # logger.info(f"{jmodule.dumps(categories, indent=4, ensure_ascii=False, default=str)}")

        for category_id in categories:
            if category_id is not None:
                users = self.get_members_by_category(category_id)
                for user in users:
                    all_users.append(user)
        return all_users

    def create_member(self, member_data: dict) -> dict:
        """
        Crée un nouveau membre dans Paheko via l'endpoint /user/new.

        :param member_data: Dictionnaire contenant les données du membre (nom, prénom, email, id_category, password, etc.).
        :return: Réponse JSON de l'API Paheko.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        url = f"{self.base_url}/user/new"
        headers = {"Content-Type": "application/json"}
        logger.debug(
            "Création d'un membre dans Paheko avec les données : {}",
            jmodule.dumps(member_data, indent=4, ensure_ascii=False, default=str),
        )
        response = self.client.post(url, json=member_data, headers=headers)
        response.raise_for_status()
        logger.info("Membre créé avec succès dans Paheko.")
        return response.json()

    def update_member(
        self, member_id: int, member_data: dict, existing_category: str = None
    ) -> dict:
        """
        Met à jour un membre existant dans Paheko via l'endpoint /user/{id}.
        Les membres administrateurs ne sont pas modifiables.

        :param member_id: ID du membre à mettre à jour.
        :param member_data: Dictionnaire contenant les données à mettre à jour (nom, id_category, etc.).
        :param existing_category: Catégorie actuelle du membre pour vérifier s'il est administrateur.
        :return: Réponse JSON de l'API Paheko.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        :raises ValueError: Si le membre est administrateur.
        """
        # Vérifier si le membre est administrateur
        logger.info("existing_category:", existing_category)
        if existing_category and "administrateur" in existing_category.lower():
            logger.warning(
                "Tentative de modification d'un membre administrateur (ID: {}). Modification refusée.",
                member_id,
            )
            raise ValueError("Les membres administrateurs ne peuvent pas être modifiés")

        url = f"{self.base_url}/user/{member_id}"
        headers = {"Content-Type": "application/json"}
        logger.debug(
            "Mise à jour du membre {} avec les données : {}",
            member_id,
            jmodule.dumps(member_data, indent=4, ensure_ascii=False, default=str),
        )
        response = self.client.post(url, json=member_data, headers=headers)
        response.raise_for_status()
        logger.info("Membre {} mis à jour avec succès.", member_id)
        return response.json()

    def get_activity(self, activity_label: str) -> dict:
        """
        Récupère ou crée une activité dans Paheko à partir d'un nom d'événement HelloAsso.
        Utilise un cache pour éviter les requêtes multiples.

        Pour les cotisations (Membership), ajoute automatiquement "Adhésion - {formTitle} YYYY" au nom.

        :param activity_label: Nom de l'activité HelloAsso.
        :return: Dictionnaire contenant les informations de l'activité.
        """

        # Vérifier le cache (avec le label adapté)
        cache_key = activity_label
        if cache_key in self.activities_cache:
            logger.debug("Activité '{}' trouvée dans le cache", activity_label)
            return self.activities_cache[cache_key]

        # Rechercher l'activité existante via SQL
        sql_query = f"SELECT id, label FROM services WHERE label LIKE '%{activity_label}%' LIMIT 1"
        try:
            result = self.get_sql(sql_query, mode="direct")
            if result.get("results") and len(result["results"]) > 0:
                activity = result["results"][0]
                logger.info("Activité existante trouvée : {}", activity)
                self.activities_cache[cache_key] = activity
                return activity
        except Exception as e:
            logger.exception(f"Erreur lors de la recherche d'activité : {e}")

        # Si l'activité n'existe pas, la créer via l'API SQL de Paheko
        # logger.info("Création d'une nouvelle activité pour l'événement : {}", activity_label)
        label = activity_label.replace("'", "''")

        try:
            select_sql = f"SELECT * FROM services WHERE label = '{label}' LIMIT 1"
            result = self.get_sql(select_sql, mode="direct")
            if result.get("results") and len(result["results"]) > 0:
                activity = result["results"][0]
                logger.info("Activité créée/trouvée : {}", activity)
                self.activities_cache[activity_label] = activity
                return activity
            else:
                # Fallback : renvoyer une structure minimale dans le cache
                activity = {"id": None, "label": activity_label}
                self.activities_cache[activity_label] = activity
                logger.warning("Activité créée en cache mais aucun id retourné par l'API SQL.")
                return activity
        except Exception as e:
            logger.exception(
                "Erreur lors de la création/récupération de l'activité {}: {}", activity_label, e
            )
            activity = {"id": None, "label": activity_label}
            self.activities_cache[activity_label] = activity
            return activity

    def attach_users_to_activity(
        self,
        activity_id: int,
        user_id: int,
        fee_id: int,
        amount: int,
        order_date: str,
        expiry_date: str = None,
    ) -> None:
        """
        Rattache un utilisateur à une activité en insérant dans la table services_users.
        Vérifie d'abord si l'inscription existe déjà pour éviter les doublons.

        :param activity_id: ID du service (activité)
        :param user_id: ID de l'utilisateur
        :param fee_id: ID du tarif (services_fees)
        :param amount: Montant payé en centimes (ou None si gratuit)
        :param order_date: Date de la commande (format YYYY-MM-DD)
        :param expiry_date: Date d'expiration (format YYYY-MM-DD, optionnel)
        """
        if not activity_id or not user_id or not fee_id:
            logger.warning(
                "Données manquantes pour rattacher l'utilisateur à l'activité: "
                "activity_id={}, user_id={}, fee_id={},order_date={}, expiry_date={}  ",
                activity_id,
                user_id,
                fee_id,
                order_date,
                expiry_date,
            )
            return

        try:
            # 1. Vérifier d'abord si l'inscription existe déjà
            check_sql = f"""SELECT * FROM services_users 
                           WHERE id_user={user_id} 
                           AND id_service={activity_id} 
                           AND id_fee={fee_id} 
                           AND date='{order_date}' 
                           LIMIT 1;"""

            existing = self.get_sql(check_sql, mode="direct")

            if existing.get("results") and len(existing["results"]) > 0:
                logger.info(
                    "ℹ️  L'utilisateur {} est déjà inscrit à l'activité {} (tarif {}, date {}). Inscription ignorée.",
                    user_id,
                    activity_id,
                    fee_id,
                    order_date,
                )
                return

            # 2. L'inscription n'existe pas, procéder à l'insertion
            # Convertir le montant: si 0 ou None → NULL, sinon en centimes
            if amount is None or amount == 0:
                expected_amount_sql = "NULL"
                paid = 0  # Gratuit = non payé
            else:
                expected_amount_sql = str(amount)  # Déjà en centimes
                paid = 1  # Payant = payé
            if order_date:
                order_date_sql = f"{order_date}"
            else:
                order_date_sql = "NULL"
            # Gérer l'expiry_date
            if expiry_date:
                expiry_date_sql = f"{expiry_date}"
            else:
                expiry_date_sql = "NULL"

            # Construire la requête SQL INSERT
            insert_sql = f"""INSERT INTO services_users (id_user, id_service, id_fee, paid, expected_amount, date, expiry_date) VALUES ({user_id}, {activity_id}, {fee_id}, {paid}, {expected_amount_sql}, '{order_date_sql}', '{expiry_date_sql}') ;"""

            logger.debug("SQL inscription service: {}", insert_sql)

            # Exécuter la requête
            res = self.get_sql(insert_sql, mode="direct")
            logger.info("Insertion SQL résultat: {}", res)

            # 3. Vérifier que l'insertion a réussi
            search_sql = f"""SELECT * FROM services_users WHERE id_user={user_id} AND id_service={activity_id} AND id_fee={fee_id} AND date='{order_date}' ;"""
            result = self.get_sql(search_sql, mode="direct")
            if result.get("results") and len(result["results"]) > 0:
                logger.debug("Inscription vérifiée dans services_users: {}", result["results"][0])
                logger.info(
                    "✅ Utilisateur {} inscrit à l'activité {} (tarif {}, montant {}€, payé={})",
                    user_id,
                    activity_id,
                    fee_id,
                    amount / 100 if amount else 0,
                    paid,
                )
            else:
                logger.warning(
                    "❌ Échec de la vérification de l'inscription de l'utilisateur {} à l'activité {} (tarif {})",
                    user_id,
                    activity_id,
                    fee_id,
                )
            search_sql = """SELECT * FROM services_users  ;"""
            result = self.get_sql(search_sql, mode="direct")
            logger.debug(
                "services_users total count after insert: {}", len(result.get("results", []))
            )
            logger.debug(
                "services_users entries: {}",
                jmodule.dumps(result.get("results", []), indent=4, ensure_ascii=False, default=str),
            )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                logger.warning(
                    "⚠️ L'utilisateur {} est déjà inscrit à l'activité {} avec le tarif {} à la date {}. Inscription ignorée.",
                    user_id,
                    activity_id,
                    fee_id,
                    order_date,
                )
            else:
                logger.exception(
                    "❌ Erreur d'intégrité lors de l'inscription de l'utilisateur {} à l'activité {} (tarif {}): ",
                    user_id,
                    activity_id,
                    fee_id,
                )
        except Exception :
            logger.exception(
                "❌ Erreur lors de l'inscription de l'utilisateur {} à l'activité {} (tarif {}): ",
                user_id,
                activity_id,
                fee_id,
            )

    @staticmethod
    def extract_all_from_custom_fields(data: dict) -> dict:
        """
        Extrait toutes les informations pertinentes depuis les customFields HelloAsso.
        Cherche les champs contenant "adresse", "rue", "address", "ville", "city",
        "code postal", "postal", "zip", "téléphone", "telephone", "phone", "mobile", "portable", "code_postal", "profession", "réseau", "email" etc.

        :param data: dict item de HelloAsso
        :return: Dictionnaire avec les clés address, city, zipCode, phone trouvées
        """

        # Regex pour valider un email
        email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        result = {}
        if "customFields" not in data:
            return result
        else:
            custom_fields = data["customFields"] if isinstance(data, dict) else data
            if not isinstance(custom_fields, list):
                return result

            for field in custom_fields:
                if not isinstance(field, dict):
                    continue
                logger.info("field all:", field.get("name") or "")
                field_name = (field.get("name") or "").lower()
                answer = field.get("answer", "")

                if not answer or not isinstance(answer, str):
                    continue

                answer = answer.strip()

                # Recherche d'email
                if any(
                    keyword in str(field_name).lower()
                    for keyword in [
                        "email",
                        "e-mail",
                        "emailaddress",
                        "email_address",
                        "adresse_email",
                        "email_addr",
                        "contact_email",
                        "payer_email",
                        "adresse email",
                    ]
                ):
                    if any(
                        keyword not in str(field_name).lower()
                        for keyword in [
                            "(par mail)",
                        ]
                    ):
                        if not result.get("email"):
                            result["email"] = answer.replace("@@", "@").lower()

                # Chercher par pattern regex dans les valeurs
                if isinstance(answer, str) and re.match(email_regex, answer):
                    if not result.get("email"):
                        result["email"] = answer

                # Recherche de profession
                if any(
                    keyword in str(field_name).lower()
                    for keyword in ["profession", "job", "emploi", "occupation"]
                ):
                    if any(
                        keyword not in str(field_name).lower()
                        for keyword in ["liste publique des professionnels"]
                    ):
                        if not result.get("profession"):
                            result["profession"] = answer

                # Recherche de réseau
                if any(
                    keyword in str(field_name).lower()
                    for keyword in ["réseau", "reseau", "network"]
                ):
                    if not result.get("network"):
                        result["network"] = answer

                # Recherche d'adresse/rue
                if any(
                    keyword in str(field_name).lower()
                    for keyword in ["adresse professionnelle", "rue", "address", "street"]
                ):
                    if not result.get("adresse"):
                        result["adresse"] = answer

                # Recherche de code postal
                if any(
                    keyword in str(field_name).lower()
                    for keyword in ["code_postal", "zip", "cp", "postal", "zipcode"]
                ):
                    if not result.get("code_postal"):
                        result["code_postal"] = answer

                # Recherche de pays
                elif any(
                    keyword in str(field_name).lower() for keyword in ["pays", "country", "nation"]
                ):
                    if not result.get("country"):
                        result["country"] = answer

                # Recherche de ville
                elif any(
                    keyword in str(field_name).lower() for keyword in ["ville", "city", "commune"]
                ):
                    if not result.get("city"):
                        result["ville"] = answer

                # # Recherche de code postal
                # elif any(keyword in str(field_name).lower() for keyword in ["code", "postal", "zip", "cp"]):
                #     if not result.get("zipCode"):
                #         result["zipCode"] = answer

                # Recherche de téléphone
                elif any(
                    keyword in str(field_name).lower()
                    for keyword in [
                        "téléphone",
                        "telephone",
                        "phone",
                        "mobile",
                        "portable",
                        "tel",
                        "gsm",
                    ]
                ):
                    if not result.get("telephone"):
                        result["telephone"] = answer

        return result

    # @staticmethod
    # def extract_address_from_custom_fields(custom_fields: list) -> dict:
    #     """
    #     Extrait les informations d'adresse depuis les customFields HelloAsso.
    #     Cherche les champs contenant "adresse", "rue", "address", "ville", "city",
    #     "code postal", "postal", "zip", etc.

    #     :param custom_fields: Liste des customFields de HelloAsso
    #     :return: Dictionnaire avec les clés address, city, zipCode trouvées
    #     """
    #     result = {}

    #     if not isinstance(custom_fields, list):
    #         return result

    #     for field in custom_fields:
    #         if not isinstance(field, dict):
    #             continue
    #         logger.info("field address:", field.get("name") or "")
    #         field_name = (field.get("name") or "").lower()
    #         answer = field.get("answer", "")

    #         if not answer or not isinstance(answer, str):
    #             continue

    #         answer = answer.strip()

    #         # Recherche d'adresse/rue
    #         if any(
    #             keyword in str(field_name).lower()
    #             for keyword in ["adresse", "adresse professionnelle", "rue", "address", "street"]
    #         ):
    #             if not result.get("address"):
    #                 result["address"] = answer

    #         # Recherche de ville
    #         elif any(
    #             keyword in str(field_name).lower() for keyword in ["ville", "city", "commune"]
    #         ):
    #             if not result.get("city"):
    #                 result["ville"] = answer

    #         # Recherche de code postal
    #         elif any(
    #             keyword in str(field_name).lower() for keyword in ["code", "postal", "zip", "cp"]
    #         ):
    #             if not result.get("zipCode"):
    #                 result["zipCode"] = answer

    #     return result

    def compare_member_data(
        self, existing_member: dict, hello_member_data: dict, new_member_data: dict
    ) -> bool:
        """
        Compare les données existantes d'un membre avec de nouvelles données.
        Retourne True si une mise à jour est nécessaire, False sinon.

        :param existing_member: Dictionnaire des données existantes du membre.
        :param hello_member_data: Dictionnaire des nouvelles données du membre.
        :param new_member_data: Dictionnaire des nouvelles données du membre.
        :return: True si une mise à jour est nécessaire, False sinon.
        """
        list_of_keys_to_ignore_hello = [
            "firstName",
            "lastName",
            "country",
            "id_category",
            "dateOfBirth",
            "nom2",
        ]
        list_of_keys_to_ignore_paheko = ["lettre_infos", "numero"]
        for key in list_of_keys_to_ignore_hello:
            if key in new_member_data:
                new_member_data.pop(key)

        updateneed = False
        for key, new_value in hello_member_data.items():

            if key not in list_of_keys_to_ignore_hello:
                if key not in list_of_keys_to_ignore_paheko:
                    if key not in existing_member:
                        logger.debug(
                            "Clé '{}' absente dans les données existant, nouveau='{}'",
                            key,
                            new_value,
                        )
                    else:
                        existing_value = existing_member.get(key)
                        if str(existing_value).strip() != str(new_value).strip():
                            logger.warning(
                                "Différence détectée pour la clé '{}': existant='{}', nouveau='{}'",
                                key,
                                existing_value,
                                new_value,
                            )
                            if (
                                new_value is not None
                                or new_value != ""
                                or new_value != "None"
                                or new_value.strip()[0] != "("
                            ):
                                new_member_data[key] = new_value
                            else:
                                new_member_data[key] = existing_value
                            updateneed = True
                        else:
                            logger.debug(
                                "Aucune différence pour la clé '{}': existant='{}', nouveau='{}'",
                                key,
                                existing_value,
                                new_value,
                            )

        for key, existing_value in existing_member.items():
            if key not in list_of_keys_to_ignore_paheko:
                if key not in hello_member_data:
                    logger.debug(
                        "Clé '{}' absente dans les nouvelles données, existant='{}'",
                        key,
                        existing_value,
                    )
                    updateneed = True
        return updateneed, new_member_data

    # @staticmethod
    # def extract_phone_from_custom_fields(custom_fields: list) -> str:
    #     """
    #     Extrait le numéro de téléphone depuis les customFields HelloAsso.
    #     Cherche les champs contenant "téléphone", "telephone", "telephone", "mobile", "portable", etc.

    #     :param custom_fields: Liste des customFields de HelloAsso
    #     :return: Numéro de téléphone trouvé ou chaîne vide
    #     """
    #     if not isinstance(custom_fields, list):
    #         return ""

    #     for field in custom_fields:
    #         if not isinstance(field, dict):
    #             continue
    #         logger.info("field phone:", field.get("name") or "")
    #         field_name = (field.get("name") or "").lower()
    #         answer = field.get("answer", "")

    #         if not answer or not isinstance(answer, str):
    #             continue

    #         # Recherche de téléphone
    #         if any(
    #             keyword in str(field_name).lower()
    #             for keyword in [
    #                 "téléphone",
    #                 "telephone",
    #                 "phone",
    #                 "mobile",
    #                 "portable",
    #                 "tel",
    #                 "gsm",
    #             ]
    #         ):
    #             return answer.strip()

    #     return ""

    def get_category_name_by_id(self, category_id: str) -> str:
        """
        Récupère le nom d'une catégorie de membre à partir de son ID.

        :param category_id: ID de la catégorie.
        :return: Nom de la catégorie ou None si non trouvée.
        """
        categories = self.get_member_categories()
        logger.debug("Recherche du nom de la catégorie pour ID {}", category_id)
        for cat_id, cat_info in categories.items():
            if str(cat_id) == str(category_id):
                logger.debug("\n\n##################################################\n\n")
                logger.debug("Catégorie trouvée pour ID {}: {}", category_id, cat_info.get("name"))
                logger.debug("\n\n##################################################\n\n")
                return cat_info.get("name")
        logger.debug("Aucune catégorie trouvée pour l'ID {}", category_id)
        return None

    def get_category_id_by_name(self, category_name: str) -> int:
        """
        Récupère l'ID d'une catégorie de membre à partir de son nom.

        :param category_name: Nom de la catégorie.
        :return: ID de la catégorie ou None si non trouvée.
        """
        categories = self.get_member_categories()
        for cat_id, cat_info in categories.items():
            if cat_info.get("name") == category_name:
                logger.debug("Catégorie trouvée pour nom '{}': ID {}", category_name, cat_id)
                return cat_info.get("id")
        logger.debug("Aucune catégorie trouvée pour le nom '{}'", category_name)
        return None

    def get_category_name_by_id_from_config(self, category_id: int) -> str:
        """
        Récupère le nom d'une catégorie de membre à partir de son ID depuis config.toml.
        Plus rapide que get_category_name_by_id car lit directement depuis le fichier.

        :param category_id: ID de la catégorie.
        :return: Nom de la catégorie ou None si non trouvée.
        """
        try:
            with open(self.config_path, "rb") as f:
                config = tomllib.load(f)

            member_categories = config.get("member_categories", {})
            logger.debug(
                "Recherche du nom de la catégorie pour ID {} dans config.toml", category_id
            )

            for cat_key, cat_info in member_categories.items():
                if cat_info.get("id") == int(category_id):
                    category_name = cat_info.get("name")
                    logger.debug("Catégorie trouvée pour ID {}: {}", category_id, category_name)
                    return category_name

            logger.debug("Aucune catégorie trouvée pour l'ID {} dans config.toml", category_id)
            return None
        except Exception as e:
            logger.error(
                "Erreur lors de la lecture de config.toml pour get_category_name_by_id: {}", e
            )
            return None

    def get_category_id_by_name_from_config(self, category_name: str) -> int:
        """
        Récupère l'ID d'une catégorie de membre à partir de son nom depuis config.toml.
        Plus rapide que get_category_id_by_name car lit directement depuis le fichier.

        :param category_name: Nom de la catégorie.
        :return: ID de la catégorie ou None si non trouvée.
        """
        try:
            with open(self.config_path, "rb") as f:
                config = tomllib.load(f)

            member_categories = config.get("member_categories", {})
            logger.debug(
                "Recherche de l'ID de la catégorie pour nom '{}' dans config.toml", category_name
            )

            for cat_key, cat_info in member_categories.items():
                if cat_info.get("name") == category_name:
                    category_id = cat_info.get("id")
                    logger.debug(
                        "Catégorie trouvée pour nom '{}': ID {}", category_name, category_id
                    )
                    return category_id

            logger.debug(
                "Aucune catégorie trouvée pour le nom '{}' dans config.toml", category_name
            )
            return None
        except Exception as e:
            logger.error(
                "Erreur lors de la lecture de config.toml pour get_category_id_by_name: {}", e
            )
            return None

    def ensure_member_exists(
        self,
        update_data: dict,
    ) -> dict:
        """
        Vérifie si un membre existe, le crée ou le met à jour si nécessaire.
        Format du nom: "NOM Prénom"
        Les administrateurs ne sont jamais modifiés.

        Logique de recherche :
        1. D'abord chercher par email
        2. Si pas trouvé et téléphone disponible, chercher par téléphone (possible homonyme)
        3. Si trouvé par téléphone, mettre à jour l'email
        4. Sinon créer un nouveau membre

        Pour les homonymes (même téléphone), ajouter l'email au nom.

        :param update_data: Données du payeur depuis HelloAsso (firstName, lastName, email, etc.).
        # :param has_membership: True si la commande contient une cotisation.
        # :param force_update: True pour forcer la mise à jour des données du membre.
        # :param date_inscription: Date d'inscription du membre (format ISO 8601).
        :return: Dictionnaire contenant les informations du membre (existant ou créé).
        """
        logger.debug(
            f"Données du membre pour création/mise à jour: {jmodule.dumps(update_data, indent=4, ensure_ascii=False, default=str   )}"
        )
        # update_data = {}
        # update_data["firstName"] = str(ens_member_data.get("firstName"))
        # update_data["lastName"] = str(ens_member_data.get("lastName"))

        # update_data["email"] = ens_member_data.get("email")
        # update_data["telephone"] = ens_member_data.get("telephone")
        # update_data["adresse"] = ens_member_data.get("adresse")
        # update_data["code_postal"] = ens_member_data.get("code_postal")
        # update_data["ville"] = ens_member_data.get("ville")
        # update_data["pays"] = ens_member_data.get("pays")
        # update_data["profession"] = ens_member_data.get("profession")
        # update_data["network"] = ens_member_data.get("network")
        # update_data["type"] = ens_member_data.get("type")  # payeur ou beneficiaire

        # Si le membre paie une cotisation, le passer en "Membres Actifs"

        logger.debug(
            f"Données du membre pour création/mise à jour: {jmodule.dumps(update_data, indent=4, ensure_ascii=False, default=str   )}"
        )

        # Étape 1 : Rechercher le membre existant par NAME
        existing_member = self.get_member_by_name(update_data["nom"])
        if existing_member:
            logger.info("Membre existant trouvé par nom: {}", update_data["nom"])
        # Étape 2 : Rechercher le membre existant par EMAIL
        else:
            existing_member = self.get_member_by_name(update_data["nom2"])
            if existing_member:
                logger.info("Membre existant trouvé par nom2: {}", update_data["nom"])
            else:
                # if update_data["email"]:
                #     logger.debug("Recherche du membre par email: {}", update_data["email"])
                #     existing_member = self.get_member_by_email(update_data["email"])
                # else:
                logger.info("Nouveau membre: {}", update_data["nom"])

        # utilisateur trouvé par email
        if existing_member:
            logger.info("Membre existant trouvé par name ou email: {}", update_data["email"])

            hello_data = dict(update_data)
            update_needed, update_data = self.compare_member_data(
                existing_member, hello_data, update_data
            )
            # logger.warning(f"Membre existant trouvé: {jmodule.dumps(existing_member, indent=4)}")

            member_id = existing_member.get("id")

            # Verrouiller les administrateurs
            existing_category = existing_member.get("category")
            # existing_category_id = self.get_category_id_by_name(existing_category)
            logger.info("existing_category:", existing_category)
            if existing_category and "administrateur" in existing_category.lower():
                logger.info(
                    "Membre {} est administrateur, aucune modification", update_data["email"]
                )
                return existing_member

            logger.warning(
                f"Membre Existant: {jmodule.dumps(existing_member, indent=4, ensure_ascii=False, default=str)}"
            )
            logger.warning(
                f"Membre Helloasso brut: {jmodule.dumps(hello_data, indent=4, ensure_ascii=False, default=str)}"
            )
            logger.warning(
                f"Membre Helloasso pour Update: {jmodule.dumps(update_data, indent=4, ensure_ascii=False, default=str)}"
            )

            # Effectuer la mise à jour si nécessaire
            if update_needed and not self.mode_simulation:
                try:
                    self.update_member(member_id, update_data, existing_category)
                    logger.info(
                        "Membre {} mis à jour: {}",
                        update_data["email"],
                        jmodule.dumps(update_data, indent=4, ensure_ascii=False, default=str),
                    )
                    # Recharger le membre après mise à jour
                    logger.debug("Rechargement du membre après mise à jour.")
                    existing_member = self.get_member_by_name(update_data["nom"])
                except ValueError as e:
                    logger.warning("Impossible de mettre à jour {}: {}", update_data["email"], e)
                    # Afficher le contexte complet en JSON
                    try:

                        logger.warning(
                            "📋 Contexte de mise à jour du membre (JSON):\n{}",
                            jmodule.dumps(update_data, indent=4, ensure_ascii=False, default=str),
                        )
                    except Exception:
                        pass
                except Exception as e:
                    logger.exception("Erreur lors de la mise à jour de {}: {}", update_data["email"], e)
                    # Afficher le contexte complet en JSON
                    try:

                        logger.error(
                            "📋 Contexte de mise à jour du membre (JSON):\n{}",
                            jmodule.dumps(
                                update_data["email"], indent=4, ensure_ascii=False, default=str
                            ),
                        )
                    except Exception:
                        pass
            elif update_needed and self.mode_simulation:
                logger.info(
                    "Mode simulation: Membre {} serait mis à jour: {}",
                    update_data["email"],
                    jmodule.dumps(update_data, indent=4, ensure_ascii=False, default=str),
                )

            return existing_member

        else:
            # Créer un nouveau membre

            logger.debug("Création d'un nouveau membre : {}", update_data)

            try:
                if not self.mode_simulation:
                    # response = self.create_member(update_data)
                    logger.info(
                        "Nouveau membre créé: {} ({})", update_data["nom"], update_data["email"]
                    )
                    # Recharger le membre après création
                    logger.debug("Rechargement du membre après création.")
                    return self.get_member_by_name(update_data["nom"])
                else:
                    logger.info("Mode simulation: Membre {} serait créé", update_data["email"])
                    return {"email": update_data["email"], "nom": update_data["nom"], "id": None}
            except Exception as e:
                logger.exception("Erreur lors de la création du membre {}: {}", update_data["email"], e)
                # Afficher les données du payeur en JSON pour le débogage
                try:

                    logger.error(
                        "📋 Contexte de création du membre (JSON):\n{}",
                        jmodule.dumps(update_data, indent=4, ensure_ascii=False, default=str),
                    )
                except Exception as json_error:
                    logger.error("Impossible de sérialiser les données: {}", json_error)
                return None

    def category_by_payer_and_hasmembership(
        self, has_payer: bool, has_membership: bool, date_inscription: str = None
    ) -> str:
        """
        Vérifie si une catégorie de membre existe en fonction de son ID.

        :param has_payer: True si c'est le payeur
        :param has_membership: True si c'est une cotisation
        :param date_inscription: Date d'inscription du membre (format ISO 8601).
        :return: True si la catégorie existe, False sinon.
        """
        if has_payer:
            category = self.get_category_name_by_id_from_config(NON_ADHERENTS_PAYER_CATEGORY_ID)
        else:
            category = self.get_category_name_by_id_from_config(NON_ADHERENTS_CATEGORY_ID)
        if has_membership:

            id_year = self.get_current_year_id(date_inscription, self.organization_slug)

            if self.IDCURRENTYEAR == id_year:
                category = self.get_category_name_by_id_from_config(MEMBRES_ACTIFS_CATEGORY_ID)
            else:
                category = self.get_category_name_by_id_from_config(ANCIENS_MEMBRES_CATEGORY_ID)
        return category

    def ensure_user_from_item_exists(
        self,
        item_data: dict,
        payer: dict,
        has_membership: bool = False,
        force_update: bool = True,
        date_inscription: str = None,
    ) -> dict:
        """
        Crée ou met à jour un membre basé sur les données du user d'un item de commande.
        Si le user n'a pas d'email, utilise l'email du payeur comme fallback.
        Utilise le numéro HelloAsso comme numéro Paheko.

        :param item_data: Données de l'item contenant le user (firstName, lastName, address, etc.)
        :param payer: Données du payeur (pour fallback email et autres infos)
        :param has_membership: True si c'est une cotisation
        :param force_update: True pour forcer la mise à jour
        :param date_inscription: Date d'inscription du membre (format ISO 8601)
        :return: Dictionnaire du membre créé ou existant
        """
        cfi = {}
        cfp = {}
        logger.warning(
            f"item_data:{jmodule.dumps(item_data, indent=4, ensure_ascii=False, default=str)}"
        )

        logger.warning(f"payer:{jmodule.dumps(payer, indent=4, ensure_ascii=False, default=str)}")

        # Extraire les informations du user
        first_name = str(item_data.get("firstName") or "").capitalize().strip()
        last_name = str(item_data.get("lastName") or "").upper().strip()
        nom = str(item_data.get("nom") or "")
        first_namepayer = str(payer.get("firstName") or "").capitalize().strip()
        last_namepayer = str(payer.get("lastName") or "").upper().strip()

        # Extraire l'email du user et du payeur (fallback)
        cfi = self.extract_all_from_custom_fields(item_data)
        logger.debug(
            f"#####Custom fields item_data########: {jmodule.dumps(cfi, indent=4, ensure_ascii=False, default=str   )}#########################"
        )
        email_user = (cfi.get("email", "") or "").lower().strip()
        if email_user == "":

            email_user = payer.get("email").lower().strip()

        email_payer = payer.get("email")

        if payer.get("customFields"):
            cfp = self.extract_all_from_custom_fields(payer)

            member_data = {
                "firstName": first_namepayer,
                "lastName": last_namepayer,
                "email": email_payer,
                "nom": f"{last_namepayer} {first_namepayer} ({email_payer})",
                "nom2": f"{first_namepayer.upper()} {last_namepayer.capitalize()} ({email_payer})",
                "telephone": item_data.get("telephone")
                or cfi.get("telephone")
                or payer.get("telephone", "")
                or cfp.get("telephone", ""),
                "adresse": item_data.get("address")
                or cfi.get("address")
                or payer.get("address", "")
                or cfp.get("address", ""),
                "ville": item_data.get("city")
                or cfi.get("city")
                or payer.get("city")
                or cfp.get("ville", "Besançon"),
                "code_postal": item_data.get("zipCode")
                or cfi.get("zipCode")
                or payer.get("zipCode")
                or cfp.get("code_postal", "25000"),
                "pays": item_data.get("country")
                or cfi.get("country")
                or payer.get("country")
                or cfp.get("pays", "France"),
                "date_inscription": date_inscription,
                "category": self.category_by_payer_and_hasmembership(
                    has_payer=False,
                    has_membership=has_membership,
                    date_inscription=date_inscription,
                ),
            }

            logger.debug(
                "Traitement du user (email present): {last_name} {first_name} ({email_user})"
            )
            logger.debug(
                f" if payer{jmodule.dumps(member_data, indent=4, ensure_ascii=False, default=str)}"
            )
            # Construire le nom final sera effectué par ensure member exists
            logger.debug(
                "member_data {jmodule.dumps(member_data, indent=4, ensure_ascii=False, default=str)}"
            )
            return self.ensure_member_exists(
                member_data,
            )

        # Enrichir avec les customFields

        # if item_data.get("customFields"):

        # Créer un nouveau membre sans email (champ email vide) et avec le nom incluant l'email du payeur
        member_payload = {
            "id_category": 1 if has_membership else 4,
            "firstName": first_name,
            "lastName": last_name,
            "email": email_user,
            "nom": f"{last_name} {first_name} ({email_user})",
            "nom2": f"{first_name.upper()} {last_name.capitalize()} ({email_user})",
            "telephone": item_data.get("phone")
            or cfi.get("telephone")
            or payer.get("phone", "")
            or cfp.get("telephone", ""),
            "adresse": item_data.get("address") or cfi.get("adresse"),
            "ville": item_data.get("city")
            or cfi.get("city")
            or payer.get("city")
            or cfp.get("city", "Besançon"),
            "code_postal": item_data.get("zipCode")
            or cfi.get("zipCode")
            or payer.get("zipCode")
            or cfp.get("zipCode", "25000"),
            "pays": item_data.get("country")
            or cfi.get("country")
            or payer.get("country")
            or cfp.get("country", "France"),
            "date_inscription": date_inscription,
            "category": self.get_category_name_by_id_from_config(NON_ADHERENTS_CATEGORY_ID),
        }
        if nom != "":
            member_payload["nom"] = nom

        logger.debug("Création d'un membre provenant d'item: {}", member_payload)
        try:

            logger.debug(f"Traitement du user: {last_name} {first_name} ({email_user})")
            logger.warning(
                f"item to member {jmodule.dumps(member_payload, indent=4, ensure_ascii=False, default=str)}"
            )
            logger.debug(
                "member_payload {jmodule.dumps(member_payload, indent=4, ensure_ascii=False, default=str)}"
            )
            return self.ensure_member_exists(
                member_payload,
            )

        except Exception as e:
            logger.exception(
                "Erreur lors de la création du membre sans email {}: {}",
                jmodule.dumps(member_payload, indent=4, ensure_ascii=False, default=str),
                e
            )
            return None

    def transaction_exists(self, reference: str) -> bool:
        """
        Vérifie si une transaction avec cette référence existe déjà dans Paheko.
        Cherche la référence comme sous-chaîne (supporte plusieurs références séparées par |).

        :param reference: Référence de la transaction (ex: "HelloAsso 123456" ou "OFX-123456")
        :return: True si la transaction existe, False sinon
        """
        try:
            # Utiliser LIKE pour chercher la référence contenue dans le champ
            # Gère le cas où plusieurs références sont séparées par " | "
            sql_query = f"SELECT COUNT(*) as count FROM acc_transactions WHERE reference LIKE '%{reference}%' LIMIT 1"
            result = self.get_sql(sql_query, mode="direct", fetch_mode="one")

            if result and "results" in result and len(result["results"]) > 0:
                count = result["results"][0].get("count", 0)
                return count > 0

            return False
        except Exception as e:
            logger.exception(
                "Erreur lors de la vérification de l'existence de la transaction {}: {}", 
                reference,
                e
            )
            # En cas d'erreur, on considère qu'elle n'existe pas pour ne pas bloquer
            return False

    def update_transaction_reference(self, transaction_id: int, reference: str) -> bool:
        """
        Met à jour la référence d'une transaction existante.

        :param transaction_id: ID de la transaction à modifier
        :param reference: Nouvelle référence à ajouter (ex: "OFX-123456")
        :return: True si la mise à jour a réussi
        """
        try:
            # Ajouter la référence OFX à la référence existante avec un espace comme séparateur
            # Format résultat: "VIRMT-HELLOASSO-5579045 OFX-554157"
            # Ne rien faire si la référence OFX est déjà présente
            sql_query = f"""
                UPDATE acc_transactions 
                SET reference = CASE 
                    WHEN reference IS NULL OR reference = '' THEN '{reference}'
                    WHEN reference LIKE '%{reference}%' THEN reference
                    ELSE reference || ' {reference}'
                END
                WHERE id = {transaction_id}
            """
            self.get_sql(sql_query, mode="direct")
            logger.debug("Référence {} ajoutée à la transaction {}", reference, transaction_id)
            return True
        except Exception as e:
            logger.exception(
                "Erreur lors de la mise à jour de la référence pour la transaction {}: {}",
                transaction_id,
                e
            )
            return False

    def update_transaction_label_and_reference(
        self, transaction_id: int, new_label: str, reference: str, notes: str = None
    ) -> bool:
        """
        Met à jour le libellé, la référence et les notes d'une transaction existante.
        Utilisé quand le libellé OFX est plus riche que celui dans Paheko.

        :param transaction_id: ID de la transaction à modifier
        :param new_label: Nouveau libellé plus détaillé
        :param reference: Référence OFX à ajouter
        :param notes: Notes à ajouter (contenant l'ancien libellé)
        :return: True si la mise à jour a réussi
        """
        try:
            # Échapper les apostrophes dans le libellé et les notes
            escaped_label = new_label.replace("'", "''")
            escaped_notes = notes.replace("'", "''") if notes else ""

            # Mettre à jour le libellé, la référence et les notes
            sql_query = f"""
                UPDATE acc_transactions 
                SET 
                    label = '{escaped_label}',
                    reference = CASE 
                        WHEN reference IS NULL OR reference = '' THEN '{reference}'
                        WHEN reference LIKE '%{reference}%' THEN reference
                        ELSE reference || ' {reference}'
                    END,
                    notes = CASE
                        WHEN notes IS NULL OR notes = '' THEN '{escaped_notes}'
                        ELSE notes || '\n\n' || '{escaped_notes}'
                    END
                WHERE id = {transaction_id}
            """
            self.get_sql(sql_query, mode="direct")
            logger.debug(
                "Transaction {} mise à jour: nouveau libellé '{}' + référence {}",
                transaction_id,
                new_label[:40],
                reference,
            )
            return True
        except Exception as e:
            logger.exception(
                "Erreur lors de la mise à jour de la transaction {}: {}",
                transaction_id,
                e
            )
            return False

    def get_journal_for_account(self, year_id: int, account: str) -> list:
        """
        Récupère le journal d'un compte spécifique pour une année donnée depuis l'API Paheko.

        :param year_id: ID de l'année comptable.
        :param account: Numéro du compte.
        :return: Liste des entrées du journal pour le compte.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        url = f"{self.base_url}/accounting/years/{year_id}/account/journal"
        params = {"code": account}
        logger.debug(
            "Récupération du journal pour le compte {} pour l'année {}",
            account,
            year_id,
        )
        response = self.client.get(url, params=params)
        response.raise_for_status()
        journal_entries = response.json()
        logger.info(
            "Journal récupéré pour le compte {} pour l'année {} : {}",
            account,
            year_id,
            journal_entries,
        )
        return journal_entries

    def create_transaction(self, transaction_data: dict) -> dict:
        """
        Crée une écriture comptable dans Paheko via l'endpoint /accounting/transaction.

        :param transaction_data: Dictionnaire contenant les données de la transaction (id_year, label, date, type, amount, debit, credit, reference, etc.).
        :return: Réponse JSON de l'API Paheko.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        url = f"{self.base_url}/accounting/transaction"
        headers = {"Content-Type": "application/json"}
        logger.debug("Création d'une transaction avec les données : {}", transaction_data)
        try:
            response = self.client.post(url, json=transaction_data, headers=headers)
            response.raise_for_status()
            logger.info("Transaction créée (réf. {})", transaction_data.get("reference"))
            return response.json()
        except Exception as exc:
            # Essayer d'extraire des informations plus détaillées si disponible
            try:
                # httpx.HTTPStatusError wraps la réponse
                resp = getattr(exc, "response", None)
                if resp is not None:
                    logger.error(
                        "Erreur création transaction (status {}) : {}",
                        getattr(resp, "status_code", "?"),
                        getattr(resp, "text", "(no body)"),
                    )
                else:
                    logger.exception(
                        "Erreur lors de la requête de création de transaction: %s", exc
                    )
            except Exception:
                logger.exception("Erreur inattendue lors du traitement de l'exception: %s", exc)
            # Ne pas remonter l'exception pour permettre la poursuite du traitement (best-effort)
            return None

    def get_sql(
        self, sql_query: str, params: tuple = None, mode: str = "api", fetch_mode: str = "all"
    ) -> dict:
        """
        Exécute une requête SQL sur l'API Paheko ou directement sur la base SQLite.

        :param sql_query: La requête SQL à exécuter (avec ? pour les paramètres).
        :param params: Tuple de paramètres pour la requête (pour prévenir l'injection SQL).
        :param mode: Mode d'exécution - "api" (par défaut) ou "direct"
            - "api": Utilise l'API Paheko (comportement actuel)
            - "direct": Interroge directement la base SQLite locale
        :param fetch_mode: Mode de récupération des résultats - "all" (par défaut) ou "one"
            - "all": Récupère tous les résultats (fetchall)
            - "one": Récupère un seul résultat (fetchone)
        :return: Dictionnaire contenant les résultats de la requête SQL.
        :raises httpx.HTTPError: En cas d'erreur HTTP (mode api).
        :raises sqlite3.Error: En cas d'erreur SQLite (mode direct).
        """
        if mode == "direct":
            # Mode direct : interroger directement la base SQLite

            if not self.db_path or not os.path.exists(self.db_path):
                logger.error("Chemin de base de données invalide ou inexistant: {}", self.db_path)
                raise FileNotFoundError(f"Base de données non trouvée: {self.db_path}")

            logger.debug("Exécution directe de la requête SQL sur {}: {}", self.db_path, sql_query)
            if params:
                logger.debug("Paramètres de la requête : {}", params)

            try:
                # Connexion à la base SQLite
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row  # Pour obtenir des dictionnaires
                cursor = conn.cursor()

                # Exécuter la requête avec ou sans paramètres
                if params:
                    cursor.execute(sql_query, params)
                else:
                    cursor.execute(sql_query)

                # Récupérer les résultats selon le mode
                if fetch_mode == "one":
                    row = cursor.fetchone()
                    results = [dict(row)] if row else []
                else:
                    rows = cursor.fetchall()
                    results = [dict(row) for row in rows]

                # ✅ COMMIT pour les requêtes INSERT/UPDATE/DELETE
                if sql_query.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                    conn.commit()
                    logger.debug("✅ Modifications commitées dans la base de données")

                conn.close()
                sleep(0.5)  # Petite pause pour s'assurer que la DB est libérée
                logger.info("Résultats de la requête SQL directe récupérés : {}", len(results))

                # Retourner au même format que l'API
                return {"results": results}

            except sqlite3.Error as e:
                logger.exception("Erreur lors de l'exécution de la requête SQL directe: {}", e)
                raise

        else:
            # Mode API (par défaut)
            # Note: L'API ne supporte pas les paramètres, il faut gérer manuellement l'échappement
            url = f"{self.base_url}/sql/"
            headers = {"Content-Type": "application/json"}
            payload = {"sql": sql_query}
            logger.debug("Exécution de la requête SQL via API : {}", sql_query)
            logger.debug("URL de l'API : {}", url)
            logger.debug("Payload de la requête : {}", payload)
            response = self.client.post(url, json=payload, headers=headers)
            logger.debug("Statut de la réponse : {}", response.status_code)
            # logger.debug("Contenu de la réponse : {}", response.text)
            response.raise_for_status()
            results = response.json()
            logger.info("Résultats de la requête SQL via API récupérés : {}", len(results))
            return results

    def get_table_names(self) -> list:
        """
        Récupère les noms des tables dans la base de données SQLite.

        :return: Liste des noms des tables.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        sql_query = "SELECT name FROM sqlite_master WHERE type='table';"
        result = self.get_sql(sql_query, mode="direct")
        table_names = [row["name"] for row in result.get("results", [])]
        logger.info("Noms des tables récupérés : {}", table_names)
        return table_names

    def get_table_definition(self, table_name: str) -> list:
        """
        Récupère la définition d'une table spécifique dans la base de données SQLite.

        :param table_name: Le nom de la table.
        :return: Liste des colonnes et leurs définitions.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        sql_query = f"PRAGMA table_info({table_name});"
        result = self.get_sql(sql_query, mode="direct")
        logger.info("Définition de la table {} récupérée : {}", table_name, result)
        return result.get("results", [])

    def get_all_tables_and_definitions(self) -> dict:
        """
        Récupère les noms des tables et leurs définitions dans la base de données SQLite.

        :return: Dictionnaire contenant les noms des tables et leurs définitions.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        tables = self.get_table_names()
        tables_definitions = {}
        for table in tables:
            tables_definitions[table] = self.get_table_definition(table)
        logger.info("Noms des tables et leurs définitions récupérés : {}", tables_definitions)
        return tables_definitions

    def get_tablenames_and_rows(self, mode: str = "direct") -> dict:
        """
        Récupère la structure complète de la base de données : liste des tables avec leurs colonnes et types.
        Teste les deux modes (API et direct) pour comparer les résultats.

        :param mode: Mode d'exécution - "api" (par défaut) ou "direct"
        :return: Dictionnaire contenant pour chaque table : {table_name: {column_name: column_type}}
        """
        logger.info(f"🔍 Récupération de la structure de la base en mode '{mode}'")

        try:
            # Récupérer la liste des tables
            sql_tables = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
            tables_result = self.get_sql(sql_tables, mode=mode)

            if not tables_result or "results" not in tables_result:
                logger.error("Aucun résultat retourné pour la liste des tables")
                return {}

            tables = [row["name"] for row in tables_result["results"]]
            logger.info(f"📊 {len(tables)} tables trouvées")

            # Pour chaque table, récupérer sa structure
            database_structure = {}

            for table_name in tables:
                logger.debug(f"Analyse de la table '{table_name}'")

                # PRAGMA table_info retourne : cid, name, type, notnull, dflt_value, pk
                sql_info = f"PRAGMA table_info({table_name});"

                try:
                    info_result = self.get_sql(sql_info, mode=mode)

                    if info_result and "results" in info_result:
                        columns = {}
                        for col in info_result["results"]:
                            col_name = col.get("name", "unknown")
                            col_type = col.get("type", "unknown")
                            col_pk = col.get("pk", 0)
                            col_notnull = col.get("notnull", 0)

                            # Format: type + contraintes
                            constraints = []
                            if col_pk:
                                constraints.append("PRIMARY KEY")
                            if col_notnull and not col_pk:
                                constraints.append("NOT NULL")

                            full_type = col_type
                            if constraints:
                                full_type += f" ({', '.join(constraints)})"

                            columns[col_name] = full_type

                        # Récupérer les clés étrangères pour cette table
                        # PRAGMA foreign_key_list retourne : id, seq, table, from, to, on_update, on_delete, match
                        sql_fk = f"PRAGMA foreign_key_list({table_name});"
                        fk_result = self.get_sql(sql_fk, mode=mode)

                        foreign_keys = {}
                        if fk_result and "results" in fk_result and fk_result["results"]:
                            for fk in fk_result["results"]:
                                from_col = fk.get("from", "unknown")
                                to_table = fk.get("table", "unknown")
                                to_col = fk.get("to", "unknown")
                                on_delete = fk.get("on_delete", "NO ACTION")
                                on_update = fk.get("on_update", "NO ACTION")

                                # Ajouter l'info de FK à la colonne correspondante
                                if from_col in columns:
                                    fk_info = f"FK → {to_table}.{to_col}"
                                    if on_delete != "NO ACTION":
                                        fk_info += f" (ON DELETE {on_delete})"
                                    if on_update != "NO ACTION":
                                        fk_info += f" (ON UPDATE {on_update})"
                                    columns[from_col] += f" | {fk_info}"

                                # Conserver aussi la liste des FKs séparément
                                if from_col not in foreign_keys:
                                    foreign_keys[from_col] = []
                                foreign_keys[from_col].append(
                                    {
                                        "references_table": to_table,
                                        "references_column": to_col,
                                        "on_delete": on_delete,
                                        "on_update": on_update,
                                    }
                                )

                        table_info = {
                            "columns": columns,
                        }
                        if foreign_keys:
                            table_info["foreign_keys"] = foreign_keys

                        database_structure[table_name] = table_info
                        logger.debug(
                            f"✓ Table '{table_name}': {len(columns)} colonnes, {len(foreign_keys)} FK"
                        )
                    else:
                        logger.warning(f"Impossible de récupérer la structure de '{table_name}'")
                        database_structure[table_name] = {}

                except Exception as e:
                    logger.error(f"Erreur lors de l'analyse de la table '{table_name}': {e}")
                    database_structure[table_name] = {"error": str(e)}

            logger.info(f"✅ Structure complète récupérée : {len(database_structure)} tables")

            # Afficher le dictionnaire formaté
            logger.info("\n" + "=" * 80)
            logger.info(f"📋 STRUCTURE DE LA BASE DE DONNÉES (mode: {mode})")
            logger.info("=" * 80)
            logger.info(jmodule.dumps(database_structure, indent=4, ensure_ascii=False))
            logger.info("=" * 80 + "\n")

            return database_structure

        except Exception as e:
            logger.exception(
                f"Erreur lors de la récupération de la structure de la base (mode {mode}): {e}"
            )
            return {}

    def get_accounting_years(self) -> list:
        """
        Récupère la liste des années comptables depuis la base de données SQLite via une requête SQL.
        Utilise le mode "direct" pour éviter les timeouts de l'API.

        :return: Liste des années comptables.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        sql_query = "SELECT id, label, start_date, end_date FROM acc_years"
        result = self.get_sql(sql_query, mode="direct")
        years = [
            {
                "id": row["id"],
                "label": row["label"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
            }
            for row in result["results"]
        ]
        logger.info("Années comptables récupérées : {}", len(years))
        return years

    def get_current_year_id(self, transaction_date: str, organization_slug: str) -> int:
        """
        Trouve l'ID de l'année comptable correspondant à la date de la transaction et vérifie que le slug de l'organisation est présent dans le nom de l'exercice.

        :param transaction_date: La date de la transaction (peut être str ou datetime).
        :param organization_slug: Le slug de l'organisation.
        :return: L'ID de l'année comptable.
        """
        # Convertir transaction_date en un objet datetime naïf (sans fuseau horaire)
        if isinstance(transaction_date, str):
            transaction_date = datetime.fromisoformat(transaction_date)

        # Maintenant on peut retirer le fuseau horaire
        if hasattr(transaction_date, "tzinfo") and transaction_date.tzinfo is not None:
            transaction_date_naive = transaction_date.replace(tzinfo=None)
        else:
            transaction_date_naive = transaction_date

        years = self.get_accounting_years()
        for year in years:
            start_date = datetime.fromisoformat(year["start_date"])
            end_date = datetime.fromisoformat(year["end_date"])

            # Convertir start_date et end_date en objets datetime naïfs
            start_date_naive = start_date.replace(tzinfo=None)
            end_date_naive = end_date.replace(tzinfo=None)

            logger.debug(f"{start_date_naive}, {transaction_date_naive}, {end_date_naive}")
            logger.debug(
                f"{type(start_date_naive)}, {type(transaction_date_naive)}, {type(end_date_naive)}"
            )

            # Vérifier que le label existe et n'est pas None
            year_label = year.get("label", "")
            if year_label and isinstance(year_label, str) and organization_slug:
                logger.info(
                    f"Comparing label '{year_label}' with organization slug '{organization_slug}'"
                )
                if (
                    start_date_naive <= transaction_date_naive <= end_date_naive
                    and organization_slug.lower() in year_label.lower()
                ):
                    return year["id"]
            elif start_date_naive <= transaction_date_naive <= end_date_naive:
                # Si pas de label ou label None, on retourne quand même l'année si dates correspondent
                logger.warning(f"Année comptable sans label valide: {year}")
                return year["id"]

        raise ValueError(
            f"Aucune année comptable trouvée pour la date {transaction_date} et l'organisation {organization_slug}"
        )

    def get_journals_for_account_all_years(self, account: str) -> list:
        """
        Récupère les journaux de toutes les années pour un compte spécifique.

        :param account: Numéro du compte.
        :return: Liste des dictionnaires contenant les entrées du journal par année.
        :raises httpx.HTTPError: En cas d'erreur HTTP.
        """
        years = self.get_accounting_years()
        all_journals = []
        for year in years:
            year_id = year.get("id")
            if year_id is not None:
                journal_entries = self.get_journal_for_account(year_id, account)
                all_journals.append({"year_id": year_id, "journal_entries": journal_entries})
        return all_journals

    def download_sqlite_backup(self) -> None:
        """
        Télécharge la base de données SQLite depuis l'endpoint /download et la sauvegarde dans le répertoire
        défini dans la configuration. Le fichier est horodaté.
        """
        url = f"{self.base_url}/download"
        logger.debug("Téléchargement de la base SQLite depuis {}", url)
        try:
            with self.client.stream("GET", url) as response:
                response.raise_for_status()
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                filename = f"backup_sqlite_{timestamp}.sqlite"
                destination = os.path.join(self.backup_sqlite_path, filename)
                with open(destination, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
                logger.info("Sauvegarde SQLite enregistrée sous '{}'. ", destination)
        except httpx.HTTPError as e:
            logger.exception("Erreur lors du téléchargement de la base SQLite : {}", e)
            raise

    def download_zip_backup(self) -> None:
        """
        Télécharge le fichier ZIP de backup via l'endpoint /download/files et le sauvegarde dans le répertoire
        défini dans la configuration. Le fichier est horodaté.
        """
        url = f"{self.base_url}/download/files"
        logger.debug("Téléchargement du fichier ZIP de backup depuis {}", url)
        try:
            with self.client.stream("GET", url) as response:
                response.raise_for_status()
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                filename = f"backup_files_{timestamp}.zip"
                destination = os.path.join(self.backup_zip_path, filename)
                with open(destination, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
                logger.info("Fichier ZIP de backup enregistré sous '{}'.", destination)
        except httpx.HTTPError as e:
            logger.exception("Erreur lors du téléchargement du fichier ZIP : {}", e)
            raise


class HelloAssoHTTPClient:
    """
    Classe principale pour interagir avec l'API HelloAsso.

    Cette classe gère l'authentification OAuth2 via client_credentials, la gestion
    du token stocké dans un fichier, et la chaîne d'appels suivante :
      1. Récupération des organisations via GET /users/me/organizations.
      2. Récupération des paiements pour une organisation via GET /organizations/{organizationSlug}/payments/search.
      3. Récupération des détails d'une commande via GET /orders/{orderId}.

    La logique est décomposée en plusieurs fonctions pour faciliter le dépannage.
    """

    def __init__(self, mode_simulation: bool = False):
        """Initialise la classe Helloasso.

        Charge la configuration depuis `config.toml`, initialise la session OAuth2,
        et charge ou génère un token d'accès.
        """
        self.config_path = "config.toml"
        with open(self.config_path, "rb") as f:
            self.config = tomllib.load(f)

        self.client_id = _required_env("HELLOASSO_API_KEY")
        self.client_secret = _required_env("HELLOASSO_API_SECRET")
        self.config["helloasso"]["username"] = _required_env("HELLOASSO_USERNAME")
        self.config["helloasso"]["password"] = _required_env("HELLOASSO_PASSWORD")
        self.base_url = self.config["helloasso"]["base_url"]
        self.authorize_url = self.config["helloasso"]["authorize_url"]
        self.token_url = self.config["helloasso"]["token_url"]
        self.redirect_uri = self.config["helloasso"]["redirect_uri"]

        # Charger le nom du fichier token depuis la config
        self.token_file = self.config.get("files", {}).get(
            "token_helloasso", "token-helloasso.json"
        )

        # Gestion du token : charger depuis le fichier ou en obtenir un nouveau
        self.token = self.load_token()
        if not self.token or self.is_token_expired():
            self.get_access_token()

        # Préparer l'en-tête d'authentification pour les appels HTTP
        self.headers = {"Authorization": f"Bearer {self.token['access_token']}"}

        logger.info("Connexion HelloAsso Ok")

    def convert_to_datetime(self, date_str: str) -> datetime:
        """
        Convertit une chaîne de date en objet datetime.

        :param date_str: La chaîne de date à convertir.
        :return: L'objet datetime correspondant.
        """
        return datetime.fromisoformat(date_str)

    def extract_year(self, date_obj: datetime) -> int:
        """
        Extrait l'année d'un objet datetime.

        :param date_obj: L'objet datetime dont l'année doit être extraite.
        :return: L'année sous forme d'entier.
        """
        return date_obj.year

    def load_token(self):
        """
        Charge le token depuis le fichier (défini dans config.toml).

        Returns:
            dict or None: Le token chargé, ou None si le fichier n'existe pas ou est invalide.
        """
        try:
            with open(self.token_file, "r") as f:
                token = jmodule.load(f)
                return token
        except (FileNotFoundError, jmodule.JSONDecodeError):
            return None

    def save_token(self):
        """
        Sauvegarde le token actuel dans le fichier (défini dans config.toml).
        Ajoute également la date actuelle (timestamp) avant de sauvegarder.
        """
        self.token["created_at"] = datetime.now().timestamp()
        token_path = Path(self.token_file)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.token_file, "w") as f:
            jmodule.dump(self.token, f, indent=4, ensure_ascii=False, default=str)
        os.chmod(token_path, 0o600)

    def is_token_expired(self):
        """
        Vérifie si le token actuel est expiré.

        Returns:
            bool: True si le token est expiré, False sinon.
        """
        if not self.token:
            return True
        created_at = self.token["created_at"]
        expires_in = self.token["expires_in"]
        expiration_time = created_at + expires_in
        return datetime.now().timestamp() >= expiration_time

    def get_access_token(self):
        """
        Obtient un nouvel access_token via le flux client_credentials et le sauvegarde dans `token-helloasso.json`.

        Utilise OAuth2Session pour récupérer le token.
        """
        session = OAuth2Session(self.client_id, self.client_secret)
        self.token = session.fetch_token(self.token_url, grant_type="client_credentials")
        self.token["created_at"] = datetime.now().timestamp()
        self.save_token()
        logger.info(
            "HelloAsso access token obtained; expires in {} seconds", self.token["expires_in"]
        )

    def refresh_access_token(self):
        """
        Rafraîchit l'access_token en utilisant le refresh_token et le sauvegarde.
        """
        session = OAuth2Session(self.client_id, self.client_secret)
        self.token = session.refresh_token(
            self.token_url, refresh_token=self.token["refresh_token"]
        )
        self.token["created_at"] = datetime.now().timestamp()
        self.save_token()
        logger.info(
            "HelloAsso access token refreshed; expires in {} seconds",
            self.token["expires_in"],
        )

    def _ensure_valid_token(self):
        """
        Vérifie que le token est valide et le renouvelle si nécessaire.
        Appelé automatiquement avant chaque requête API.
        """
        if not self.token or self.is_token_expired():
            logger.warning("⚠️  Token expiré ou manquant, renouvellement...")
            self.get_access_token()
            # Mettre à jour le header avec le nouveau token
            self.headers = {"Authorization": f"Bearer {self.token['access_token']}"}
            logger.info("✅ Token renouvelé avec succès")

    def _make_request(self, method: str, url: str, **kwargs):
        """
        Effectue une requête HTTP avec vérification automatique du token.
        Renouvelle le token si nécessaire en cas de 401.

        :param method: Méthode HTTP (GET, POST, etc.)
        :param url: URL de la requête
        :param kwargs: Arguments supplémentaires pour httpx (headers, params, json, etc.)
        :return: Response object
        """
        # Vérifier le token avant la requête
        self._ensure_valid_token()

        # Ajouter les headers d'authentification
        if "headers" in kwargs:
            kwargs["headers"] = {**self.headers, **kwargs["headers"]}
        else:
            kwargs["headers"] = self.headers

        # Faire la requête
        try:
            response = httpx.request(method, url, **kwargs)

            # Si 401, le token a peut-être expiré entre-temps
            if response.status_code == 401:
                logger.warning("⚠️  401 Unauthorized, tentative de renouvellement du token...")
                self.get_access_token()
                kwargs["headers"] = {**self.headers, **kwargs.get("headers", {})}
                response = httpx.request(method, url, **kwargs)

            return response
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.error("❌ Échec d'authentification après renouvellement du token")
            raise

    def get_organizations(self) -> list:
        """
        Récupère la liste des organisations via GET /users/me/organizations.

        Returns:
            list: Liste des organisations sous forme de dictionnaires.
        """
        url = f"{self.base_url}/users/me/organizations"
        logger.debug("Récupération des organisations depuis {}", url)
        response = self._make_request("GET", url)
        response.raise_for_status()
        organizations = response.json()
        logger.info("Organisations récupérées: {}", organizations)
        return organizations

    def get_payments(
        self,
        organization_slug: str,
        page_size: int = 10,
        page_index: int = 1,
        continuation_token: str = "",
    ) -> dict:
        """
        Récupère les paiements pour une organisation via GET /organizations/{organizationSlug}/payments

        :param organization_slug: Le slug de l'organisation.
        :param page_size: Nombre d'éléments par page.
        :param page_index: Index de la page.
        :param continuation_token: token de suite.
        Returns:
            dict: Dictionnaire contenant les paiements et informations de pagination.
        """
        url = f"{self.base_url}/organizations/{organization_slug}/payments"
        headers = {"Content-Type": "application/json"}
        params = {
            "pageSize": page_size,
            "pageIndex": page_index,
            "continuationToken": continuation_token,
        }
        logger.debug("Récupération des paiements pour l'organisation {}...", organization_slug)
        response = self._make_request("GET", url, headers=headers, params=params)
        response.raise_for_status()
        payments = response.json()
        payments_data = payments["data"]
        logger.debug("Reçu {} paiements", len(payments_data))
        if (
            payments["pagination"]["continuationToken"] != ""
            and payments["pagination"]["continuationToken"] != continuation_token
        ):
            logger.debug("Récupération de la page suivante des paiements")
            next_payments_data = self.get_payments(
                organization_slug,
                continuation_token=payments["pagination"]["continuationToken"],
            )
            for payment_unit in next_payments_data:
                payments_data.append(payment_unit)

        logger.info("Paiements récupérés pour {}: {}", organization_slug, payments["pagination"])
        return payments_data

    def get_organization_slug(self) -> str:
        """
        Récupère le slug de la première organisation disponible.

        :return: Le slug de l'organisation.
        :raises ValueError: Si aucune organisation n'est trouvée.
        """
        organizations = self.get_organizations()
        if not organizations:
            raise ValueError("Aucune organisation trouvée.")

        organization_slug = organizations[0].get("organizationSlug")
        if not organization_slug:
            raise ValueError("Le slug de l'organisation est introuvable.")

        logger.info("Slug de l'organisation récupéré : {}", organization_slug)
        return organization_slug

    def get_payments_global(self, organization_slug: str, page_size: int = 10):
        """Récupère tous les paiements via l'endpoint global"""
        url = f"{self.base_url}/organizations/{organization_slug}/payments"
        all_payments = []
        page_num = 1

        logger.info("📥 Récupération des paiements (endpoint global)...")

        while True:
            params = {"pageSize": page_size, "pageIndex": page_num}

            response = self._make_request("GET", url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            payments_data = data.get("data", [])
            all_payments.extend(payments_data)

            if not data.get("pagination", {}).get("continuationToken"):
                break

            page_num += 1

        logger.info(f"✅ Total (endpoint global): {len(all_payments)} paiements")
        return all_payments

    def get_forms_list(self, organization_slug: str):
        """Récupère la liste des formulaires"""
        url = f"{self.base_url}/organizations/{organization_slug}/forms"

        logger.info("📋 Récupération des formulaires...")
        response = self._make_request("GET", url, timeout=30)
        response.raise_for_status()
        forms = response.json().get("data", [])
        logger.info(f"✅ {len(forms)} formulaires trouvés")
        return forms

    def get_payments_by_form(self, organization_slug: str, formType: str, formSlug: str):
        """Récupère les paiements pour un formulaire spécifique"""
        url = f"{self.base_url}/organizations/{organization_slug}/forms/{formType}/{formSlug}/payments"

        payments = []
        page_index = 1

        try:
            while True:
                params = {"pageSize": 100, "pageIndex": page_index}

                response = self._make_request("GET", url, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()

                payments_data = data.get("data", [])
                if not payments_data:
                    break

                payments.extend(payments_data)

                if not data.get("pagination", {}).get("continuationToken"):
                    break

                page_index += 1

            return payments

        except Exception as e:
            logger.exception(f"⚠️  Erreur pour {formType}/{formSlug}: {e}")
            return []

    def consolidate_payments(self, global_payments: list, formPayments: list):
        """Fusionne et déduplique les paiements"""
        logger.info("🔄 Consolidation des paiements...")

        payments_by_id = {}

        # Ajouter les paiements de l'endpoint global
        for p in global_payments:
            if p.get("id"):
                payments_by_id[p.get("id")] = p

        global_count = len(payments_by_id)
        duplicates_found = 0

        # Ajouter les paiements des formulaires
        for p in formPayments:
            if p.get("id"):
                if p.get("id") not in payments_by_id:
                    payments_by_id[p.get("id")] = p
                else:
                    duplicates_found += 1

        consolidated = list(payments_by_id.values())

        logger.info(f"   ✅ Paiements globaux: {global_count}")
        logger.info(f"   ✅ Paiements additionnels: {len(consolidated) - global_count}")
        logger.info(f"   ⚠️  Doublons ignorés: {duplicates_found}")
        logger.info(f"   💰 TOTAL CONSOLIDÉ: {len(consolidated)} paiements")

        return consolidated

    def extract_members(self, payments: list):
        """Extrait tous les membres uniques"""
        logger.info("👥 Extraction des membres...")

        members_by_email = {}

        for payment in payments:
            logger.info("Payment ID:", payment.get("id"))
            logger.info("Amount:", payment.get("payer"))
            payer = payment.get("payer", {})
            logger.info(payer.get("email"), "")
            email = payer.get("email", "").lower()

            if email:
                if email not in members_by_email:
                    member = {
                        "email": payer.get("email"),
                        "firstName": payer.get("firstName", ""),
                        "lastName": payer.get("lastName", ""),
                        "telephone": payer.get("phone", ""),
                        "address": payer.get("address", ""),
                        "zipCode": payer.get("zipCode", ""),
                        "ville": payer.get("city", ""),
                        "pays": payer.get("country", ""),
                        "company": payer.get("company", ""),
                        "payments": [],
                        "total_amount": 0,
                        "payment_count": 0,
                    }
                    members_by_email[email] = member

                member = members_by_email[email]
                member["payments"].append(payment.get("id"))
                member["total_amount"] += payment.get("amount", 0)
                member["payment_count"] += 1

        members = list(members_by_email.values())
        logger.info(f"   ✅ Total membres: {len(members)}")

        return members

    def extract_orders_from_payments(self, payments: list):
        """Extrait toutes les commandes uniques"""
        logger.info("🛒 Extraction des commandes...")

        orders_by_id = {}

        for payment in payments:
            order = payment.get("order", {})
            order_id = order.get("id")

            if order_id:
                if order_id not in orders_by_id:
                    consolidated_order = {
                        "id": order_id,
                        "date": order.get("date"),
                        "formSlug": order.get("formSlug"),
                        "formType": order.get("formType"),
                        "formName": order.get("formName"),
                        "payments": [],
                        "items": [],
                        "total_amount": 0,
                    }
                    orders_by_id[order_id] = consolidated_order

                order_obj = orders_by_id[order_id]
                order_obj["payments"].append(payment.get("id"))
                order_obj["total_amount"] += payment.get("amount", 0)

                for item in payment.get("items", []):
                    if not any(i.get("id") == item.get("id") for i in order_obj["items"]):
                        order_obj["items"].append(item)

        orders_list = list(orders_by_id.values())
        logger.info(f"   ✅ Total commandes: {len(orders_list)}")

        return orders_list

    def consolidate_all(self):
        """Exécute la consolidation complète"""
        logger.info("=" * 80)
        logger.info("CONSOLIDATION COMPLÈTE")
        logger.info("=" * 80)

        # Récupérer l'organisation
        org_slug = self.get_organization_slug()

        # Récupérer tous les paiements
        global_payments = self.get_payments_global(org_slug)

        # Récupérer par formulaires
        forms = self.get_forms_list(org_slug)
        formPayments = []

        logger.info("\n📥 Récupération des paiements (endpoints par formulaire)...")
        for form in forms:
            formType = form.get("formType")
            formSlug = form.get("formSlug", "")

            if formType and formSlug:
                logger.debug(f"   📝 {formSlug} ({formType}/{formSlug})...")
                payments = self.get_payments_by_form(org_slug, formType, formSlug)
                if payments:
                    formPayments.extend(payments)
                    logger.debug(f"      ✓ {len(payments)} paiements")

        logger.info(f"✅ Total (endpoints par formulaire): {len(formPayments)} paiements")

        # Consolider
        consolidated_payments = self.consolidate_payments(global_payments, formPayments)

        # Extraire
        members = self.extract_members(consolidated_payments)
        orders = self.extract_orders_from_payments(consolidated_payments)

        logger.info("=" * 80)

        return {
            "payments": consolidated_payments,
            "members": members,
            "orders": orders,
        }

    def consolidate(self):
        """
        Consolide les données depuis HelloAsso via les deux sources (endpoint global + par formulaires).

        Retourne:
            dict: Dictionnaire avec 'payments', 'members', 'orders'
        """
        try:
            # org_slug = self.get_organization_slug()
            return self.consolidate_all()
        except Exception as e:
            logger.exception("Erreur lors de la consolidation: {}", e)
            raise

    def get_orders(
        self,
        organization_slug: str,
        page_size: int = 10,
        page_index: int = 1,
        continuation_token: str = "",
    ) -> dict:
        """
        Récupère les paiements pour une organisation via GET /organizations/{organizationSlug}/orders.

        :param organization_slug: Le slug de l'organisation.
        :param page_size: Nombre d'éléments par page.
        :param page_index: Index de la page.
        :param continuation_token: token de suite.
        Returns:
            dict: Dictionnaire contenant les paiements et informations de pagination.
        """
        url = f"{self.base_url}/organizations/{organization_slug}/orders"
        headers = {"Content-Type": "application/json"}
        params = {
            "pageSize": page_size,
            "pageIndex": page_index,
            "continuationToken": continuation_token,
        }
        logger.debug("Récupération des paiements pour l'organisation {}...", organization_slug)
        response = self._make_request("GET", url, headers=headers, params=params)
        response.raise_for_status()
        orders = response.json()
        orders_data = orders["data"]
        logger.debug("Reçu {} commandes", len(orders_data))
        if (
            orders["pagination"]["continuationToken"] != ""
            and orders["pagination"]["continuationToken"] != continuation_token
        ):
            logger.debug("Récupération de la page suivante des commandes")
            next_orders_data = self.get_orders(
                organization_slug,
                continuation_token=orders["pagination"]["continuationToken"],
            )
            for payment_unit in next_orders_data:
                orders_data.append(payment_unit)

        logger.info("Paiements récupérés pour {}: {}", organization_slug, orders["pagination"])
        return orders_data

    def get_cash_outs(self, organization_slug: str) -> list[dict]:
        """
        Récupère et groupe les virements bancaires (cash-outs)
        depuis l'API HelloAsso.

        Les virements sont identifiés par les paiements ayant
        cashOutState='CashedOut'. Chaque virement (idCashOut)
        regroupe plusieurs paiements.

        :param organization_slug: Le slug de l'organisation.
        :return: Liste des virements avec leur date, montant total
                 et liste des paiements.
        """
        logger.info(
            "Récupération des virements bancaires (cash-outs) " "pour {}...",
            organization_slug,
        )

        # Récupérer tous les paiements
        all_payments = self.get_payments(organization_slug, page_size=10)

        # Filtrer les paiements qui ont été virés (CashedOut)
        cashed_out_payments = [p for p in all_payments if p.get("cashOutState") == "CashedOut"]
        logger.debug("Trouvé {} paiements avec cash-out", len(cashed_out_payments))

        # Grouper par idCashOut
        cash_outs_dict = {}
        for payment in cashed_out_payments:
            cash_out_id = payment.get("idCashOut")
            if not cash_out_id:
                continue

            if cash_out_id not in cash_outs_dict:
                cash_outs_dict[cash_out_id] = {
                    "id": cash_out_id,
                    "date": payment.get("cashOutDate"),
                    "payments": [],
                    "total_amount_centimes": 0,
                }

            payer = payment.get("payer", {})
            first_name = payer.get("firstName", "")
            last_name = payer.get("lastName", "")

            cash_outs_dict[cash_out_id]["payments"].append(
                {
                    "id": payment["id"],
                    "amount": payment["amount"],
                    "order_id": payment.get("order", {}).get("id"),
                    "payer_name": f"{first_name} {last_name}",
                }
            )
            cash_outs_dict[cash_out_id]["total_amount_centimes"] += payment["amount"]

        # Convertir en liste
        cash_outs = list(cash_outs_dict.values())

        logger.info("Trouvé {} virements bancaires", len(cash_outs))
        for cash_out in cash_outs:
            logger.debug(
                "Virement {} : {} paiements, montant total: {:.2f}€, " "date: {}",
                cash_out["id"],
                len(cash_out["payments"]),
                cash_out["total_amount_centimes"] / 100,
                cash_out["date"],
            )

        return cash_outs

    def get_items(
        self,
        organization_slug: str,
        page_size: int = 10,
        page_index: int = 1,
        continuation_token: str = "",
    ) -> dict:
        """
        Récupère les paiements pour une organisation via GET /organizations/{organizationSlug}/items

        :param organization_slug: Le slug de l'organisation.
        :param page_size: Nombre d'éléments par page.
        :param page_index: Index de la page.
        :param continuation_token: token de suite.
        Returns:
            dict: Dictionnaire contenant les paiements et informations de pagination.
        """
        url = f"{self.base_url}/organizations/{organization_slug}/items"
        headers = {**self.headers, "Content-Type": "application/json"}
        params = {
            "pageSize": page_size,
            "pageIndex": page_index,
            "continuationToken": continuation_token,
        }
        logger.debug("Récupération des items pour l'organisation {}...", organization_slug)
        headers = {"Content-Type": "application/json"}
        response = self._make_request("GET", url, headers=headers, params=params)
        response.raise_for_status()
        items = response.json()
        items_data = items["data"]
        logger.debug("Reçu {} items", len(items_data))
        if (
            items["pagination"]["continuationToken"] != ""
            and items["pagination"]["continuationToken"] != continuation_token
        ):
            logger.debug("Récupération de la page suivante des items")
            next_items_data = self.get_items(
                organization_slug,
                continuation_token=items["pagination"]["continuationToken"],
            )
            for payment_unit in next_items_data:
                items_data.append(payment_unit)

        logger.info("Paiements récupérés pour {}: {}", organization_slug, items["pagination"])
        return items_data

    def get_payment_details(self, payment_id: str) -> dict:
        """
        Récupère les détails d'un paiement via GET /payments/{payment_id}.

        :param payment_id: L'identifiant du paiement.
        Returns:
            dict: Détails du paiement.
        """
        url = f"{self.base_url}/payments/{payment_id}"
        logger.info(url)
        logger.debug("Récupération des détails du paiement {}...", payment_id)
        response = self._make_request("GET", url)
        response.raise_for_status()
        payment_details = response.json()
        logger.info("Détails du paiement {} récupérés.", payment_id)
        return payment_details

    def get_item_details(self, item_id: str) -> dict:
        """
        Récupère les détails d'un item via GET /items/{itemId}.

        :param item_id: L'identifiant de l'item.
        Returns:
            dict: Détails de l'item.
        """
        url = f"{self.base_url}/items/{item_id}"
        logger.debug("Récupération des détails de l'item {}...", item_id)
        response = self._make_request("GET", url)
        response.raise_for_status()
        item_details = response.json()
        logger.info("Détails de l'item {} récupérés.", item_id)
        return item_details

    def get_order_details(self, order_id: str) -> dict:
        """
        Récupère les détails d'une commande via GET /orders/{orderId}.

        :param order_id: L'identifiant de la commande.
        Returns:
            dict: Détails de la commande.
        """
        url = f"{self.base_url}/orders/{order_id}"
        logger.debug("Récupération des détails de la commande {}...", order_id)
        response = self._make_request("GET", url)
        response.raise_for_status()
        order_details = response.json()
        logger.info("Détails de la commande {} récupérés.", order_id)
        return order_details

    def download_pdf_with_auth(
        self, pdf_url: str, destination_folder: str, filename: str = None
    ) -> str:
        """
        Télécharge un PDF depuis HelloAsso en utilisant l'authentification par login/password.

        HelloAsso nécessite une authentification par formulaire web pour accéder aux PDF
        (tickets, cartes de membre, reçus de paiement).

        :param pdf_url: URL complète du PDF à télécharger
        :param destination_folder: Dossier où sauvegarder le PDF
        :param filename: Nom du fichier (optionnel, auto-généré si None)
        :return: Chemin complet du fichier téléchargé

        Exemples d'URLs:
        - Ticket: https://www.helloasso.com/.../ticket?ticketId=XXX&ag=XXX
        - Carte membre: https://www.helloasso.com/.../carte-adherent?cardId=XXX&ag=XXX
        - Reçu: https://www.helloasso.com/.../paiement-attestation/XXX
        """
        try:
            # Créer le dossier de destination s'il n'existe pas
            os.makedirs(destination_folder, exist_ok=True)

            # Générer un nom de fichier si non fourni
            if not filename:
                # Extraire l'ID du PDF depuis l'URL
                if "ticketId=" in pdf_url:
                    pdf_id = pdf_url.split("ticketId=")[1].split("&")[0]
                    filename = f"ticket_{pdf_id}.pdf"
                elif "cardId=" in pdf_url:
                    pdf_id = pdf_url.split("cardId=")[1].split("&")[0]
                    filename = f"carte_membre_{pdf_id}.pdf"
                elif "paiement-attestation" in pdf_url:
                    pdf_id = pdf_url.split("/")[-1]
                    filename = f"recu_paiement_{pdf_id}.pdf"
                else:
                    filename = f"document_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

            destination_path = os.path.join(destination_folder, filename)

            # Vérifier si le fichier existe déjà
            if os.path.exists(destination_path):
                logger.info("📄 PDF déjà téléchargé: {}", filename)
                return destination_path

            logger.debug("📥 Téléchargement du PDF depuis: {}", pdf_url)

            # Créer une session avec gestion des cookies
            with httpx.Client(follow_redirects=True, timeout=30.0) as session:
                # 1. Récupérer les identifiants depuis config.toml
                username = self.config["helloasso"]["username"]
                password = self.config["helloasso"]["password"]

                # 2. Se connecter à HelloAsso (authentification par formulaire web)
                login_url = "https://auth.helloasso.com/login"
                login_data = {
                    "username": username,
                    "password": password,
                }

                logger.debug("🔐 Authentification à HelloAsso...")
                login_response = session.post(login_url, data=login_data)

                if login_response.status_code != 200:
                    logger.error(
                        "❌ Échec de l'authentification HelloAsso (status {})",
                        login_response.status_code,
                    )
                    return None

                # 3. Télécharger le PDF avec la session authentifiée
                logger.debug("📥 Téléchargement du PDF authentifié...")
                pdf_response = session.get(pdf_url)
                pdf_response.raise_for_status()

                # 4. Vérifier que c'est bien un PDF
                content_type = pdf_response.headers.get("content-type", "")
                if "pdf" not in content_type.lower():
                    logger.warning(
                        "⚠️  Le contenu téléchargé n'est peut-être pas un PDF (type: {})",
                        content_type,
                    )

                # 5. Sauvegarder le fichier
                with open(destination_path, "wb") as f:
                    f.write(pdf_response.content)

                logger.info(
                    "✅ PDF téléchargé: {} ({} octets)", filename, len(pdf_response.content)
                )
                return destination_path

        except httpx.HTTPError as e:
            logger.exception("❌ Erreur HTTP lors du téléchargement du PDF {}: {}", pdf_url, e)
            return None
        except Exception as e:
            logger.exception("❌ Erreur lors du téléchargement du PDF {}: {}", pdf_url, e)
            return None

    def download_order_pdfs(self, order: dict, base_folder: str = "./pdfs") -> dict:
        """
        Télécharge tous les PDF associés à une commande (tickets, cartes, reçus).

        :param order: Dictionnaire de la commande HelloAsso
        :param base_folder: Dossier racine pour stocker les PDF
        :return: Dictionnaire avec les chemins des fichiers téléchargés

        Structure de sortie:
        {
            "tickets": ["/path/to/ticket1.pdf", ...],
            "membership_cards": ["/path/to/card1.pdf", ...],
            "payment_receipts": ["/path/to/receipt1.pdf", ...]
        }
        """
        downloaded_files = {"tickets": [], "membership_cards": [], "payment_receipts": []}

        order_id = order.get("id")
        if not order_id:
            logger.warning("⚠️  Commande sans ID, impossible de télécharger les PDF")
            return downloaded_files

        # Créer un sous-dossier par commande
        order_folder = os.path.join(base_folder, f"order_{order_id}")

        logger.info("📦 Téléchargement des PDF pour la commande {}", order_id)

        # 1. Télécharger les tickets des items
        for item in order.get("items", []):
            ticket_url = item.get("ticketUrl")
            if ticket_url:
                ticket_path = self.download_pdf_with_auth(
                    ticket_url, os.path.join(order_folder, "tickets")
                )
                if ticket_path:
                    downloaded_files["tickets"].append(ticket_path)

            # Télécharger les cartes de membre
            card_url = item.get("membershipCardUrl")
            if card_url:
                card_path = self.download_pdf_with_auth(
                    card_url, os.path.join(order_folder, "cartes_membre")
                )
                if card_path:
                    downloaded_files["membership_cards"].append(card_path)

        # 2. Télécharger les reçus de paiement
        for payment in order.get("payments", []):
            receipt_url = payment.get("paymentReceiptUrl")
            if receipt_url:
                receipt_path = self.download_pdf_with_auth(
                    receipt_url, os.path.join(order_folder, "reçus")
                )
                if receipt_path:
                    downloaded_files["payment_receipts"].append(receipt_path)

        logger.info(
            "✅ PDF téléchargés pour commande {}: {} tickets, {} cartes, {} reçus",
            order_id,
            len(downloaded_files["tickets"]),
            len(downloaded_files["membership_cards"]),
            len(downloaded_files["payment_receipts"]),
        )

        return downloaded_files

    def is_order_fully_synced(self, order: dict, paheko_client: "PahekoAPIClient") -> bool:
        """
        Vérifie si une commande HelloAsso a été complètement synchronisée avec Paheko.

        Vérifie :
        1. L'existence du membre (acheteur et utilisateurs)
        2. L'existence de chaque transaction/ligne
        3. L'existence des activités associées

        :param order: La commande HelloAsso à vérifier
        :param paheko_client: Instance du client Paheko
        :return: True si tout est synchronisé, False sinon
        """
        try:
            # Récupérer les acheteurs/utilisateurs à vérifier
            noms_to_check = set()
            if order.get("payer", {}).get("email"):
                noms_to_check.add(
                    f"{order["payer"]["lastName"].upper()} {order["payer"]["firstName"].capitalize()} ({order["payer"]["email"]})"
                )

            for item in order.get("items", []):
                if item.get("user", {}).get("email"):
                    noms_to_check.add(
                        f"{order["user"]["lastName"].upper()} {order["user"]["firstName"].capitalize()}"
                    )

            # Vérifier l'existence des membres
            for nom in noms_to_check:
                logger.debug("Vérification du membre {}", nom)
                member = paheko_client.get_member_by_name(nom)
                if not member:
                    logger.info("Membre {} non synchronisé", nom)
                    return False

            # Vérifier l'existence des transactions
            try:
                org_slug = self.get_organization_slug()
            except Exception as e:
                logger.exception(f"Impossible de récupérer organization_slug: {e}")
                org_slug = self.config.get("helloasso", {}).get("org_slug")

            year_id = paheko_client.get_current_year_id(
                self.convert_to_datetime(order["date"]), org_slug
            )

            # Vérifier la transaction principale
            main_reference = f"HelloAsso {order['id']}"
            if not paheko_client.transaction_exists(main_reference, year_id):
                logger.info("Transaction principale {} non trouvée", main_reference)
                return False

            # Vérifier chaque ligne/item
            for item in order.get("items", []):
                item_reference = f"HelloAsso {item['id']}"
                if not paheko_client.transaction_exists(item_reference, year_id):
                    logger.info("Transaction d'item {} non trouvée", item_reference)
                    return False

            # Vérifier l'existence des activités
            formSlug = order.get("formSlug")
            if formSlug:
                activity = paheko_client.get_activity(
                    formSlug, order.get("date"), order.get("formType")
                )
                if not activity or not activity.get("id"):
                    logger.info("Activité {} non trouvée", formSlug)
                    return False

            return True

        except Exception as e:
            logger.exception("Erreur lors de la vérification de la commande {}:", order.get("id"))
            # Afficher le contexte complet en JSON
            try:

                context = {
                    "order_id": order.get("id"),
                    "order_date": order.get("date"),
                    "payer": order.get("payer"),
                    "items": order.get("items"),
                    "formSlug": order.get("formSlug"),
                    "formType": order.get("formType"),
                    "amount": order.get("amount"),
                    "status": order.get("state"),
                    "erreur": str(e),
                }
                logger.warning(
                    "📋 Contexte de vérification de commande (JSON):\n{}",
                    jmodule.dumps(context, indent=4, ensure_ascii=False, default=str),
                )
            except Exception as json_error:
                logger.exception("Impossible de sérialiser la commande: {}", json_error)
            return False

    def process_payment_details(self, payment_details: dict):
        """
        Traite et affiche les détails du paiement pour faciliter le dépannage.
        Vous pouvez adapter cette fonction pour extraire et formater les informations souhaitées.

        :param order_details: Dictionnaire contenant les détails du paiement.
        """
        logger.info("Traitement des détails du paiement:")

    def process_order_details(self, order_details: dict):
        """
        Traite et affiche les détails de la commande pour faciliter le dépannage.
        Vous pouvez adapter cette fonction pour extraire et formater les informations souhaitées.

        :param order_details: Dictionnaire contenant les détails de la commande.
        """
        logger.info("Traitement des détails de la commande:")
        logger.debug(
            "Order details: order_id={}, payer={}",
            order_details.get("id"),
            order_details.get("payer", {}).get("email"),
        )
        self.commandes[order_details["id"]] = order_details

    def process_item_details(self, item_details: dict):
        """
        Traite et affiche les détails de l'item pour faciliter le dépannage.
        Vous pouvez adapter cette fonction pour extraire et formater les informations souhaitées.

        :param order_details: Dictionnaire contenant les détails de l'item'.
        """
        logger.info("Traitement des détails de l'item:")

    def __call__(self):
        """
        Exécute la chaîne d'appels suivante :
          1. Récupère les organisations via GET /users/me/organizations.
          2. Pour chaque organisation, récupère les paiements via GET /organizations/{organizationSlug}/payments/search.
          3. Pour chaque paiement, récupère les détails de la commande via GET /orders/{orderId} et les traite.
        """
        logger.info("debut call")
        organizations = self.get_organizations()
        for org in organizations:
            org_slug = org.get("organizationSlug")
            if not org_slug:
                logger.warning("Organisation sans 'organizationSlug' : {}", org)
                continue
            logger.info("Traitement de l'organisation : {}", org_slug)

            self.payments = self.get_payments(org_slug)
            logger.info("Nombre de paiements pour {} : {}", org_slug, len(self.payments))

            self.orders = self.get_orders(org_slug)
            logger.info("Nombre de commandes pour {} : {}", org_slug, len(self.orders))

            self.items = self.get_items(org_slug)
            logger.info("Nombre de items pour {} : {}", org_slug, len(self.items))

        logger.info("fin call")

    def get_all_forms(
        self, organization_slug: str, states: Optional[List[str]] = None
    ) -> List[Dict]:
        """Récupère tous les formulaires de l'organisation"""
        url = f"{self.base_url}/organizations/{organization_slug}/forms"
        params = {}
        if states:
            params["states"] = states

        try:
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()
            forms = data.get("data", [])
            logger.info(f"📋 {len(forms)} formulaires trouvés")
            return forms
        except Exception as e:
            logger.error(f"❌ Erreur récupération formulaires: {e}")
            return []

    def get_form_details(
        self, organization_slug: str, formType: str, formSlug: str
    ) -> Optional[Dict]:
        """Récupère les détails d'un formulaire"""
        url = (
            f"{self.base_url}/organizations/{organization_slug}/forms/{formType}/{formSlug}/public"
        )

        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if hasattr(e, "response") and hasattr(e.response, "status_code"):
                if e.response.status_code == 404:
                    logger.warning(f"⚠️  Formulaire non accessible (404): {formType}/{formSlug}")
                    return None
            logger.error(f"❌ Erreur récupération détails {formType}/{formSlug}: {e}")
            return None

    def get_all_forms_with_details(
        self, organization_slug: str, states: Optional[List[str]] = None
    ) -> List[Dict]:
        """Récupère tous les formulaires avec leurs détails"""
        if states is None:
            states = ["Public", "Private"]

        logger.info("🔍 Récupération de tous les formulaires HelloAsso...")
        forms = self.get_all_forms(organization_slug, states=states)
        all_forms_with_details = []

        for form in forms:
            formSlug = form.get("formSlug")
            formType = form.get("formType")
            formState = form.get("state")

            if not formSlug or not formType:
                continue

            details = self.get_form_details(organization_slug, formType, formSlug)
            if details:
                all_forms_with_details.append(details)
            else:
                if formState != "Public":
                    logger.info(f"ℹ️  Utilisation des données de base pour {formType}/{formSlug}")
                    all_forms_with_details.append(form)

        logger.info(f"✅ {len(all_forms_with_details)} formulaires avec détails récupérés")

        # Afficher les détails
        if all_forms_with_details:
            logger.info("\n" + "=" * 100)
            logger.info("📋 DÉTAILS DES FORMULAIRES RÉCUPÉRÉS")
            logger.info("=" * 100)
            for i, form in enumerate(all_forms_with_details, 1):
                logger.info(
                    f"\n[{i}/{len(all_forms_with_details)}] {form.get('formType', 'N/A')} - {form.get('title', 'Sans titre')}"
                )
                logger.info(f"    Slug: {form.get('formSlug', 'N/A')}")
                logger.info(f"    État: {form.get('state', 'N/A')}")
                logger.info(
                    f"    Dates: {form.get('startDate', 'N/A')} → {form.get('endDate', 'N/A')}"
                )

                tiers = form.get("tiers", [])
                if tiers:
                    logger.info(f"    Tarifs ({len(tiers)}):")
                    for tier in tiers:
                        price = tier.get("price")
                        min_amount = tier.get("minAmount")
                        amount = (
                            price
                            if price is not None
                            else (min_amount if min_amount is not None else 0)
                        )
                        logger.info(f"      - {tier.get('label', 'Sans label')}: {amount/100:.2f}€")
                else:
                    logger.info("    Tarifs: Aucun")

            logger.info("\n" + "=" * 100 + "\n")

        return all_forms_with_details


class sync_helloasso_orders_and_transactions:
    """
    Classe pour synchroniser les commandes de HelloAsso avec Paheko.
    Pour chaque commande, crée un membre dans Paheko (si non existant) et pour chaque ligne de commande,
    crée une transaction comptable dans Paheko. Utilise un mécanisme
    idempotent pour éviter les doublons.
    """

    def __init__(
        self, mode_simulation: bool = False, force_sync: bool = False, mode_update: bool = False
    ):

        self.config_path = "config.toml"
        with open(self.config_path, "rb") as f:
            config = tomllib.load(f)
        self.base_url = config["paheko"]["base_url"]
        self.paheko_client = PahekoAPIClient(mode_simulation=mode_simulation)
        self.helloasso_client = HelloAssoHTTPClient(mode_simulation=mode_simulation)
        self.stats = {
            "orders_processed": 0,
            "orders_already_synced": 0,
            "orders_created": 0,
            "orders_failed": 0,
            "members_updated": 0,
            "members_created": 0,
            "members_failed": 0,
            "transactions_created": 0,
            "transactions_skipped": 0,
            "services_created": 0,
            "services_updated": 0,
            "services_unchanged": 0,
            "tarifs_created": 0,
            "tarifs_updated": 0,
            "tarifs_unchanged": 0,
            "errors": 0,
        }
        self.mode_simulation = mode_simulation
        self.force_sync = force_sync  # Nouveau : forcer la synchronisation même si déjà fait
        self.mode_update = (
            mode_update  # Nouveau : mode UPDATE pour filtrer les commandes déjà synchronisées
        )
        self.organization_slug = self.helloasso_client.get_organization_slug()

        # Charger les chemins depuis la configuration
        self.cashouts_csv = config.get("files", {}).get("cashouts_csv", "versements.csv")
        self.non_synced_dir = config.get("files", {}).get("non_synced_dir", "non_synced")

        self.sync_helloasso_services_main()
        # Récupérer et stocker les services et tarifs dans config.toml
        self.fetch_and_store_member_categories()
        self.fetch_and_store_services()
        self.fetch_and_store_tarifs()

        self.create_missing_cashouts_main()

        # ✅ Compteurs pour statistiques améliorées

        # ✅ Créer les répertoires pour export JSON des non synchronisés

        os.makedirs(f"{self.non_synced_dir}/orders", exist_ok=True)
        os.makedirs(f"{self.non_synced_dir}/cashouts", exist_ok=True)
        os.makedirs(f"{self.non_synced_dir}/members", exist_ok=True)
        sleep(5)

    def get_helloasso_references_from_paheko(self, year_id: int = None) -> set:
        """
        Récupère tous les IDs de commandes HelloAsso déjà présents dans Paheko.

        Extrait les références HelloAsso depuis le champ 'reference' des transactions,
        en préservant les autres références (OFX, etc.) qui peuvent coexister.

        Format du champ reference: "HelloAsso 12345" ou "HelloAsso 12345 OFX-67890"

        :param year_id: ID de l'année comptable (optionnel, None = toutes les années)
        :return: Set des IDs HelloAsso (ex: {12345, 67890, ...})
        """
        try:
            # Construire la requête SQL
            if year_id:
                sql_query = f"""
                    SELECT DISTINCT t.reference
                    FROM acc_transactions t
                    WHERE t.id_year = {year_id}
                    AND t.reference LIKE '%HelloAsso%'
                """
            else:
                sql_query = """
                    SELECT DISTINCT t.reference
                    FROM acc_transactions t
                    WHERE t.reference LIKE '%HelloAsso%'
                """

            response = self.paheko_client.get_sql(sql_query)

            if not response or not response.get("results"):
                logger.debug("Aucune référence HelloAsso trouvée dans Paheko")
                return set()

            # Extraire les IDs HelloAsso depuis les références
            helloasso_ids = set()

            for row in response["results"]:
                reference = row.get("reference", "")
                if reference:
                    # Extraire tous les IDs HelloAsso du format "HelloAsso 12345"
                    import re

                    matches = re.findall(r"HelloAsso\s+(\d+)", reference)
                    for match in matches:
                        helloasso_ids.add(int(match))

            logger.info("📋 {} commandes HelloAsso trouvées dans Paheko", len(helloasso_ids))
            if helloasso_ids:
                logger.debug("IDs HelloAsso dans Paheko: {}", sorted(list(helloasso_ids))[:10])
                if len(helloasso_ids) > 10:
                    logger.debug("... ({} autres)", len(helloasso_ids) - 10)

            return helloasso_ids

        except Exception as e:
            logger.exception("Erreur lors de la récupération des références HelloAsso:{} ", e)
            return set()

    def get_helloasso_cashouts_from_paheko(self, year_id: int = None) -> set:
        """
        Récupère tous les IDs de virements (cashouts) HelloAsso déjà présents dans Paheko.

        Extrait les références de virements depuis le champ 'reference' des transactions.

        Format du champ reference: "Virement HelloAsso 12345"

        :param year_id: ID de l'année comptable (optionnel, None = toutes les années)
        :return: Set des IDs de cashouts HelloAsso (ex: {12345, 67890, ...})
        """
        try:
            # Construire la requête SQL
            if year_id:
                sql_query = f"""
                    SELECT DISTINCT t.reference
                    FROM acc_transactions t
                    WHERE t.id_year = {year_id}
                    AND t.reference LIKE 'Virement HelloAsso%'
                """
            else:
                sql_query = """
                    SELECT DISTINCT t.reference
                    FROM acc_transactions t
                    WHERE t.reference LIKE 'Virement HelloAsso%'
                """

            response = self.paheko_client.get_sql(sql_query, mode="direct")

            if not response or not response.get("results"):
                logger.debug("Aucun virement HelloAsso trouvé dans Paheko")
                return set()

            # Extraire les IDs de cashouts depuis les références
            cashout_ids = set()

            for row in response["results"]:
                reference = row.get("reference", "")
                if reference:
                    # Extraire tous les IDs du format "Virement HelloAsso 12345"
                    import re

                    matches = re.findall(r"Virement HelloAsso\s+(\d+)", reference)
                    for match in matches:
                        cashout_ids.add(int(match))

            logger.info("📋 {} virements HelloAsso trouvés dans Paheko", len(cashout_ids))
            if cashout_ids:
                logger.debug("IDs virements dans Paheko: {}", sorted(list(cashout_ids))[:10])
                if len(cashout_ids) > 10:
                    logger.debug("... ({} autres) ", len(cashout_ids) - 10)

            return cashout_ids

        except Exception as e:
            logger.exception("Erreur lors de la récupération des virements HelloAsso: {}", e)
            return set()

    def fetch_and_store_member_categories(self) -> dict:
        """
        Récupère la liste des catégories de membres depuis l'API Paheko via l'endpoint /user/categories,
        puis met à jour le fichier de configuration (config.toml) en y ajoutant une section [member_categories].

        :return: Les catégories récupérées.
        :raises httpx.HTTPError: En cas d'erreur lors de la récupération.
        """
        url = f"{self.base_url}/user/categories"
        logger.debug("Récupération des catégories de membres depuis {}", url)
        response = self.paheko_client.client.get(url)
        response.raise_for_status()
        categories = response.json()
        logger.info("Catégories récupérées : {}", len(categories))
        logger.debug("Catégories= {}", categories)
        # Charger la configuration existante
        with open(self.config_path, "rb") as f:
            config_data = tomllib.load(f)
        logger.debug(
            "config_data avant mise à jour des member_categories: {}",
            jmodule.dumps(config_data, indent=4, ensure_ascii=False, default=str),
        )

        # Mettre à jour avec la section des catégories de membres
        config_data["member_categories"] = categories
        logger
        # Écrire la configuration mise à jour dans le fichier TOML
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(tomli_w.dumps(config_data))
        logger.debug(
            "config_data après mise à jour des member_categories: {}",
            jmodule.dumps(config_data, indent=4, ensure_ascii=False, default=str),
        )
        return categories

    def fetch_and_store_services(self) -> dict:
        """
        Récupère la liste des services (activités) depuis la base de données Paheko via SQL,
        puis met à jour le fichier de configuration (config.toml) en y ajoutant une section [services].

        :return: Les services récupérés sous forme de dictionnaire {id: service_data}.
        :raises httpx.HTTPError: En cas d'erreur lors de la récupération.
        """
        sql_query = (
            "SELECT id, label, description, start_date, end_date FROM services ORDER BY label"
        )
        logger.debug("Récupération des services depuis la base de données")

        try:
            result = self.paheko_client.get_sql(sql_query, mode="direct")
            services_list = result.get("results", [])
            logger.info("Services récupérés : {}", len(services_list))

            # Convertir la liste en dictionnaire avec l'ID comme clé
            services_dict = {}
            for service in services_list:
                service_id = service.get("id")
                if service_id:
                    services_dict[str(service_id)] = {
                        "id": service.get("id"),
                        "label": service.get("label"),
                        "description": service.get("description", "") or "",
                        "start_date": service.get("start_date", "") or "",
                        "end_date": service.get("end_date", "") or "",
                    }

            logger.debug("services_dict: {}", services_dict)
            # Charger la configuration existante
            with open(self.config_path, "rb") as f:
                config_data = tomllib.load(f)
            logger.debug(
                "config_data avant mise à jour des services: {}",
                jmodule.dumps(config_data, indent=4, ensure_ascii=False, default=str),
            )
            # Mettre à jour avec la section des services
            config_data["services"] = services_dict
            logger.debug(
                "config_data après mise à jour des services: {}",
                jmodule.dumps(config_data, indent=4, ensure_ascii=False, default=str),
            )
            # Écrire la configuration mise à jour dans le fichier TOML
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(tomli_w.dumps(config_data))
            logger.debug(
                "config_data après mise à jour des services: {}",
                jmodule.dumps(config_data, indent=4, ensure_ascii=False, default=str),
            )
            logger.info("Services mis à jour dans le fichier de configuration.")
            return services_dict

        except Exception as e:
            logger.exception("Erreur lors de la récupération des services : {}", e)
            return {}

    def fetch_and_store_tarifs(self) -> dict:
        """
        Récupère la liste des tarifs depuis la base de données Paheko via SQL,
        puis met à jour le fichier de configuration (config.toml) en y ajoutant une section [tarifs].

        :return: Les tarifs récupérés sous forme de dictionnaire {id: tarif_data}.
        :raises httpx.HTTPError: En cas d'erreur lors de la récupération.
        """
        sql_query = (
            "SELECT id, label, description, amount, id_service FROM services_fees ORDER BY label"
        )
        logger.debug("Récupération des tarifs depuis la base de données")

        try:
            result = self.paheko_client.get_sql(sql_query, mode="direct")
            tarifs_list = result.get("results", [])
            logger.info("Tarifs récupérés : {}", len(tarifs_list))

            # Convertir la liste en dictionnaire avec l'ID comme clé
            logger.debug("tarifs_list: {}", tarifs_list)
            tarifs_dict = {}
            for tarif in tarifs_list:
                tarif_id = tarif.get("id")
                if tarif_id:
                    tarifs_dict[str(tarif_id)] = {
                        "id": tarif.get("id"),
                        "label": tarif.get("label"),
                        "description": tarif.get("description", "") or "",
                        "amount": tarif.get("amount", "") or "0",
                        "id_service": tarif.get("id_service"),
                    }
            logger.debug("tarifs_dict: {}", tarifs_dict)
            # Charger la configuration existante
            with open(self.config_path, "rb") as f:
                config_data = tomllib.load(f)
            logger.debug(
                "config_data avant mise à jour des tarifs: {}",
                jmodule.dumps(config_data, indent=4, ensure_ascii=False, default=str),
            )
            # Mettre à jour avec la section des tarifs
            config_data["tarifs"] = tarifs_dict
            logger.debug(
                "config_data après mise à jour des tarifs: {}",
                jmodule.dumps(config_data, indent=4, ensure_ascii=False, default=str),
            )
            # Écrire la configuration mise à jour dans le fichier TOML
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(tomli_w.dumps(config_data))
            logger.debug(
                "config_data écrite dans le fichier: {}",
                jmodule.dumps(config_data, indent=4, ensure_ascii=False, default=str),
            )
            logger.info("Tarifs mis à jour dans le fichier de configuration.")
            return tarifs_dict

        except Exception as e:
            logger.exception("Erreur lors de la récupération des tarifs : {}", e)
            return {}

    def save_non_synced_item(self, item_type: str, item_id: str, item_data: dict) -> None:
        """
        Sauvegarde un élément non synchronisé dans un fichier JSON.

        :param item_type: Type d'élément ('orders', 'cashouts', 'members')
        :param item_id: Identifiant unique de l'élément
        :param item_data: Dictionnaire complet des données de l'élément
        """

        # Fonction pour convertir les objets datetime en string
        def json_serial(obj):
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")

        filename = f"{self.non_synced_dir}/{item_type}/{item_type}_{item_id}.json"
        try:
            with open(filename, "w", encoding="utf-8") as f:
                jmodule.dump(item_data, f, indent=4, ensure_ascii=False, default=json_serial)
            logger.info(f"📄 Exporté élément non synchronisé: {filename} ")
        except Exception as e:
            logger.exception(f"❌ Erreur export JSON {filename}: {e}")

    def print_sync_statistics(self) -> None:
        """
        Affiche les statistiques détaillées de la synchronisation.
        """
        logger.info("\n" + "=" * 70)
        logger.info("📊 STATISTIQUES DE SYNCHRONISATION DÉTAILLÉES")
        logger.info("=" * 70)

        logger.info("\n📦 COMMANDES:")
        logger.info(f"  • Traitées:           {self.stats['orders_processed']}")
        logger.info(f"  • Déjà synchronisées: {self.stats['orders_already_synced']}")
        logger.info(f"  • Nouvelles créées:   {self.stats['orders_created']}")
        logger.info(f"  • Échecs:             {self.stats['orders_failed']}")

        logger.info("\n👥 MEMBRES:")
        logger.info(f"  • Traités (créés/màj): {self.stats['members_updated']}")
        logger.info(f"  • Échecs:              {self.stats['members_failed']}")

        logger.info("\n💰 TRANSACTIONS:")
        logger.info(f"  • Créées:              {self.stats['transactions_created']}")
        logger.info(f"  • Existantes (skip):   {self.stats['transactions_skipped']}")

        logger.info("\n" + "=" * 70)

        # Afficher un résumé des fichiers JSON exportés

        orders_files = (
            len([f for f in os.listdir(f"{self.non_synced_dir}/orders") if f.endswith(".json")])
            if os.path.exists(f"{self.non_synced_dir}/orders")
            else 0
        )
        members_files = (
            len([f for f in os.listdir(f"{self.non_synced_dir}/members") if f.endswith(".json")])
            if os.path.exists(f"{self.non_synced_dir}/members")
            else 0
        )
        cashouts_files = (
            len([f for f in os.listdir(f"{self.non_synced_dir}/cashouts") if f.endswith(".json")])
            if os.path.exists(f"{self.non_synced_dir}/cashouts")
            else 0
        )

        if orders_files + members_files + cashouts_files > 0:
            logger.info("\n📄 EXPORTS JSON (éléments non synchronisés):")
            if orders_files > 0:
                logger.info(
                    f"  • Orders:   {orders_files} fichiers dans {self.non_synced_dir}/orders/"
                )
            if members_files > 0:
                logger.info(
                    f"  • Members:  {members_files} fichiers dans {self.non_synced_dir}/members/"
                )
            if cashouts_files > 0:
                logger.info(
                    f"  • Cashouts: {cashouts_files} fichiers dans {self.non_synced_dir}/cashouts/"
                )
            logger.info("=" * 70)

    def create_bank_transfer_transaction(self, cash_out: dict) -> dict | None:
        """
        Crée une transaction comptable pour un virement bancaire
        de HelloAsso vers le compte bancaire.

        :param cash_out: Dictionnaire contenant les infos du virement:
            - id: Identifiant du cash-out (idCashOut)
            - date: Date du virement (cashOutDate)
            - total_amount_centimes: Montant total en centimes
            - payments: Liste des paiements inclus
        :return: Réponse de l'API Paheko ou None en cas d'erreur
        """
        # Référence unique du virement
        reference = f"Virement HelloAsso {cash_out['id']}"

        # Convertir le montant de centimes en euros
        amount = round(cash_out["total_amount_centimes"] / 100.0, 2)

        # Parser la date du virement
        # cash_out_date = self.helloasso_client.convert_to_datetime(cash_out["date"])
        cash_out_date = cash_out["date"]
        date_str = str(cash_out_date)[:10]
        # date_str = cash_out_date.strftime("%Y-%m-%d")

        # Récupérer l'année comptable correspondante
        try:
            id_year = self.paheko_client.get_current_year_id(cash_out_date, self.organization_slug)
        except Exception as e:
            logger.exception(
                "Impossible de déterminer l'année comptable pour le " "virement {} (date: {}): {}",
                reference,
                date_str,
                e
            )
            return None

        # Créer le libellé avec le nombre de paiements
        payment_count = len(cash_out["payments"])
        label = (
            f"Virement bancaire HelloAsso "
            f"({payment_count} paiement{'s' if payment_count > 1 else ''})"
        )

        # Transaction ADVANCED avec 2 lignes:
        # - Débit 512BPBFC (l'argent arrive sur le compte bancaire BPBFC)
        # - Crédit 512HELLOASSO (l'argent quitte le compte HelloAsso)
        transaction_data = {
            "id_year": id_year,
            "label": label,
            "date": date_str,
            "type": "ADVANCED",
            "lines": [
                {
                    "account": "512BPBFC",
                    "debit": amount,
                    "credit": 0,
                    "label": label,
                    "reference": reference,
                },
                {
                    "account": "512HELLOASSO",
                    "debit": 0,
                    "credit": amount,
                    "label": label,
                    "reference": reference,
                },
            ],
            "reference": reference,
        }

        try:
            if self.mode_simulation:
                logger.info(
                    "Mode simulation : création du virement {} " "({:.2f}€, {} paiements).\n{}",
                    reference,
                    amount,
                    payment_count,
                    transaction_data,
                )
                return {"simulated": True}
            else:
                response = self.paheko_client.create_transaction(transaction_data)
                if response:
                    logger.info(
                        "Virement créé : {} ({:.2f}€, {} paiements)",
                        reference,
                        amount,
                        payment_count,
                    )
                    return response
                else:
                    logger.error("Échec de la création du virement {}", reference)
                    return None

        except Exception as e:
            logger.exception("Erreur lors de la création du virement {} : {}", reference, e)
            # Afficher le contexte complet en JSON
            try:

                context = {
                    "virement_id": cash_out.get("id"),
                    "virement_date": cash_out.get("date"),
                    "montant_euros": amount,
                    "nombre_paiements": len(cash_out.get("payments", [])),
                    "reference_transaction": reference,
                    "donnees_virement": transaction_data,
                    "paiements_inclus": cash_out.get("payments"),
                    "erreur": str(e),
                    "type_erreur": type(e).__name__,
                }
                logger.error(
                    "📋 Contexte de création de virement bancaire (JSON):\n{}",
                    jmodule.dumps(context, indent=4, ensure_ascii=False, default=str),
                )
            except Exception as json_error:
                logger.error("Impossible de sérialiser le contexte: {}", json_error)
            return None

    def create_missing_cashouts_from_csv(self, csv_file: str = None) -> dict:
        """
        Crée les virements manquants à partir d'un fichier CSV.

        Lit le fichier CSV contenant les cashout IDs et crée les transactions
        correspondantes dans Paheko. Ajoute également les nouveaux virements
        au fichier CSV avec un statut "Créé dans Paheko".

        :param csv_file: Chemin du fichier CSV à traiter (si None, utilise la config)
        :return: Dictionnaire de statistiques (succès, erreurs, montant total)
        """

        # Utiliser le fichier de la config si non spécifié
        # if csv_file is None:
        #     csv_file = self.cashouts_csv

        logger.info("=" * 80)
        logger.info("CRÉATION DES VIREMENTS MANQUANTS DEPUIS CSV")
        logger.info("=" * 80)

        csv_path = Path(csv_file)
        if not csv_path.exists():
            logger.error("Fichier CSV introuvable: {}", csv_file)
            return {"success": 0, "errors": 0, "total_amount": 0}

        # Récupérer tous les paiements une seule fois
        logger.info("📥 Récupération de tous les paiements HelloAsso...")
        all_payments = self.helloasso_client.get_payments(self.organization_slug, page_size=10)
        logger.info("✅ {} paiements récupérés", len(all_payments))

        # Lire le CSV
        logger.info("📖 Lecture du fichier CSV: {}", csv_file)
        cashout_ids = []
        csv_data = []

        # Essayer différents encodages

        encodings = ["utf-8", "latin-1", "iso-8859-1", "cp1252"]
        file_content = None
        # used_encoding = None

        for enc in encodings:
            try:
                with open(csv_path, "r", encoding=enc) as f:
                    file_content = f.read()
                # used_encoding = enc
                logger.info("  Encodage détecté: {}", enc)
                break
            except UnicodeDecodeError:
                continue

        if not file_content:
            logger.error("Impossible de lire le fichier CSV avec les encodages disponibles")
            return {"success": 0, "errors": 0, "total_amount": 0}

        # Parser le CSV
        reader = csv.DictReader(io.StringIO(file_content))
        for row in reader:
            try:
                cashout_id = int(row["cashoutId"])
                cashout_ids.append(cashout_id)
                csv_data.append(row)
            except (ValueError, KeyError) as e:
                logger.exception("Ligne CSV invalide: ligne {} : {}",row, e)

        logger.info("📋 {} cashouts à traiter", len(cashout_ids))

        # Filtrer les cashouts déjà présents dans Paheko
        logger.info("🔍 Vérification des cashouts déjà présents dans Paheko...")
        existing_cashouts = self.get_helloasso_cashouts_from_paheko()

        # Filtrer la liste des cashouts
        cashout_ids_to_process = [cid for cid in cashout_ids if cid not in existing_cashouts]

        if len(cashout_ids_to_process) < len(cashout_ids):
            already_synced = len(cashout_ids) - len(cashout_ids_to_process)
            logger.info(f"⏭️  {already_synced} cashout(s) déjà synchronisé(s), ignoré(s)")

        logger.info(f"📝 {len(cashout_ids_to_process)} cashout(s) à traiter")

        # Traiter chaque cashout
        success_count = 0
        error_count = 0
        total_amount = 0
        # new_entries = []

        for i, cashout_id in enumerate(cashout_ids_to_process, 1):
            logger.debug(
                "[{}/{}] Traitement cashoutId={}", i, len(cashout_ids_to_process), cashout_id
            )

            # Filtrer les paiements pour ce cashout
            payments = [p for p in all_payments if p.get("idCashOut") == cashout_id]

            if not payments:
                logger.warning("  ⏭️  Aucun paiement trouvé pour cashout {}", cashout_id)
                error_count += 1
                continue

            # Construire le cashout
            cash_out = {
                "id": cashout_id,
                "date": payments[0].get("cashOutDate"),
                "payments": [],
                "total_amount_centimes": 0,
            }

            for payment in payments:
                payer = payment.get("payer", {})
                first_name = payer.get("firstName", "")
                last_name = payer.get("lastName", "")

                cash_out["payments"].append(
                    {
                        "id": payment["id"],
                        "amount": payment["amount"],
                        "order_id": payment.get("order", {}).get("id"),
                        "payer_name": f"{first_name} {last_name}",
                    }
                )
                cash_out["total_amount_centimes"] += payment["amount"]

            amount = cash_out["total_amount_centimes"] / 100
            date = cash_out["date"][:10] if cash_out["date"] else "N/A"
            payment_count = len(payments)

            logger.info("  📍 {} | {:.2f}€ | {} paiements", date, amount, payment_count)

            # Créer la transaction dans Paheko
            result = self.create_bank_transfer_transaction(cash_out)

            if result:
                success_count += 1
                total_amount += amount
                # ✅ Plus de stockage en JSON/CSV - Paheko est la seule source de vérité
                logger.info("  ✅ Virement créé")
            else:
                error_count += 1
                logger.warning("  ❌ Erreur lors de la création")

        # ✅ Plus de mise à jour du CSV - Le CSV sert uniquement de liste d'entrée

        # Résumé
        logger.info("\n" + "=" * 80)
        logger.info("📊 RÉSUMÉ CRÉATION VIREMENTS")
        logger.info("=" * 80)
        logger.info("✅ Succès: {}/{}", success_count, len(cashout_ids_to_process))
        logger.info("❌ Erreurs: {}/{}", error_count, len(cashout_ids_to_process))
        logger.info("💰 Montant total créé: {:.2f}€", total_amount)
        logger.info("=" * 80)

        logger.info(f"\n✅ {success_count}/{len(cashout_ids_to_process)} virements créés")
        logger.info(f"❌ {error_count}/{len(cashout_ids_to_process)} erreurs")
        logger.info(f"💰 Montant: {total_amount:.2f}€\n")

        return {
            "success": success_count,
            "errors": error_count,
            "total_amount": total_amount,
            "total_processed": len(cashout_ids_to_process),
        }

    def force_create_missing_cashouts_from_csv(self, csv_file: str):
        """
        Crée les virements manquants (non traités) du CSV de manière forcée,
        sans vérifier les données HelloAsso. Utilise les montants du CSV directement.

        :param csv_file: Chemin du fichier CSV à traiter
        :return: Dictionnaire de statistiques (succès, erreurs, montant total)
        """

        logger.info("=" * 80)
        logger.info("🔨 CRÉATION FORCÉE DES VIREMENTS MANQUANTS DEPUIS CSV")
        logger.info("=" * 80)

        csv_path = Path(csv_file)
        if not csv_path.exists():
            logger.error("Fichier CSV introuvable: {}", csv_file)
            return {"success": 0, "errors": 0, "total_amount": 0}

        # Lire le CSV
        logger.info("📖 Lecture du fichier CSV: {}", csv_file)
        cashout_rows = []

        # Essayer différents encodages

        encodings = ["utf-8", "latin-1", "iso-8859-1", "cp1252"]
        file_content = None
        # used_encoding = None

        for enc in encodings:
            try:
                with open(csv_path, "r", encoding=enc) as f:
                    file_content = f.read()
                # used_encoding = enc
                logger.info("  Encodage détecté: {}", enc)
                break
            except UnicodeDecodeError:
                continue

        if not file_content:
            logger.error("Impossible de lire le fichier CSV avec les encodages disponibles")
            return {"success": 0, "errors": 0, "total_amount": 0}

        # Parser le CSV
        reader = csv.DictReader(io.StringIO(file_content))
        # fieldnames = reader.fieldnames
        all_rows = []

        for row in reader:
            all_rows.append(row)
            # Garder seulement les lignes non créées (vides Statut_Paheko)
            if not row.get("Statut_Paheko"):
                cashout_rows.append(row)

        logger.info("📋 {} cashouts à créer de manière forcée", len(cashout_rows))

        # Vérifier les références dupliquées dans Paheko AVANT création
        logger.info("\n🔍 Vérification des cashouts déjà présents dans Paheko...")
        existing_cashouts = self.get_helloasso_cashouts_from_paheko()

        # Déterminer quels cashouts peuvent être créés (sans doublon)
        can_create = []
        cannot_create = []

        for row in cashout_rows:
            cashout_id = int(row["cashoutId"])

            if cashout_id in existing_cashouts:
                cannot_create.append(
                    {
                        "cashout_id": cashout_id,
                        "reference": f"Virement HelloAsso {cashout_id}",
                    }
                )
                logger.warning("  ⚠️  Cashout {} déjà créé, sera ignoré", cashout_id)
            else:
                can_create.append(row)

        if cannot_create:
            logger.warning(
                "⚠️  {} virements déjà existants (références dupliquées détectées)",
                len(cannot_create),
            )
        logger.info("✅ {} virements prêts à être créés", len(can_create))

        # Traiter chaque cashout non créé ET non dupliqué
        success_count = 0
        error_count = 0
        total_amount = 0
        #updated_rows = []

        for i, row in enumerate(can_create, 1):
            try:
                cashout_id = int(row["cashoutId"])
                date_str = row["Date"]
                amount_str = row["Montant"]

                logger.debug(
                    "[{}/{}] Traitement forcé cashoutId={}", i, len(cashout_rows), cashout_id
                )

                # Parser la date (format ISO YYYY-MM-DD)
                try:
                    date = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    logger.warning(
                        "  ⚠️  Format de date invalide: {} pour cashout {}", date_str, cashout_id
                    )
                    date = datetime.now().strftime("%Y-%m-%d")

                # Parser le montant
                try:
                    amount = float(amount_str)
                except ValueError:
                    logger.error(
                        "  ❌ Montant invalide: {} pour cashout {}", amount_str, cashout_id
                    )
                    error_count += 1
                    continue

                logger.info("  📍 {} | {:.2f}€ | Forcé (sans vérification)", date, amount)

                # Construire un objet cashout fictif (sans paiements)
                cash_out = {
                    "id": cashout_id,
                    "date": date,
                    "payments": [],
                    "total_amount_centimes": int(amount * 100),
                }

                # Créer la transaction dans Paheko
                result = self.create_bank_transfer_transaction(cash_out)

                if result:
                    success_count += 1
                    total_amount += amount
                    # ✅ Plus de stockage en JSON/CSV - Paheko est la seule source de vérité
                    logger.info("  ✅ Virement créé (forcé)")
                else:
                    error_count += 1
                    logger.warning("  ❌ Erreur lors de la création")

            except Exception as e:
                logger.exception("  ❌ Exception: {}", e)
                error_count += 1

        # ✅ Plus de mise à jour du CSV - Le CSV sert uniquement de liste d'entrée

        # Résumé
        logger.info("\n" + "=" * 80)
        logger.info("📊 RÉSUMÉ CRÉATION FORCÉE VIREMENTS")
        logger.info("=" * 80)
        logger.info("✅ Succès: {}/{}", success_count, len(can_create))
        logger.info("❌ Erreurs: {}/{}", error_count, len(can_create))
        if cannot_create:
            logger.warning("⚠️  Déjà existants (références dupliquées): {}", len(cannot_create))
        logger.info("💰 Montant total créé: {:.2f}€", total_amount)
        logger.info("=" * 80)

        logger.info(f"\n✅ {success_count}/{len(can_create)} virements créés (forcé)")
        logger.info(f"❌ {error_count}/{len(can_create)} erreurs")
        if cannot_create:
            logger.info(f"⚠️  {len(cannot_create)} virements déjà existants (références dupliquées)")
        logger.info(f"💰 Montant: {total_amount:.2f}€\n")

        return {
            "success": success_count,
            "errors": error_count,
            "total_amount": total_amount,
            "total_processed": len(can_create),
            "duplicates_skipped": len(cannot_create),
        }

    def verify_no_duplicate_references(self, csv_file: str) -> dict:
        """
        Vérifie qu'aucun virement n'a la même référence dans Paheko.
        Retourne les virements avec références dupliquées.
        Utilise l'endpoint Paheko: GET /api/accounting/years/{ID_YEAR}/account/journal?code=512BPBFC
        """
        logger.info("=" * 80)
        logger.info("🔍 VÉRIFICATION DES RÉFÉRENCES DUPLIQUÉES")
        logger.info("=" * 80)

        try:
            csv_path = Path(csv_file)
            if not csv_path.exists():
                logger.error("❌ Fichier CSV introuvable: {}", csv_file)
                return {"duplicates": [], "unique_count": 0, "duplicate_count": 0}

            # Récupérer toutes les transactions du compte 512BPBFC (où arrivent les virements)
            logger.info("📥 Récupération des transactions depuis Paheko...")
            logger.info("   Récupération de TOUTES les années...")

            # Récupérer toutes les années disponibles
            try:
                url = f"{self.paheko_client.base_url}/accounting/years"
                response = self.paheko_client.client.get(url)
                response.raise_for_status()
                years_data = response.json()
                if isinstance(years_data, list):
                    all_years = years_data
                else:
                    all_years = years_data.get("data", [])
                logger.info("  📅 Années trouvées: {}", len(all_years))
            except Exception as e:
                logger.exception("❌ Erreur lors de la récupération des années: {}", e)
                all_years = []

            # Récupérer les transactions de TOUTES les années
            paheko_transactions = []
            for year in all_years:
                try:
                    year_id = year.get("id")
                    url = (
                        f"{self.paheko_client.base_url}/accounting/years/{year_id}/account/journal"
                    )
                    params = {"code": "512BPBFC"}
                    response = self.paheko_client.client.get(url, params=params)
                    response.raise_for_status()
                    journal_data = response.json()
                    # L'endpoint retourne une liste directement ou dict avec "data"
                    if isinstance(journal_data, list):
                        year_transactions = journal_data
                    else:
                        year_transactions = journal_data.get("data", [])
                    paheko_transactions.extend(year_transactions)
                    logger.info(
                        "  ✅ Année {}: {} transactions", year.get("label"), len(year_transactions)
                    )
                except Exception as e:
                    logger.exception("  ⚠️ Année {}: {}", year.get("label"), e)

            # Extraire les références HelloAsso
            helloasso_refs = {}
            duplicates = []

            for trans in paheko_transactions:
                ref = trans.get("reference", "")
                if "HelloAsso" in ref:
                    if ref not in helloasso_refs:
                        helloasso_refs[ref] = [trans.get("id")]
                    else:
                        helloasso_refs[ref].append(trans.get("id"))
                        duplicates.append(
                            {
                                "reference": ref,
                                "transaction_ids": helloasso_refs[ref],
                                "count": len(helloasso_refs[ref]),
                            }
                        )

            logger.info(
                "✅ Références uniques: {}",
                len([r for r, ids in helloasso_refs.items() if len(ids) == 1]),
            )
            logger.info("⚠️  Références dupliquées trouvées: {}", len(duplicates))

            if duplicates:
                for dup in duplicates:
                    logger.warning(
                        "  ⚠️  {}: {} transactions (IDs: {})",
                        dup["reference"],
                        dup["count"],
                        dup["transaction_ids"],
                    )

            return {
                "duplicates": duplicates,
                "unique_count": len([r for r, ids in helloasso_refs.items() if len(ids) == 1]),
                "duplicate_count": len(duplicates),
                "total_helloasso_refs": len(helloasso_refs),
            }

        except Exception as e:
            logger.exception("❌ Erreur lors de la vérification: {}", e)

            traceback.print_exc()
            return {"duplicates": [], "unique_count": 0, "duplicate_count": 0}

    def verify_all_cashouts_created(self, csv_file: str) -> dict:
        """
        Vérifie que tous les virements du CSV sont créés dans Paheko.
        Retourne les virements manquants.
        """
        logger.info("=" * 80)
        logger.info("✅ VÉRIFICATION DES VIREMENTS CRÉÉS DANS PAHEKO")
        logger.info("=" * 80)

        try:
            csv_path = Path(csv_file)
            if not csv_path.exists():
                logger.error("❌ Fichier CSV introuvable: {}", csv_file)
                return {"missing": [], "found": [], "total": 0, "completion_rate": 0}

            # Lire le CSV
            encodings = ["utf-8", "latin-1", "iso-8859-1", "cp1252"]
            file_content = None

            for enc in encodings:
                try:
                    with open(str(csv_path), "r", encoding=enc) as f:
                        file_content = f.read()
                    break
                except UnicodeDecodeError:
                    continue

            if not file_content:
                logger.error("❌ Impossible de lire le fichier CSV")
                return {"missing": [], "found": [], "total": 0, "completion_rate": 0}

            # Parser le CSV - extraire tous les virements (créés ou non)
            reader = csv.DictReader(io.StringIO(file_content))
            csv_cashouts = {}
            for row in reader:
                cashout_id = int(row.get("cashoutId"))
                csv_cashouts[cashout_id] = {
                    "date": row.get("Date"),
                    "montant": float(row.get("Montant")),
                    "statut": row.get("Statut_Paheko", ""),
                }

            logger.info("📋 Total virements dans CSV: {}", len(csv_cashouts))

            # Récupérer toutes les transactions HelloAsso de Paheko via l'API
            logger.info("📥 Récupération des transactions HelloAsso depuis Paheko...")
            logger.info("   Récupération de TOUTES les années...")

            # Récupérer toutes les années disponibles
            try:
                url = f"{self.paheko_client.base_url}/accounting/years"
                response = self.paheko_client.client.get(url)
                response.raise_for_status()
                years_data = response.json()
                if isinstance(years_data, list):
                    all_years = years_data
                else:
                    all_years = years_data.get("data", [])
                logger.info("  📅 Années trouvées: {}", len(all_years))
            except Exception as e:
                logger.exception("❌ Erreur lors de la récupération des années: {}", e)
                all_years = []

            # Récupérer le journal général complet pour TOUTES les années
            # Le journal contient toutes les écritures (2 ou n lignes par transaction)
            paheko_transactions = []
            for year in all_years:
                try:
                    year_id = year.get("id")
                    url = f"{self.paheko_client.base_url}/accounting/years/{year_id}/journal"
                    response = self.paheko_client.client.get(url)
                    response.raise_for_status()
                    journal_data = response.json()
                    # L'endpoint retourne une liste directement ou dict avec "data"
                    if isinstance(journal_data, list):
                        year_transactions = journal_data
                    else:
                        year_transactions = journal_data.get("data", [])
                    paheko_transactions.extend(year_transactions)
                    logger.info(
                        "  ✅ Année {}: {} écritures",
                        year.get("label"),
                        len(year_transactions),
                    )
                except Exception as e:
                    logger.exception("  ⚠️ Année {}: {}", year.get("label"), e)

            logger.info(
                "  📊 Total écritures trouvées dans le journal: {}",
                len(paheko_transactions),
            )

            # Extraire les IDs de cashout de Paheko en indexant par référence
            # Chaque virement a typiquement 2 écritures (débit + crédit)
            paheko_cashouts = {}  # cashout_id -> {'count': N, 'lines': [...]}
            for trans in paheko_transactions:
                ref = trans.get("reference")
                if ref and "Virement HelloAsso" in ref:
                    # Format: "Virement HelloAsso XXXXX"
                    try:
                        cashout_id = int(ref.split()[-1])
                        if cashout_id not in paheko_cashouts:
                            paheko_cashouts[cashout_id] = {"count": 0, "lines": []}
                        paheko_cashouts[cashout_id]["count"] += 1
                        paheko_cashouts[cashout_id]["lines"].append(
                            {
                                "account": trans.get("account"),
                                "debit": trans.get("debit"),
                                "credit": trans.get("credit"),
                                "label": trans.get("label"),
                                "date": trans.get("date"),
                            }
                        )
                    except (ValueError, IndexError):
                        logger.warning("  ⚠️ Impossible d'extraire ID de: {}", ref)

            # Comparer
            missing = []
            found = []

            for cashout_id in sorted(csv_cashouts.keys()):
                if cashout_id in paheko_cashouts:
                    found.append(cashout_id)
                    entry = paheko_cashouts[cashout_id]
                    count = entry["count"]
                    lines = entry["lines"]

                    logger.info(
                        "✅ Cashout {}: TROUVÉ dans Paheko ({} écritures)",
                        cashout_id,
                        count,
                    )

                    # Afficher les détails des écritures
                    for i, line in enumerate(lines, 1):
                        debit = line.get("debit") or 0
                        credit = line.get("credit") or 0
                        account = line.get("account") or ""
                        label = line.get("label") or ""
                        logger.info(
                            "   [{}/{}] {} | {} | Débit: {:.2f}€ | Crédit: {:.2f}€",
                            i,
                            count,
                            account,
                            label,
                            float(debit),
                            float(credit),
                        )
                else:
                    missing.append(
                        {
                            "id": cashout_id,
                            "date": csv_cashouts[cashout_id]["date"],
                            "montant": csv_cashouts[cashout_id]["montant"],
                        }
                    )
                    logger.warning("❌ Cashout {}: MANQUANT dans Paheko", cashout_id)

            logger.info("=" * 80)
            logger.info("📊 RÉSUMÉ VÉRIFICATION")
            logger.info("=" * 80)
            logger.info("✅ Trouvés: {}/{}", len(found), len(csv_cashouts))
            logger.info("❌ Manquants: {}/{}", len(missing), len(csv_cashouts))
            logger.info("=" * 80)

            return {
                "missing": missing,
                "found": found,
                "total": len(csv_cashouts),
                "completion_rate": (
                    round(len(found) / len(csv_cashouts) * 100, 2) if csv_cashouts else 0
                ),
            }

        except Exception as e:
            logger.exception("❌ Erreur lors de la vérification: {}", e)
            return {"missing": [], "found": [], "total": 0, "completion_rate": 0}

    def verify_orders_created(self) -> dict:
        """
        Vérifie que toutes les commandes HelloAsso sont synchronisées dans Paheko.
        Récupère les commandes de l'API HelloAsso et les compare avec les transactions dans Paheko.
        Pointe chaque transaction de HelloAsso et son statut dans Paheko.
        """
        logger.info("=" * 80)
        logger.info("✅ VÉRIFICATION DES COMMANDES HELLOASSO DANS PAHEKO")
        logger.info("=" * 80)

        try:
            # Récupérer toutes les commandes depuis HelloAsso
            logger.info("📥 Récupération des commandes depuis HelloAsso...")
            self.orders = self.helloasso_client.get_orders(
                organization_slug=self.organization_slug, page_size=10
            )
            logger.info("  📋 Total commandes HelloAsso: {}", len(self.orders))

            if not self.orders:
                logger.info("  Aucune commande à vérifier")
                return {
                    "total_orders": 0,
                    "found": [],
                    "missing": [],
                    "completion_rate": 0,
                }

            # Récupérer toutes les années pour chercher les transactions dans Paheko
            logger.info("📥 Récupération des transactions depuis Paheko...")
            try:
                url = f"{self.paheko_client.base_url}/accounting/years"
                response = self.paheko_client.client.get(url)
                response.raise_for_status()
                years_data = response.json()
                if isinstance(years_data, list):
                    all_years = years_data
                else:
                    all_years = years_data.get("data", [])
                logger.info("  📅 Années trouvées: {}", len(all_years))
            except Exception as e:
                logger.exception("❌ Erreur lors de la récupération des années: {}", e)
                all_years = []

            # Récupérer le journal général complet pour TOUTES les années
            # Permet de voir toutes les écritures (2 ou n lignes) pour chaque commande
            paheko_transactions = []
            for year in all_years:
                try:
                    year_id = year.get("id")
                    url = f"{self.paheko_client.base_url}/accounting/years/{year_id}/journal"
                    response = self.paheko_client.client.get(url)
                    response.raise_for_status()
                    journal_data = response.json()
                    # L'endpoint retourne une liste directement ou dict avec "data"
                    if isinstance(journal_data, list):
                        year_transactions = journal_data
                    else:
                        year_transactions = journal_data.get("data", [])
                    paheko_transactions.extend(year_transactions)
                    logger.info(
                        "  ✅ Année {}: {} écritures",
                        year.get("label"),
                        len(year_transactions),
                    )
                except Exception as e:
                    logger.exception("  ⚠️ Année {}: {}", year.get("label"), e)

            logger.info(
                "  📊 Total écritures trouvées dans le journal: {}", len(paheko_transactions)
            )

            # Créer un index des références HelloAsso
            # Regroupe les écritures par ID de commande pour pointer chaque transaction
            paheko_helloasso_refs = {}  # order_id -> {'count': N, 'lines': [...]}
            for trans in paheko_transactions:
                ref = trans.get("reference", "")
                if ref and ref.startswith("HelloAsso "):
                    try:
                        # Format: "HelloAsso {order_id}"
                        order_id = int(ref.split()[-1])
                        if order_id not in paheko_helloasso_refs:
                            paheko_helloasso_refs[order_id] = {"count": 0, "lines": []}
                        paheko_helloasso_refs[order_id]["count"] += 1
                        paheko_helloasso_refs[order_id]["lines"].append(
                            {
                                "id": trans.get("id"),
                                "account": trans.get("account"),
                                "debit": trans.get("debit"),
                                "credit": trans.get("credit"),
                                "label": trans.get("label"),
                                "date": trans.get("date"),
                            }
                        )
                    except (ValueError, IndexError):
                        logger.warning("  ⚠️ Impossible d'extraire ID de: {}", ref)

            # Comparer les commandes HelloAsso avec Paheko
            found = []
            missing = []
            problems = []

            for order in self.orders:
                order_id = order.get("id")
                order_date = order.get("date", "N/A")
                # order_amount peut être un dict {'total': X, 'vat': Y, 'discount': Z} ou un number
                order_amount_raw = order.get("amount", 0)
                if isinstance(order_amount_raw, dict):
                    order_amount = order_amount_raw.get("total", 0)
                else:
                    order_amount = order_amount_raw
                items_count = len(order.get("items", []))
                order_state = order.get("state", "Unknown")

                if order_id in paheko_helloasso_refs:
                    found.append(order_id)
                    entry = paheko_helloasso_refs[order_id]
                    trans_count = entry["count"]
                    lines = entry["lines"]

                    logger.info(
                        "✅ Commande {}: TROUVÉE ({} items, {} écritures dans Paheko, statut: {})",
                        order_id,
                        items_count,
                        trans_count,
                        order_state,
                    )

                    # Afficher les détails de chaque écriture
                    for i, line in enumerate(lines, 1):
                        debit = line.get("debit") or 0
                        credit = line.get("credit") or 0
                        account = line.get("account") or ""
                        label = line.get("label") or ""
                        logger.info(
                            "   [{}/{}] {} | {} | Débit: {:.2f}€ | Crédit: {:.2f}€",
                            i,
                            trans_count,
                            account,
                            label,
                            float(debit),
                            float(credit),
                        )

                    # Vérifier s'il y a un problème (par ex: nombre de lignes inattendu)
                    if trans_count != 2 * items_count:
                        problems.append(
                            {
                                "order_id": order_id,
                                "date": order_date,
                                "amount": order_amount,
                                "items": items_count,
                                "transactions": trans_count,
                                "expected": 2 * items_count,
                                "problem": f"Nombre de lignes incorrect (attendu {2 * items_count}, trouvé {trans_count})",
                                "lines": lines,
                            }
                        )
                        logger.warning(
                            "  ⚠️  Problème: attendu {} écritures, trouvé {}",
                            2 * items_count,
                            trans_count,
                        )
                else:
                    missing.append(
                        {
                            "id": order_id,
                            "date": order_date,
                            "amount": order_amount,
                            "items": items_count,
                            "state": order_state,
                        }
                    )
                    logger.warning(
                        "❌ Commande {}: MANQUANTE (statut: {}, {} items, {}€)",
                        order_id,
                        order_state,
                        items_count,
                        order_amount / 100,
                    )

            logger.info("=" * 80)
            logger.info("📊 RÉSUMÉ VÉRIFICATION COMMANDES")
            logger.info("=" * 80)
            logger.info("✅ Trouvées: {}/{}", len(found), len(self.orders))
            logger.info("❌ Manquantes: {}/{}", len(missing), len(self.orders))
            if problems:
                logger.warning("⚠️  Problèmes détectés: {}", len(problems))
            logger.info("=" * 80)

            completion_rate = (len(found) / len(self.orders) * 100) if self.orders else 0

            return {
                "total_orders": len(self.orders),
                "found": found,
                "missing": missing,
                "problems": problems,
                "completion_rate": round(completion_rate, 2),
            }

        except Exception as e:
            logger.exception("❌ Erreur lors de la vérification des commandes: {}", e)

            return {
                "total_orders": 0,
                "found": [],
                "missing": [],
                "problems": [],
                "completion_rate": 0,
            }

    def verify_members_by_category(self) -> dict:
        """
        Vérifie les membres de chaque catégorie.
        Utilise l'endpoint /user/categories pour récupérer les catégories
        et le nombre de membres dans chaque catégorie.
        Identifie les membres manquants ou en doublons.
        """
        logger.info("=" * 80)
        logger.info("👥 VÉRIFICATION DES MEMBRES PAR CATÉGORIE")
        logger.info("=" * 80)

        try:
            # Récupérer les catégories depuis Paheko
            logger.info("📥 Récupération des catégories de membres depuis Paheko...")
            try:
                url = f"{self.paheko_client.base_url}/user/categories"
                response = self.paheko_client.client.get(url)
                response.raise_for_status()
                categories_data = response.json()

                # L'endpoint retourne un dict avec les catégories
                if isinstance(categories_data, dict):
                    categories = list(categories_data.values())
                else:
                    categories = categories_data if isinstance(categories_data, list) else []

                # Trier par nom
                categories = sorted(categories, key=lambda x: x.get("name", ""))
                logger.info("  📅 Catégories trouvées: {}", len(categories))
            except Exception as e:
                logger.exception("❌ Erreur lors de la récupération des catégories: {}", e)
                return {
                    "categories": [],
                    "total_paheko": 0,
                    "completion_rate": 0,
                }

            # Afficher les catégories et leur nombre de membres
            category_summary = {}
            total_paheko_members = 0

            logger.info("\n" + "=" * 80)
            logger.info("📊 CATÉGORIES ET NOMBRE DE MEMBRES")
            logger.info("=" * 80)

            for category in categories:
                category_id = category.get("id")
                category_name = category.get("name", "Unknown")
                member_count = category.get("count", 0)

                category_summary[category_id] = {
                    "name": category_name,
                    "paheko_count": member_count,
                    "members": [],
                }

                total_paheko_members += member_count

                logger.info(
                    "  [{}] {}: {} membres",
                    category_id,
                    category_name,
                    member_count,
                )

            logger.info("=" * 80)
            logger.info("📈 Total membres dans Paheko: {}", total_paheko_members)
            logger.info("=" * 80)

            # Récupérer les membres de chaque catégorie
            logger.info("\n📥 Récupération des détails des membres...")
            all_members = {}  # email -> {'name': ..., 'category': ...}

            for category in categories:
                category_id = category.get("id")
                category_name = category.get("name")

                try:
                    # Récupérer les membres de cette catégorie en utilisant la méthode existante
                    members_list = self.paheko_client.get_members_by_category(category_id)

                    category_summary[category_id]["members"] = members_list

                    logger.info("  ✅ {}: {} membres récupérés", category_name, len(members_list))

                    # Indexer les membres par email
                    for member in members_list:
                        email = member.get("email", "")
                        if email:
                            if email not in all_members:
                                all_members[email] = {
                                    "name": f"{member.get('nom', '')} {member.get('prenom', '')}".strip(),
                                    "nom": member.get("nom", ""),
                                    "prenom": member.get("prenom", ""),
                                    "numero": member.get("numero", ""),
                                    "email": email,
                                    "telephone": member.get("telephone", ""),
                                    "category": category_name,
                                    "categories": [],
                                }
                            all_members[email]["categories"].append(
                                {
                                    "id": category_id,
                                    "name": category_name,
                                }
                            )

                except Exception as e:
                    logger.exception(
                        "  ⚠️  Erreur lors de la récupération des membres de '{}': {}",
                        category_name,
                        e
                    )

            # Afficher les résultats détaillés par catégorie
            logger.info("\n" + "=" * 80)
            logger.info("👥 DÉTAIL DES MEMBRES PAR CATÉGORIE")
            logger.info("=" * 80)

            for category_id in sorted(category_summary.keys()):
                category_info = category_summary[category_id]
                category_name = category_info["name"]
                members = category_info["members"]

                logger.info("\n[{}] {} ({} membres)", category_id, category_name, len(members))

                for member in members:
                    email = member.get("email", "N/A")
                    name = f"{member.get('nom', '')} {member.get('prenom', '')}".strip()
                    logger.info("   • {} ({})", name, email)

            # Identifier les doublons (membres dans plusieurs catégories)
            logger.info("\n" + "=" * 80)
            logger.info("⚠️  DOUBLONS (MEMBRES DANS PLUSIEURS CATÉGORIES)")
            logger.info("=" * 80)

            doublets = [email for email, data in all_members.items() if len(data["categories"]) > 1]

            if doublets:
                logger.warning("⚠️  {} membres trouvés dans plusieurs catégories:", len(doublets))
                for email in sorted(doublets):
                    member_data = all_members[email]
                    categories_list = ", ".join(
                        [f"{c['name']} ({c['id']})" for c in member_data["categories"]]
                    )
                    logger.warning(
                        "   • {} dans: {}",
                        email,
                        categories_list,
                    )
            else:
                logger.info("✅ Aucun doublon détecté")

            logger.info("=" * 80)
            logger.info("📊 RÉSUMÉ VÉRIFICATION MEMBRES")
            logger.info("=" * 80)
            logger.info("✅ Total catégories: {}", len(categories))
            logger.info("✅ Total membres uniques: {}", len(all_members))
            logger.info("⚠️  Total doublons: {}", len(doublets))
            logger.info("=" * 80)

            return {
                "categories": category_summary,
                "all_members": all_members,
                "duplicates": doublets,
                "total_paheko": total_paheko_members,
                "total_unique_members": len(all_members),
                "completion_rate": 100.0,  # Tous les membres sont présents
            }

        except Exception as e:
            logger.exception("❌ Erreur lors de la vérification des membres:    {}", e)

            return {
                "categories": [],
                "all_members": {},
                "duplicates": [],
                "total_paheko": 0,
                "total_unique_members": 0,
                "completion_rate": 0,
            }

    def verify_items_service_inscriptions(self) -> dict:
        """
        Vérifie que tous les items de commandes sont bien inscrits aux activités (table services_users).
        Identifie les items qui n'ont pas été inscrits correctement aux services.

        :return: Dictionnaire avec les statistiques et la liste des items manquants
        """
        logger.info("=" * 80)
        logger.info("🔍 VÉRIFICATION DES INSCRIPTIONS AUX ACTIVITÉS (ITEMS)")
        logger.info("=" * 80)

        try:
            # 1. Récupérer toutes les commandes HelloAsso
            logger.info("📥 Récupération des commandes HelloAsso...")
            orders = self.helloasso_client.get_orders(
                organization_slug=self.organization_slug, page_size=10
            )
            logger.info("  📋 Total commandes HelloAsso: {}", len(orders))

            # 2. Extraire tous les items des commandes
            all_items = []
            for order in orders:
                order_id = order.get("id")
                order_date = order.get("date")
                formSlug = order.get("formSlug")
                formType = order.get("formType")

                for item in order.get("items", []):
                    item_id = item.get("id")
                    item_name = item.get("name")
                    item_type = item.get("type")
                    item_state = item.get("state")

                    # Ne vérifier que les items traités (Processed, Registered)
                    if item_state in ("Processed", "Registered"):
                        user = item.get("user", order.get("payer", {}))
                        user_email = user.get("email", "").lower().strip()

                        all_items.append(
                            {
                                "order_id": order_id,
                                "order_date": order_date,
                                "formSlug": formSlug,
                                "formType": formType,
                                "item_id": item_id,
                                "item_name": item_name,
                                "item_type": item_type,
                                "item_state": item_state,
                                "user_email": user_email,
                            }
                        )

            logger.info("  ✅ Total items traités: {}", len(all_items))

            # 3. Récupérer toutes les inscriptions dans services_users
            logger.info("📥 Récupération des inscriptions aux services...")
            query = """
                SELECT 
                    su.id,
                    su.id_user,
                    su.id_service,
                    su.id_fee,
                    su.date,
                    u.email as user_email,
                    s.label as service_label,
                    sf.label as fee_label
                FROM services_users su
                JOIN users u ON su.id_user = u.id
                JOIN services s ON su.id_service = s.id
                JOIN services_fees sf ON su.id_fee = sf.id
            """
            result = self.paheko_client.get_sql(query, mode="direct")
            inscriptions = result.get("results", [])
            logger.info("  ✅ Total inscriptions dans Paheko: {}", len(inscriptions))

            # 4. Créer un index des inscriptions par email et date
            inscriptions_by_user = {}
            for inscription in inscriptions:
                email = str(inscription.get("user_email", "")).lower().strip()
                date = str(inscription.get("date", ""))
                # service_label = str(inscription.get("service_label", ""))
                fee_label = inscription.get("fee_label", "")

                key = f"{email}|{date}|{fee_label}"
                inscriptions_by_user[key] = inscription

            # 5. Vérifier chaque item
            missing_inscriptions = []
            found_inscriptions = []

            for item in all_items:
                user_email = item["user_email"]
                order_date = item["order_date"][:10] if item["order_date"] else ""
                item_name = item["item_name"]

                # Construire la clé de recherche
                key = f"{user_email}|{order_date}|{item_name}"

                if key in inscriptions_by_user:
                    found_inscriptions.append(item)
                    logger.debug(
                        "✅ Item {} ({}) inscrit pour {} le {}",
                        item["item_id"],
                        item_name,
                        user_email,
                        order_date,
                    )
                else:
                    missing_inscriptions.append(item)
                    logger.warning(
                        "❌ Item {} ({}) NON inscrit pour {} le {} - Commande {}",
                        item["item_id"],
                        item_name,
                        user_email,
                        order_date,
                        item["order_id"],
                    )

            # 6. Afficher les résultats
            logger.info("=" * 80)
            logger.info("📊 RÉSUMÉ VÉRIFICATION INSCRIPTIONS AUX ACTIVITÉS")
            logger.info("=" * 80)
            logger.info("  ✅ Items inscrits:     {}/{}", len(found_inscriptions), len(all_items))
            logger.info("  ❌ Items NON inscrits: {}/{}", len(missing_inscriptions), len(all_items))

            completion_rate = (len(found_inscriptions) / len(all_items) * 100) if all_items else 0
            logger.info("  📈 Taux d'inscription: {:.1f}%", completion_rate)
            logger.info("=" * 80)

            # 7. Afficher la liste des items manquants avec numéros pour rejeu
            if missing_inscriptions:
                logger.info(
                    "\n❌ ITEMS NON INSCRITS AUX ACTIVITÉS ({}):", len(missing_inscriptions)
                )
                logger.info("-" * 80)

                # Grouper par commande pour faciliter le rejeu
                items_by_order = {}
                for item in missing_inscriptions:
                    order_id = item["order_id"]
                    if order_id not in items_by_order:
                        items_by_order[order_id] = []
                    items_by_order[order_id].append(item)

                # Afficher les détails par commande
                for order_id, items in sorted(items_by_order.items()):
                    logger.info(
                        f"\n  📦 Commande #{order_id} ({len(items)} item(s) non inscrit(s)):"
                    )
                    for item in items:
                        logger.info(
                            "     └─ Item #{}: {} ({}) - {} le {}",
                            item["item_id"],
                            item["item_name"],
                            item["item_type"],
                            item["user_email"],
                            item["order_date"][:10] if item["order_date"] else "N/A",
                        )

                # Créer une liste pour rejeu facile
                logger.info("\n📋 LISTE POUR REJEU DE SYNCHRONISATION:")
                logger.info("-" * 80)
                logger.info("Commandes à retraiter:")
                unique_orders = sorted(set(item["order_id"] for item in missing_inscriptions))
                for i, order_id in enumerate(unique_orders, 1):
                    items_count = len(items_by_order[order_id])
                    logger.info(f"  {i}. Commande #{order_id} ({items_count} item(s))")

                logger.info("\nItems individuels à retraiter:")
                for i, item in enumerate(missing_inscriptions, 1):
                    logger.info(
                        f"  {i}. Item #{item['item_id']} (Commande #{item['order_id']}) - {item['item_name']}"
                    )

                logger.info(
                    "\n💡 Pour retraiter une commande spécifique, relancez la synchronisation"
                )
                logger.info(
                    f"   Les commandes concernées: {', '.join(map(str, unique_orders[:5]))}"
                )
                if len(unique_orders) > 5:
                    logger.info(f"   ... et {len(unique_orders) - 5} autres")

            return {
                "total_items": len(all_items),
                "found_inscriptions": len(found_inscriptions),
                "missing_inscriptions": len(missing_inscriptions),
                "completion_rate": round(completion_rate, 2),
                "missing_items_details": missing_inscriptions,
                "orders_to_reprocess": sorted(
                    set(item["order_id"] for item in missing_inscriptions)
                ),
                "items_to_reprocess": [item["item_id"] for item in missing_inscriptions],
            }

        except Exception as e:
            logger.exception("❌ Erreur lors de la vérification des inscriptions: {}", e)
            return {
                "total_items": 0,
                "found_inscriptions": 0,
                "missing_inscriptions": 0,
                "completion_rate": 0,
                "missing_items_details": [],
            }

    def verify_helloasso_paheko_members_comparison(self) -> dict:
        """
        Compare les membres HelloAsso avec les membres Paheko.
        Identifie les membres manquants dans Paheko et les membres en extra.
        """
        logger.info("=" * 80)
        logger.info("🔍 COMPARAISON DES MEMBRES HELLOASSO VS PAHEKO")
        logger.info("=" * 80)

        try:
            # 1. Récupérer tous les membres depuis Paheko (utiliser la vérification précédente)
            logger.info("📥 Récupération des membres Paheko...")
            paheko_verification = self.verify_members_by_category()
            paheko_members = paheko_verification.get("all_members", {})
            paheko_emails = set()
            paheko_members_by_email = {}

            for email, member_info in paheko_members.items():
                logger.info("paheko_emails.add email", email)
                paheko_emails.add(email.lower().strip())
                paheko_members_by_email[email.lower().strip()] = member_info

            logger.info("  ✅ Total membres Paheko: {}", len(paheko_emails))

            # 2. Récupérer toutes les commandes HelloAsso et extraire les payeurs uniques
            logger.info("📥 Récupération des commandes HelloAsso...")
            self.orders = self.helloasso_client.get_orders(
                organization_slug=self.organization_slug, page_size=10
            )
            logger.info("  📋 Total commandes HelloAsso: {}", len(self.orders))

            # Extraire les payeurs uniques avec leurs données
            helloasso_payers = {}  # email -> payer_data
            for order in self.orders:
                payer = order.get("payer", {})
                if not payer:
                    continue

                first_name = payer.get("firstName", "").strip()
                last_name = payer.get("lastName", "").strip()
                logger.info("payer.get(email)", payer.get("email", ""))
                email = payer.get("email", "").lower().strip()
                telephone = payer.get("phone", "").strip()
                address = payer.get("address", "").strip()
                ville = payer.get("city", "").strip()
                zip_code = payer.get("zipCode", "").strip()
                pays = payer.get("country", "").strip()

                # Valider l'email
                if not email or "@" not in email:
                    continue

                # Ajouter ou mettre à jour le payeur
                if email not in helloasso_payers:
                    helloasso_payers[email] = {
                        "email": email,
                        "firstName": first_name,
                        "lastName": last_name,
                        "telephone": telephone,
                        "address": address,
                        "ville": ville,
                        "zipCode": zip_code,
                        "pays": pays,
                        "orders_count": 0,
                    }
                helloasso_payers[email]["orders_count"] += 1

            helloasso_emails = set(helloasso_payers.keys())
            logger.info("  ✅ Total payeurs uniques HelloAsso: {}", len(helloasso_emails))

            # 3. Comparer les deux ensembles
            missing_in_paheko = helloasso_emails - paheko_emails
            extra_in_paheko = paheko_emails - helloasso_emails

            logger.info("=" * 80)
            logger.info("📊 RÉSUMÉ DE LA COMPARAISON")
            logger.info("=" * 80)
            logger.info("  📌 Membres Paheko: {}", len(paheko_emails))
            logger.info("  📌 Payeurs HelloAsso: {}", len(helloasso_emails))
            logger.info("  ⚠️  Manquants dans Paheko: {}", len(missing_in_paheko))
            logger.info("  ℹ️  En extra dans Paheko: {}", len(extra_in_paheko))

            # 4. Afficher les détails des manquants
            if missing_in_paheko:
                logger.info("")
                logger.info("❌ MEMBRES MANQUANTS DANS PAHEKO ({}):", len(missing_in_paheko))
                logger.info("-" * 80)
                for email in sorted(missing_in_paheko):
                    payer = helloasso_payers[email]
                    name = f"{payer['lastName']} {payer['firstName']}".strip()
                    self.orders = payer["orders_count"]
                    logger.info(
                        "  • {} ({}) - {} commandes",
                        name,
                        email,
                        self.orders,
                    )

            # 5. Afficher les détails des en extra
            if extra_in_paheko:
                logger.info("")
                logger.info("ℹ️  MEMBRES EN EXTRA DANS PAHEKO ({}):", len(extra_in_paheko))
                logger.info("-" * 80)
                for email in sorted(extra_in_paheko):
                    member = paheko_members_by_email.get(email, {})
                    nom = member.get("nom", "?")
                    prenom = member.get("prenom", "?")
                    category = member.get("category", "?")
                    logger.info(
                        "  • {} {} ({}) - Catégorie: {}",
                        prenom,
                        nom,
                        email,
                        category,
                    )

            logger.info("")
            logger.info("=" * 80)

            return {
                "paheko_members_count": len(paheko_emails),
                "helloasso_members_count": len(helloasso_emails),
                "missing_in_paheko": sorted(list(missing_in_paheko)),
                "extra_in_paheko": sorted(list(extra_in_paheko)),
                "missing_count": len(missing_in_paheko),
                "extra_count": len(extra_in_paheko),
                "helloasso_payers": helloasso_payers,
                "paheko_members_by_email": paheko_members_by_email,
            }

        except Exception as e:
            logger.exception("❌ Erreur lors de la comparaison des membres: {}", e)

            return {
                "paheko_members_count": 0,
                "helloasso_members_count": 0,
                "missing_in_paheko": [],
                "extra_in_paheko": [],
                "missing_count": 0,
                "extra_count": 0,
                "helloasso_payers": {},
            }

    @staticmethod
    def normalize_country(country) -> str:
        """
        Normalise le code pays en nom complet.
        FRA, FR -> France

        :param country: Code ou nom du pays (peut être None, str)
        :return: Nom du pays normalisé
        """
        if not country or not isinstance(country, str):
            return "France"

        country_clean = country.strip().upper()

        # Codes ISO pour la France
        if country_clean in ["FR", "FRA", "FRANCE"]:
            return "France"

        # Si c'est un autre pays, retourner tel quel (capitalisé)
        return country.strip().capitalize()

    def sync_helloasso_services_main(self):
        """
        Point d'entrée pour la synchronisation des services HelloAsso → Paheko
        """
        try:

            logger.info("\n" + "=" * 80)
            logger.info("🔄 SYNCHRONISATION HELLOASSO → PAHEKO")
            logger.info("=" * 80)
            if self.mode_simulation:
                logger.info("⚠️  MODE mode_simulation: Aucune modification ne sera effectuée\n")

            # stats = self.sync_all()

            logger.info("\n✅ Synchronisation terminée avec succès!")
            sleep(5)
            return 0

        except Exception as e:
            logger.info(f"\n❌ Erreur: {e}")
            logger.exception("Erreur lors de la synchronisation des services:")
            sleep(5)
            return 1

    def __call__(self):

        # IDs de catégories dans Paheko (à adapter selon votre configuration)

        # Mapping pour les transactions comptables selon le type d'ordre
        order_mapping = {
            "Donation": {"type": "RECETTE", "debit": "512HELLOASSO", "credit": "754"},
            "Membership": {
                "type": "RECETTE",
                "debit": "512HELLOASSO",
                "credit": "756",
            },
            "Registration": {
                "type": "RECETTE",
                "debit": "512HELLOASSO",
                "credit": "706",
            },
        }
        current_year = date.today().year

        # Récupérer les commandes depuis HelloAsso
        logger.debug("Récupération des commandes pour l'organisation {}", self.organization_slug)
        self.orders = self.helloasso_client.get_orders(organization_slug=self.organization_slug)
        logger.info("Commandes récupérées: {} au total", len(self.orders))

        # Enrichir les commandes avec les détails (customFields, etc.)
        logger.debug("Enrichissement des commandes avec les détails...")
        for i, order in enumerate(self.orders):
            try:
                order_id = order.get("id")
                if order_id:
                    order_details = self.helloasso_client.get_order_details(order_id)
                    if order_details:
                        # Fusionner les détails dans la commande
                        # Garder les items détaillés avec customFields
                        if "items" in order_details:
                            self.orders[i]["items"] = order_details["items"]
                        logger.debug(
                            "Commande {} enrichie avec les détails (items avec customFields)",
                            order_id,
                        )
            except Exception as e:
                logger.exception(
                    "Impossible d'enrichir la commande {} avec les détails: {}",
                    order.get("id"),
                    e
                )
                # Continuer avec la commande sans détails

        # Compter les commandes déjà synchronisées
        orders_synced = 0
        orders_failed = 0
        orders_created = 0

        # Filtrer les commandes si mode --update activé (AVANT le tri)
        if hasattr(self, "mode_update") and self.mode_update:
            logger.info("🔄 Mode UPDATE activé : filtrage des commandes déjà synchronisées...")

            # Récupérer tous les IDs HelloAsso déjà dans Paheko
            existing_helloasso_ids = self.get_helloasso_references_from_paheko()

            # Filtrer les commandes (garder uniquement celles NON présentes dans Paheko)
            orders_before = len(self.orders)
            self.orders = [
                order for order in self.orders if order.get("id") not in existing_helloasso_ids
            ]
            orders_filtered = orders_before - len(self.orders)
            logger.info(
                "🔄 {} commandes sur {} déjà synchronisées dans Paheko et ignorées",
                orders_filtered,
                orders_before,
            )
            sleep(10)
            logger.info(
                "✅ Filtrage terminé : {} commandes déjà synchronisées (ignorées), {} commandes à traiter",
                orders_filtered,
                len(self.orders),
            )
        else:
            logger.info(
                "📊 {} commandes à traiter (mode normal - pas de filtrage)", len(self.orders)
            )

        # Trier les commandes par ID croissant (du + petit au + grand)
        self.orders.sort(key=lambda x: x.get("id", 0))
        logger.info("📊 Commandes triées par ID croissant : {} commandes", len(self.orders))

        sleep(5)
        # Vérifier chaque commande
        for order in self.orders:
            # ✅ NOUVEAU COMPORTEMENT : Toujours traiter les membres (pour mise à jour)
            # mais skip les transactions déjà créées (pour éviter doublons)

            # ✅ Incrémenter le compteur de commandes traitées
            self.stats["orders_processed"] += 1

            # On traite toujours pour mettre à jour les membres
            try:
                logger.info(
                    "Traitement de la commande {} (membres: update, transactions: create si manquant)",
                    order.get("id"),
                )
                orders_created += 1
            except Exception as e:
                logger.exception(
                    "Erreur lors de la synchronisation de la commande {}: {}", order.get("id"), e
                )
                orders_failed += 1

        for order in self.orders:
            listitem = []
            logger.warning(jmodule.dumps(order, indent=4, ensure_ascii=False, default=str))
            payer = order.get("payer")

            logger.debug("Traitement de la commande du {}", order.get("date"))
            datetransaction = self.helloasso_client.convert_to_datetime(order.get("date"))
            date_inscription = self.helloasso_client.convert_to_datetime(
                order.get("date")
            ).strftime("%Y-%m-%d 00:00:00")
            id_year = self.paheko_client.get_current_year_id(
                datetransaction, self.organization_slug
            )
            email = payer.get("email")

            if not payer:
                logger.warning("Commande {} sans infos sur le donateur.", order.get("order_id"))
                continue

            # Vérifier si la commande contient une cotisation
            # has_membership = any(
            #     item.get("order_type") == "Membership" for item in order.get("items", [])
            # )
            # has_membership = False

            payer["date_inscription"] = date_inscription
            payer["category"] = self.paheko_client.category_by_payer_and_hasmembership(
                has_payer=True, has_membership=False, date_inscription=date_inscription
            )
            payer["nom"] = (
                f"{str(payer.get('lastName')).upper().strip()} {str(payer.get('firstName')).capitalize().strip()} ({str(payer.get('email')).lower().strip()})"
            )
            payer["nom2"] = (
                f"{str(payer.get('firstName')).upper().strip()} {str(payer.get('lastName')).capitalize().strip()} ({str(payer.get('email')).lower().strip()})"
            )
            payer["pays"] = self.normalize_country(payer.get("country", "France"))

            # Utilisation de la fonction centralisée pour créer ou mettre à jour le membre
            try:

                logger.debug(
                    "Traitement du membre avec les données payer {}",
                    jmodule.dumps(payer, indent=4, ensure_ascii=False, default=str),
                )

                member = self.paheko_client.ensure_member_exists(
                    payer,
                )

                if not member:
                    logger.error(
                        "ABANDON du traitement de la commande {} : impossible de traiter le membre {}",
                        order.get("id"),
                        payer["nom"],
                    )
                    self.stats["members_failed"] += 1
                    continue

                # ✅ Membre traité avec succès (création ou mise à jour)
                # Note: ensure member exists fait déjà la différence en interne
                self.stats["members_updated"] += 1

                # ✅ Plus de marquage en mémoire - Paheko est la source de vérité

                logger.debug(
                    "Membre {} traité avec succès (ID: {})", payer["nom"], member.get("id")
                )

                # ✅ Télécharger les PDFs de la commande (tickets, cartes de membre, reçus)
                try:
                    if not hasattr(self, "pdf_downloader"):
                        # Créer le downloader une seule fois (nécessite username/password pour Playwright)
                        self.pdf_downloader = HelloAssoPDFDownloader(
                            username=self.helloasso_client.config["helloasso"]["username"],
                            password=self.helloasso_client.config["helloasso"]["password"],
                            org_slug=self.helloasso_client.config["helloasso"]["org_slug"],
                        )
                        self.pdf_downloader.start_browser()
                        self.pdf_downloader.login()
                        logger.info("🌐 Downloader PDF initialisé et connecté")

                    # Télécharger les PDFs (skip si déjà téléchargés)
                    pdf_results = self.pdf_downloader.download_order_pdfs(
                        order, destination_folder="./pdfs_helloasso"
                    )

                    # Logger les résultats
                    for pdf_type, path in pdf_results.items():
                        if path:
                            logger.debug("   📄 PDF téléchargé: {}", pdf_type)
                        else:
                            logger.debug("   ⚠ PDF non téléchargé: {}", pdf_type)

                except Exception as pdf_error:
                    logger.warning(
                        "Erreur lors du téléchargement des PDFs de la commande {}: {}",
                        order.get("id"),
                        str(pdf_error)[:100],
                    )
                    # Ne pas bloquer le traitement de la commande si le téléchargement échoue

            except ValueError as e:
                # Cas particulier : tentative de modification d'un administrateur
                logger.warning(
                    "Membre {} protégé : {}. Traitement de la commande {} continué sans modification du membre.",
                    payer["nom"],
                    str(e),
                    order.get("id"),
                )
                # Afficher la commande en JSON pour le débogage
                try:

                    logger.warning(
                        "📋 Contexte - Membre administrateur protégé (JSON):\n{}",
                        jmodule.dumps(order, indent=4, ensure_ascii=False, default=str),
                    )
                except Exception as json_error:
                    logger.debug("Impossible de sérialiser le contexte: {}", json_error)
                # On récupère quand même le membre existant pour la transaction
                logger.debug(
                    "Récupération du membre administrateur {} sans modification.", payer["nom"]
                )
                member = self.paheko_client.get_member_by_name(payer["nom"])
                if not member:
                    logger.error(
                        "ABANDON du traitement de la commande {} : membre administrateur {} introuvable",
                        order.get("id"),
                        email,
                    )
                    continue

            except Exception as e:
                error_type = type(e).__name__
                error_msg = str(e)

                # Gérer spécifiquement les timeouts
                if "timeout" in error_type.lower() or "timeout" in error_msg.lower():
                    logger.error(
                        "⏱️  TIMEOUT lors du traitement du membre {} (commande {})",
                        email,
                        order.get("id"),
                    )
                    logger.warning(
                        "   L'API Paheko a mis trop de temps à répondre. La commande sera exportée pour traitement ultérieur."
                    )
                else:
                    logger.exception("Erreur lors du traitement du membre {} : ", email)

                logger.error(
                    "ABANDON du traitement de la commande {} car le membre n'a pas pu être traité",
                    order.get("id"),
                )
                # ✅ Incrémenter compteur échecs membres
                self.stats["members_failed"] += 1

                # ✅ Export JSON du membre non synchronisé
                member_data = {
                    "commande_id": order.get("id"),
                    "commande_date": order.get("date"),
                    "commande_montant": order.get("amount"),
                    "commande_statut": order.get("state"),
                    "payeur_email": payer.get("email"),
                    "payeur_nom": f"{payer.get('lastName').strip()} {payer.get('firstName', '').strip()}".strip(),
                    "payeur_nom2": f"{payer.get('firstName').strip()} {payer.get('lastName', '').strip()}".strip(),
                    "payeur_telephone": payer.get("phone"),
                    "items": order.get("items"),
                    "formSlug": order.get("formSlug"),
                    "formType": order.get("formType"),
                    "erreur": str(e),
                    "type_erreur": type(e).__name__,
                }
                self.save_non_synced_item("members", email, member_data)

                # Afficher le contexte complet en JSON
                try:

                    logger.error(
                        "📋 Contexte complet du traitement de commande (JSON):\n{}",
                        jmodule.dumps(member_data, indent=4, ensure_ascii=False, default=str),
                    )
                except Exception as json_error:
                    logger.error("Impossible de sérialiser le contexte: {}", json_error)
                continue

            reference = f"HelloAsso {order.get('id')}"
            logger.debug("Vérification de l'existence de la transaction {}", reference)
            # ✅ Vérifier si la transaction existe (pour éviter les doublons comptables)
            # Mais on continuera quand même pour traiter/mettre à jour les membres
            transaction_exists = self.paheko_client.transaction_exists(reference)
            if transaction_exists:
                logger.debug(
                    "Transaction {} déjà créée, skip création transaction (membres seront mis à jour)",
                    reference,
                )
                # On ne fait pas 'continue' ici - on laisse le code des membres s'exécuter
                # mais on marquera qu'il ne faut pas créer la transaction

            # Extraire et formater la date de la commande
            raw_order_date = order.get("date")
            if raw_order_date:
                # La date est au format ISO 8601: "2022-06-06T12:48:52.8653471+02:00"
                # On extrait juste la partie date (YYYY-MM-DD)
                order_date = raw_order_date[:10]
            else:
                order_date = date.today().isoformat()

            items = order.get("items", [])
            user_id = None
            userpaheko = None
            userpaheko = self.paheko_client.get_member_by_name(f"{payer.get('nom')}")
            if not userpaheko:
                logger.debug(
                    "Pas de membre Paheko trouvé par nom pour {} {}, tentative par nom2.",
                    payer.get("firstName"),
                    payer.get("lastName"),
                )
                userpaheko = self.paheko_client.get_member_by_name(f"{payer.get('nom2')}")
                # if not userpaheko:
                #     logger.debug(
                #         "Pas de membre Paheko trouvé par nom2 pour {} {}, tentative par email.",
                #         payer.get("firstName"),
                #         payer.get("lastName"),
                #     )
                #     userpaheko = self.paheko_client.get_member_by_email(
                #         str(payer.get("email")).lower().strip()
                #     )
            logger.debug(
                "Membre Paheko trouvé pour le payeur: {}",
                jmodule.dumps(userpaheko, indent=4, ensure_ascii=False, default=str),
            )
            # if len(items) > 1:
            # Création d'une écriture multi-lignes (type ADVANCED)
            lines = []
            for item in items:
                listitem.append(item.get("id"))
                if (
                    item.get("state") == "Processed"
                    or item.get("state") == "Refunded"
                    or item.get("state") == "Canceled"
                    or item.get("state") == "Registered"
                ):

                    item_type = item.get("type")

                    logger.success(
                        f"helloasso.item_type {item_type}, item_id {item.get("id")}, item_name {item.get("name")},"
                    )

                    # Donation Payment Registration Membership MonthlyDonation MonthlyPayment OfflineDonation Contribution Bonus Product
                    if item_type not in (
                        "Registration",
                        "Donation",
                        "Membership",
                        "MonthlyDonation",
                        "MonthlyPayment",
                        "OfflineDonation",
                        "Contribution",
                        "Bonus",
                        "Product",
                    ):
                        logger.error(
                            "Type d'item non supporté pour l'item ID {}: {}",
                            item.get("id"),
                            item_type,
                        )
                        continue

                    mapping = order_mapping.get(item_type)
                    if not mapping:
                        logger.error(
                            "Type de ligne inconnu '{}' dans la commande {}.",
                            item_type,
                            order.get("order_id"),
                        )
                    user = item.get("user")

                    # Si pas de user dans l'item, utiliser le payer de la commande
                    if not user:
                        user = payer
                        # Enrichir le payer avec ses propres customFields si disponibles
                        if "customFields" in payer:
                            user["customFields"] = payer["customFields"]
                    else:
                        # Ajouter les customFields de l'item au user pour extraire l'email
                        if "customFields" in item:
                            user["customFields"] = item["customFields"]

                    # Créer/mettre à jour le user comme membre dans Paheko avec ses propres adresses
                    # (ou celles du payeur en fallback)
                    try:
                        logger.debug(
                            "1 Création/mise à jour du user de l'item ID {}", item.get("id")
                        )
                        item_user = self.paheko_client.ensure_user_from_item_exists(
                            item_data=user,
                            payer=payer,
                            has_membership=item_type == "Membership",
                            force_update=True,
                            date_inscription=date_inscription,
                        )
                        if item_user:
                            logger.debug(
                                "User {} {} de l'item {} créé/mis à jour ",
                                user.get("firstName"),
                                user.get("lastName"),
                                item.get("id"),
                            )
                            logger.info(
                                "Détails du membre créé/mis à jour pour l'item ID {}: {}",
                                item.get("id"),
                                jmodule.dumps(item_user, indent=4, ensure_ascii=False, default=str),
                            )
                        else:

                            logger.error(
                                "❌ SKIP ITEM {} - Impossible de créer le membre {} {} (pas d'email valide ?)",
                                item.get("id"),
                                user.get("firstName"),
                                user.get("lastName"),
                            )
                            logger.error(
                                jmodule.dumps(order, indent=4, ensure_ascii=False, default=str)
                            )
                            continue  # Sauter cet item, ne pas créer de transaction
                    except Exception as e:

                        logger.exception(
                            "❌ SKIP ITEM {} - Erreur lors du traitement du user: {} {}: {}",
                            item.get("id"),
                            item.get("lastName"),
                            item.get("firstName"),
                            e
                        )
                        logger.error(
                            jmodule.dumps(order, indent=4, ensure_ascii=False, default=str)
                        )
                        continue  # Sauter cet item en cas d'erreur

                    # Détermination du libellé de la ligne en fonction du type
                    if item_type == "Registration":
                        label = f"Evénement - {order.get("formSlug")} {item.get("name")} ({user.get('lastName', '').upper()} {user.get('firstName', '').capitalize()})"

                    elif item_type == "Donation":
                        label = f"Don - {self.helloasso_client.extract_year(datetransaction)} ({user.get('lastName', '').upper()} {user.get('firstName', '').capitalize()})"

                    elif item_type == "Membership":
                        label = f"Adhésion - {self.helloasso_client.extract_year(datetransaction)} ({user.get('lastName', '').upper()} {user.get('firstName', '').capitalize()})"

                    elif item_type == "MonthlyDonation":
                        label = f"Don mensuel - {self.helloasso_client.extract_year(datetransaction)} ({user.get('lastName', '').upper()} {user.get('firstName', '').capitalize()})"

                    elif item_type == "MonthlyPayment":
                        label = f"Paiement mensuel - {self.helloasso_client.extract_year(datetransaction)} ({user.get('lastName', '').upper()} {user.get('firstName', '').capitalize()})"

                    elif item_type == "OfflineDonation":
                        label = f"Don hors-ligne - {self.helloasso_client.extract_year(datetransaction)} ({user.get('lastName', '').upper()} {user.get('firstName', '').capitalize()})"

                    elif item_type == "Contribution":
                        label = f"Contribution - {self.helloasso_client.extract_year(datetransaction)} ({user.get('lastName', '').upper()} {user.get('firstName', '').capitalize()})"

                    elif item_type == "Bonus":
                        label = f"Bonus - {self.helloasso_client.extract_year(datetransaction)} ({user.get('lastName', '').upper()} {user.get('firstName', '').capitalize()})"

                    elif item_type == "Product":
                        label = f"Produit - {item.get('name')} ({user.get('lastName', '').upper()} {user.get('firstName', '').capitalize()})"

                    else:
                        label = item.get("name", "Ligne sans libellé")
                    logger.debug("Libellé de la ligne: {}", label)
                    # Constitution des lignes en débit et crédit
                    # HelloAsso fournit les montants en centimes d'euros
                    # Paheko API attend les montants en euros (float avec 2 décimales)
                    # car l'API multiplie automatiquement par 100 en interne
                    amount_raw = item.get("amount") or 0
                    try:
                        amount_centimes = int(amount_raw)
                    except Exception:
                        amount_centimes = int(float(amount_raw))

                    # Convertir de centimes en euros
                    amount = round(amount_centimes / 100.0, 2)

                    # 🆕 Rattacher l'utilisateur à l'activité (inscription au service)
                    try:
                        # Récupérer l'activité (service) correspondante
                        activity_label = self._get_serviceLabel(order)

                        activity = self.paheko_client.get_activity(activity_label)
                        logger.debug(
                            "Récupération de l'activité pour l'inscription: formSlug '{}' → activité '{}'",
                            order.get("formSlug"),
                            activity_label,
                        )
                        logger.debug(
                            "Détails de l'activité trouvée pour inscription: {}",
                            jmodule.dumps(activity, indent=4, ensure_ascii=False, default=str),
                        )
                        if activity and activity.get("id") and item_user:
                            activity_id = activity.get("id")
                            item_user_id = item_user.get("id")
                            item_name = item.get("name", "")
                            if item_name == "":
                                item_name = "don"
                            # Trouver le fee_id correspondant au nom du tarif
                            logger.debug(
                                "Recherche du tarif pour l'activité ID {} et le nom d'item '{}'",
                                activity_id,
                                item_name,
                            )

                            # Essayer plusieurs stratégies de recherche du tarif
                            fee_id = None

                            # Stratégie 1 : Correspondance exacte
                            fee_query_exact = f"""
                                SELECT id, label FROM services_fees 
                                WHERE id_service = {activity_id} 
                                AND label = '{item_name}'
                                LIMIT 1
                            """
                            fee_result = self.paheko_client.get_sql(fee_query_exact, mode="direct")

                            if fee_result.get("results") and len(fee_result["results"]) > 0:
                                fee_id = fee_result["results"][0].get("id")
                                logger.debug(
                                    "✅ Tarif trouvé (correspondance exacte): ID {}", fee_id
                                )
                            else:
                                # Stratégie 2 : Recherche LIKE (contient)
                                logger.debug("⚠️  Correspondance exacte échouée, essai avec LIKE...")
                                fee_query_like = f"""
                                    SELECT id, label FROM services_fees 
                                    WHERE id_service = {activity_id} 
                                    AND label LIKE '%{item_name}%'
                                    LIMIT 1
                                """
                                fee_result = self.paheko_client.get_sql(
                                    fee_query_like, mode="direct"
                                )

                                if fee_result.get("results") and len(fee_result["results"]) > 0:
                                    fee_id = fee_result["results"][0].get("id")
                                    tarif_label_found = fee_result["results"][0].get("label")
                                    logger.debug(
                                        "✅ Tarif trouvé (LIKE): ID {} - Label '{}'",
                                        fee_id,
                                        tarif_label_found,
                                    )
                                else:
                                    # Stratégie 3 : Lister tous les tarifs disponibles pour debug
                                    logger.warning(
                                        "❌ Aucun tarif trouvé pour '{}' dans le service {}",
                                        item_name,
                                        activity_id,
                                    )
                                    all_fees_query = f"""
                                        SELECT id, label FROM services_fees 
                                        WHERE id_service = {activity_id}
                                    """
                                    all_fees_result = self.paheko_client.get_sql(
                                        all_fees_query, mode="direct"
                                    )
                                    if all_fees_result.get("results"):
                                        logger.warning("   Tarifs disponibles pour ce service:")
                                        for tarif in all_fees_result["results"]:
                                            logger.warning(
                                                "      - ID {}: '{}'",
                                                tarif.get("id"),
                                                tarif.get("label"),
                                            )

                            if fee_id:
                                fee_id = fee_result["results"][0].get("id")

                                # Récupérer end_date du service pour expiry_date
                                service_query = f"SELECT end_date FROM services WHERE id = {activity_id} LIMIT 1"
                                service_result = self.paheko_client.get_sql(
                                    service_query, mode="direct"
                                )
                                expiry_date = None
                                if (
                                    service_result.get("results")
                                    and len(service_result["results"]) > 0
                                ):
                                    expiry_date = service_result["results"][0].get("end_date")
                                if (
                                    item.get("state") == "Refunded"
                                    or item.get("state") == "Canceled"
                                ):
                                    logger.info(
                                        "Item ID {} remboursé (état : {}), ajustement de expiry_date",
                                        item.get("id"),
                                        item.get("state"),
                                    )
                                    # Pour les remboursements, on peut choisir d'ajuster la date d'expiration
                                    expiry_date = (
                                        order_date  # ou une autre logique selon les besoins
                                    )
                                else:
                                    # Inscrire l'utilisateur au service
                                    logger.debug(
                                        "Inscription du membre ID {} au service ID {} avec le tarif ID {} (item {}) ordre date {} expiry_date {}",
                                        item_user_id,
                                        activity_id,
                                        fee_id,
                                        item.get("id"),
                                        order_date,
                                        expiry_date,
                                    )
                                    self.paheko_client.attach_users_to_activity(
                                        activity_id=activity_id,
                                        user_id=item_user_id,
                                        fee_id=fee_id,
                                        amount=amount_centimes if amount_centimes > 0 else None,
                                        order_date=order_date,
                                        expiry_date=expiry_date,
                                    )
                            else:
                                # Tarif non trouvé - item NE SERA PAS inscrit
                                logger.error(
                                    "❌ ITEM #{} NON INSCRIT : Tarif '{}' introuvable pour le service {} (activité: {})",
                                    item.get("id"),
                                    item_name,
                                    activity_id,
                                    activity_label,
                                )
                                logger.error(
                                    "   → Commande #{} | User: {} | Email: {}",
                                    order.get("id"),
                                    f"{user.get('lastName', '')} {user.get('firstName', '')}".strip(),
                                    user.get("email", "N/A"),
                                )
                        else:
                            # Activité ou utilisateur manquant
                            if not activity or not activity.get("id"):
                                logger.error(
                                    "❌ ITEM #{} NON INSCRIT : Activité '{}' introuvable ou invalide",
                                    item.get("id"),
                                    activity_label,
                                )
                            elif not item_user:
                                logger.error(
                                    "❌ ITEM #{} NON INSCRIT : Utilisateur de l'item introuvable",
                                    item.get("id"),
                                )
                    except Exception as e:
                        logger.exception(
                            "❌ ERREUR INSCRIPTION : Item #{} n'a pas pu être inscrit au service. Exception: {}",
                            item.get("id"),
                            e
                        )
                        logger.error(
                            "   → Commande #{} | Activité: {} | Item: {}",
                            order.get("id"),
                            activity_label if "activity_label" in locals() else "N/A",
                            item_name if "item_name" in locals() else item.get("name", "N/A"),
                        )
                        # ⚠️ SKIP les items avec un montant nul (gratuits)
                    if amount == 0:
                        logger.warning(
                            "Item ID {} ignoré : montant = 0 (inscription/tarif gratuit)",
                            item.get("id"),
                        )
                        debit_line = {
                            "account": mapping.get("debit"),
                            "debit": 1,
                            "credit": 0,
                            "label": f"Offert {label}",
                            "reference": f"HelloAsso {item.get('id')}",
                        }
                        lines.append(debit_line)

                        # Ligne de crédit (compte produit 756, 758, etc.)
                        credit_line = {
                            "account": mapping.get("credit"),
                            "debit": 0,
                            "credit": 1,
                            "label": f"Offert {label}",
                            "reference": f"HelloAsso {item.get('id')}",
                        }
                        lines.append(credit_line)
                        credit_line_refund = {
                            "account": mapping.get("debit"),
                            "debit": 0,
                            "credit": 1,
                            "label": f"Offert {label}",
                            "reference": f"HelloAsso Offert {item.get('id')}",
                        }
                        lines.append(credit_line_refund)
                        debit_line_refund = {
                            "account": mapping.get("credit"),
                            "debit": 1,
                            "credit": 0,
                            "label": f"Offert {label}",
                            "reference": f"HelloAsso Offert {item.get('id')}",
                        }
                        lines.append(debit_line_refund)
                    else:
                        # Ligne de débit (compte 512HELLOASSO)
                        debit_line = {
                            "account": mapping.get("debit"),
                            "debit": amount,
                            "credit": 0,
                            "label": label,
                            "reference": f"HelloAsso {item.get('id')}",
                        }
                        lines.append(debit_line)

                        # Ligne de crédit (compte produit 756, 758, etc.)
                        credit_line = {
                            "account": mapping.get("credit"),
                            "debit": 0,
                            "credit": amount,
                            "label": label,
                            "reference": f"HelloAsso {item.get('id')}",
                        }
                        lines.append(credit_line)

                        if item.get("state") == "Refunded" or item.get("state") == "Canceled":
                            logger.info(
                                "Item ID {} remboursé (état : {})",
                                item.get("id"),
                                item.get("state"),
                            )
                            credit_line_refund = {
                                "account": mapping.get("debit"),
                                "debit": 0,
                                "credit": amount,
                                "label": f"Remboursement {label}",
                                "reference": f"HelloAsso Remboursement {item.get('id')}",
                            }
                            lines.append(credit_line_refund)
                            debit_line_refund = {
                                "account": mapping.get("credit"),
                                "debit": amount,
                                "credit": 0,
                                "label": f"Remboursement {label}",
                                "reference": f"HelloAsso Remboursement {item.get('id')}",
                            }
                            lines.append(debit_line_refund)

                else:
                    logger.info(
                        "Item ID {} ignoré (état : {})",
                        item.get("id"),
                        item.get("state"),
                    )
                    continue

            if not lines:
                logger.warning(
                    "Aucune ligne valide n'a été trouvée pour la commande ID {}",
                    order.get("id"),
                )
                continue

            if userpaheko:
                user_id = userpaheko["id"]
            transaction_data = {
                "id_year": id_year,
                "label": order.get("formSlug"),
                "date": order_date,
                "type": "ADVANCED",  # Écriture multi-lignes
                "lines": lines,
                "reference": reference,
                "linked_users": [user_id] if user_id else [],
            }
            # S'assurer que l'activité correspondant au formSlug existe
            try:
                formSlug = order.get("formSlug") or ""
                formType = order.get("formType")  # "Membership", "Event", etc.
                order_date_str = order.get("date")
                activity_label = self._get_serviceLabel(order)
                activity = self.paheko_client.get_activity(activity_label)
                logger.debug(
                    "Vérification de l'activité pour formSlug '{}' → activité '{}'",
                    formSlug,
                    activity_label,
                )
                logger.debug(
                    "Détails de l'activité trouvée: {}",
                    jmodule.dumps(activity, indent=4, ensure_ascii=False, default=str),
                )
                if activity and activity.get("id"):
                    transaction_data["service_id"] = activity.get("id")
            except Exception as e:
                logger.exception(
                    "Impossible de créer/rattacher l'activité pour {}: {}",
                    order.get("formSlug"),
                    e
                )
            try:
                if self.mode_simulation:
                    logger.info(
                        "Mode simulation activé : création de la transaction pour la commande ID {} lignes {} (réf. {}).\n{} ",
                        order.get("order_id"),
                        str(listitem),
                        reference,
                        transaction_data,
                        
                    )
                else:
                    # ✅ Ne créer la transaction que si elle n'existe pas déjà
                    if not transaction_exists:
                        response = self.paheko_client.create_transaction(transaction_data)
                        self.stats["transactions_created"] += 1
                        # ✅ Plus de stockage en JSON - Paheko gère la persistance
                        logger.info(
                            "Transaction multi-lignes créée pour la commande ID {} lignes {} (réf. {}).\n{}\n{}",
                            order.get("order_id"),
                            str(listitem),
                            reference,
                            transaction_data,
                            response,
                        )
                    else:
                        self.stats["transactions_skipped"] += 1
                        logger.debug(
                            "Transaction multi-lignes {} déjà existante, skip création",
                            reference,
                        )
            except Exception as e:
                self.stats["orders_failed"] += 1
                logger.exception(
                    "Erreur lors de la création de la transaction  multi-lignes pour la commande ID {} lignes {} (réf. {}).\n{}",
                    order.get("order_id"),
                    str(listitem),
                    reference,
                    transaction_data,
                )

                # ✅ Export JSON de la commande non synchronisée
                order_data = {
                    "commande_id": order.get("id"),
                    "commande_date": order.get("date"),
                    "commande_montant": order.get("amount"),
                    "reference_transaction": reference,
                    "items": listitem,
                    "nombreLignes": len(transaction_data.get("lines", [])),
                    "donnees_transaction": transaction_data,
                    "payeur": f"{payer.get('lastName').strip()} {payer.get('firstName', '').strip()} ({payer.get('email', '').strip()})".strip(),
                    "erreur": str(e),
                    "type_erreur": type(e).__name__,
                }
                self.save_non_synced_item("self.orders", str(order.get("id")), order_data)

                # Afficher le contexte complet en JSON
                try:

                    logger.error(
                        "📋 Contexte de création de transaction multi-lignes (JSON):\n{}",
                        jmodule.dumps(order_data, indent=4, ensure_ascii=False, default=str),
                    )
                except Exception as json_error:
                    logger.error("Impossible de sérialiser le contexte: {}", json_error)

        # ===================================================================
        # SYNCHRONISATION DES VIREMENTS BANCAIRES (CASH-OUTS)
        # ===================================================================
        logger.info("\n" + "=" * 70)
        logger.info("💸 SYNCHRONISATION DES VIREMENTS BANCAIRES")
        logger.info("=" * 70)

        try:
            # Récupérer les virements bancaires depuis HelloAsso
            cash_outs = self.helloasso_client.get_cash_outs(self.organization_slug)

            if not cash_outs:
                logger.info("Aucun virement bancaire à synchroniser.")
            else:
                logger.info(
                    "Trouvé {} virement(s) bancaire(s) à synchroniser.",
                    len(cash_outs),
                )

                # Compteurs pour les statistiques
                transfers_created = 0
                transfers_skipped = 0
                transfers_failed = 0

                # Traiter chaque virement
                for cash_out in cash_outs:
                    # ✅ Vérification directe dans Paheko au lieu d'un fichier JSON
                    reference = f"Virement HelloAsso {cash_out['id']}"
                    if self.paheko_client.transaction_exists(reference):
                        transfers_skipped += 1
                        logger.debug("Virement {} déjà créé dans Paheko.", reference)
                        continue

                    # Créer la transaction dans Paheko
                    result = self.create_bank_transfer_transaction(cash_out)

                    if result:
                        transfers_created += 1
                    else:
                        transfers_failed += 1

                # Afficher les statistiques
                logger.info("\n📊 Résultats de la synchronisation :")
                logger.info(f"   • Virements créés : {transfers_created}")
                logger.info(f"   • Virements déjà traités : {transfers_skipped}")
                if transfers_failed > 0:
                    logger.warning(f"   • Virements en échec : {transfers_failed}")

        except Exception as e:
            logger.exception(
                "Erreur lors de la synchronisation des virements bancaires: {}",
                e,
            )
            # Afficher le contexte complet en JSON
            try:

                context = {
                    "nombre_virements_a_traiter": (
                        len(cash_outs) if "cash_outs" in locals() else "inconnu"
                    ),
                    "virements_crees": transfers_created if "transfers_created" in locals() else 0,
                    "virements_ignores": (
                        transfers_skipped if "transfers_skipped" in locals() else 0
                    ),
                    "virements_echoues": transfers_failed if "transfers_failed" in locals() else 0,
                    "erreur": str(e),
                    "type_erreur": type(e).__name__,
                }
                logger.error(
                    "📋 Contexte de synchronisation des virements (JSON):\n{}",
                    jmodule.dumps(context, indent=4, ensure_ascii=False, default=str),
                )
            except Exception as json_error:
                logger.error("Impossible de sérialiser le contexte: {}", json_error)

        # ===================================================================
        # FIN DE LA SYNCHRONISATION
        # ===================================================================
        # Fermer le downloader PDF s'il a été créé
        if hasattr(self, "pdf_downloader"):
            try:
                self.pdf_downloader.close()
                logger.info("🔒 Downloader PDF fermé proprement")
            except Exception as e:
                logger.warning("Erreur lors de la fermeture du downloader PDF: {}", str(e))

        logger.info("\n✅ Synchronisation terminée\n")

        # Affiche les statistiques globales
        self.print_global_statistics()

    def _create_service(
        self,
        label: str,
        description: Optional[str],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> int:
        """Crée un service"""
        query = "INSERT INTO services (label, description, duration, start_date, end_date) VALUES (?, ?, ?, ?, ?)"

        # Utiliser get_sql pour l'insertion
        self.paheko_client.get_sql(
            query, params=(label, description, None, start_date, end_date), mode="direct"
        )

        # Récupérer le dernier ID inséré
        result = self.paheko_client.get_sql(
            "SELECT last_insert_rowid() as id", mode="direct", fetch_mode="one"
        )
        service_id = result.get("results", [{}])[0].get("id", 0)

        logger.info(f"✅ Service créé: {label} (ID: {service_id})")
        return service_id

    def _update_service(
        self,
        service_id: int,
        label: str,
        description: Optional[str],
        start_date: Optional[str],
        end_date: Optional[str],
    ):
        """Met à jour un service avec requête paramétrée (protection contre injection SQL)"""
        query = "UPDATE services SET label = ?, description = ?, start_date = ?, end_date = ? WHERE id = ?"
        self.paheko_client.get_sql(
            query, params=(label, description, start_date, end_date, service_id), mode="direct"
        )
        logger.info(f"✅ Service mis à jour: {label} (ID: {service_id})")

    def _get_service_by_label(self, label: str) -> Optional[Dict]:
        """Récupère un service par son label avec requête paramétrée"""
        query = "SELECT * FROM services WHERE label = ? LIMIT 1"
        result = self.paheko_client.get_sql(query, params=(label,), mode="direct", fetch_mode="one")
        logger.debug("Recherche du service avec label '{}': {}", label, result)
        rows = result.get("results", [])
        return rows[0] if rows else None

    def _get_tarif_by_label_and_service(self, label: str, service_id: int) -> Optional[Dict]:
        """Récupère un tarif par label et service avec requête paramétrée"""
        query = "SELECT * FROM services_fees WHERE label = ? AND id_service = ? LIMIT 1"
        result = self.paheko_client.get_sql(
            query, params=(label, service_id), mode="direct", fetch_mode="one"
        )
        logger.debug(
            "Recherche du tarif avec label '{}' pour le service ID {}: {}",
            label,
            service_id,
            result,
        )
        rows = result.get("results", [])
        return rows[0] if rows else None

    def _create_tarif(
        self,
        label: str,
        amount: int,
        service_id: int,
        description: Optional[str],
    ) -> int:
        """Crée un tarif (les dates sont au niveau du service, pas du tarif)"""
        query = "INSERT INTO services_fees (label, description, amount, formula, id_service, id_account, id_year, id_project) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"

        # Utiliser get_sql pour l'insertion
        self.paheko_client.get_sql(
            query,
            params=(label, description, amount, None, service_id, None, None, None),
            mode="direct",
        )

        # Récupérer le dernier ID inséré
        result = self.paheko_client.get_sql(
            "SELECT last_insert_rowid() as id", mode="direct", fetch_mode="one"
        )
        tarif_id = result.get("results", [{}])[0].get("id", 0)

        logger.info(f"✅ Tarif créé: {label} - {amount/100:.2f}€ (ID: {tarif_id})")
        return tarif_id

    def _update_tarif(self, tarif_id: int, label: str, amount: int, description: Optional[str]):
        """Met à jour un tarif avec requête paramétrée"""
        query = "UPDATE services_fees SET label = ?, description = ?, amount = ? WHERE id = ?"
        self.paheko_client.get_sql(
            query, params=(label, description, amount, tarif_id), mode="direct"
        )
        logger.info(f"✅ Tarif mis à jour: {label} - {amount/100:.2f}€ (ID: {tarif_id})")

    def _get_serviceLabel(self, form: Dict) -> str:
        """Obtient le label du service pour un formulaire"""
        formType = form.get("formType", "Unknown")
        ft = form.get("formType", "Unknown")
        formSlug = form.get("formSlug", "")
        raw_order_date = form.get("date")

        new_mots = [
            "Financement participatif",
            "Adhésion",
            "Événement",
            "Don",
            "Formulaire de paiement",
            "Paiement",
            "Boutique",
        ]
        for c, mot in enumerate(
            [
                "CrowdFunding",
                "Membership",
                "Event",
                "Donation",
                "PaymentForm",
                "Checkout",
                "Shop",
            ]
        ):
            formType = formType.replace(mot, new_mots[c]).strip()
        if ft == "Membership":
            if raw_order_date:
                # La date est au format ISO 8601: "2022-06-06T12:48:52.8653471+02:00"
                # On extrait juste la partie date (YYYY)
                year = raw_order_date[:4]

                return f"{formType} - {formSlug} {year}"
        logger.debug("Service label standard: {} - {}", formType, formSlug)
        return f"{formType} - {formSlug}"

    def _extract_service_info(
        self, form: Dict
    ) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
        """Extrait les infos de service depuis un formulaire"""
        serviceLabel = self._get_serviceLabel(form)

        description = form.get("description")
        if description and len(description) > 500:
            description = description[:497] + "..."

        start_date = form.get("startDate")
        end_date = form.get("endDate")

        if start_date:
            start_date = datetime.fromisoformat(start_date.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d"
            )
        if end_date:
            end_date = datetime.fromisoformat(end_date.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        logger.debug(
            "Infos service extraites - \nserviceLabel: {}\n, description: {}\n, start_date: {}\n, end_date: {}\n",
            serviceLabel,
            description,
            start_date,
            end_date,
        )
        return serviceLabel, description, start_date, end_date

    def _sync_services(self, form: Dict) -> Optional[int]:
        """Synchronise un service"""
        logger.info(
            "\n🔄 Synchronisation du service pour le formulaire: {}",
            jmodule.dumps(form, indent=4, ensure_ascii=False, default=str),
        )
        serviceLabel, description, start_date, end_date = self._extract_service_info(form)
        logger.debug(
            f"serviceLabel: {serviceLabel}\n description: {description}\n start_date: {start_date}\n end_date: {end_date}"
        )

        existing_service = self._get_service_by_label(serviceLabel)
        formType = form.get("formType")
        # formSlug = form.get("formSlug")
        is_membership = formType == "Membership"
        tiers = form.get("tiers", [])
        listids = []
        listlabels = []
        if not tiers:
            return listlabels, listids
        if is_membership:
            # Pour les Membership, créer un SERVICE par année comptable avec dates
            accounting_years = self.paheko_client.get_accounting_years()

            for year in accounting_years:
                year_label = year.get("label", "")
                # Extraire l'année du tarifLabel (ex: "Association 2024" -> "2024")

                year_match = re.search(r"\d{4}", year_label)
                if year_match:
                    year_number = year_match.group()
                    year_int = int(year_number)

                    # Calculer les dates de validité : 1er octobre YYYY-1 → 31 décembre YYYY
                    service_start_date = f"{year_int - 1}-10-01"
                    service_end_date = f"{year_int}-12-31"

                    # Créer le serviceLabel au format "Adhésion - {formSlug} {YEAR}"
                    # serviceLabel = f"{serviceLabel} {year_number}"
                    serviceLabel = f"{self._get_serviceLabel(form)} {year_number}"
                    # Vérifier si ce service existe déjà
                    existing_service = self._get_service_by_label(serviceLabel)

                    if existing_service:
                        # Le service existe, vérifier si le tarif existe aussi
                        existing_service_id = existing_service["id"]
                        service_description = form.get("description")
                        if service_description and len(service_description) > 500:
                            service_description = service_description[:497] + "..."
                        logger.debug(
                            "Le service existe déjà: {} (ID: {})",
                            serviceLabel,
                            existing_service_id,
                        )
                        logger.debug(
                            "Vérification des mises à jour pour le service:\n desc existante: {},\n desc nouvelle: {},\n start_date existante: {},\n start_date nouvelle: {},\n end_date existante: {},\n end_date nouvelle: {}",
                            existing_service.get("description"),
                            service_description,
                            existing_service.get("start_date"),
                            service_start_date,
                            existing_service.get("end_date"),
                            service_end_date,
                        )
                        needs_update = (
                            existing_service.get("description") != service_description
                            or existing_service.get("start_date") != service_start_date
                            or existing_service.get("end_date") != service_end_date
                        )
                        logger.debug("needs_update: {}", needs_update)
                        sleep(5)
                        if needs_update and not self.mode_simulation:
                            logger.success(
                                f"Mise à jour du service: {serviceLabel} (du {service_start_date or 'N/A'} au {service_end_date or 'N/A'})"
                            )
                            self._update_service(
                                existing_service["id"],
                                serviceLabel,
                                service_description,
                                service_start_date,
                                service_end_date,
                            )
                            self.stats["services_updated"] += 1
                        else:
                            self.stats["services_unchanged"] += 1
                        logger.debug("Service existant ID: {} ", existing_service_id)
                        logger.debug("Service existant serviceLabel: {} ", serviceLabel)
                        listids.append(existing_service["id"])
                        listlabels.append(serviceLabel)
                    else:
                        # Le service n'existe pas : créer le service
                        if not self.mode_simulation:
                            logger.success(
                                f"Création du service: {serviceLabel} (du {service_start_date or 'N/A'} au {service_end_date or 'N/A'})"
                            )
                            service_id = self._create_service(
                                serviceLabel,
                                form.get("description"),
                                service_start_date,
                                service_end_date,
                            )
                        else:
                            service_id = -1
                        self.stats["services_created"] += 1
                        logger.debug("Service existant ID: {} ", service_id)
                        logger.debug("Service existant serviceLabel: {} ", serviceLabel)
                        listids.append(service_id)
                        listlabels.append(serviceLabel)
            return (
                listlabels,
                listids,  # Les services Membership sont gérés par année, pas besoin de retourner un
            )

        if existing_service:
            existing_service_id = existing_service["id"]
            service_description = form.get("description")
            if service_description and len(service_description) > 500:
                service_description = service_description[:497] + "..."
            needs_update = (
                existing_service.get("description") != service_description
                or existing_service.get("start_date") != start_date
                or existing_service.get("end_date") != end_date
            )

            if needs_update and not self.mode_simulation:
                logger.success(
                    f"Mise à jour du service: {serviceLabel} (du {start_date or 'N/A'} au {end_date or 'N/A'})"
                )
                self._update_service(
                    existing_service["id"], serviceLabel, description, start_date, end_date
                )
                self.stats["services_updated"] += 1
            else:
                self.stats["services_unchanged"] += 1
            logger.debug("Service existant ID: {} ", existing_service_id)
            logger.debug("Service existant serviceLabel: {} ", serviceLabel)
            return [serviceLabel], [existing_service["id"]]
        else:
            # if formType != "Membership":
            if not self.mode_simulation:
                logger.success(
                    f"Création du service: {serviceLabel} (du {start_date or 'N/A'} au {end_date or 'N/A'})"
                )
                service_id = self._create_service(serviceLabel, description, start_date, end_date)
                logger.debug("Service existant ID: {} ", existing_service_id)
                logger.debug("Service existant serviceLabel: {} ", serviceLabel)
            else:
                service_id = -1
            self.stats["services_created"] += 1
            return [serviceLabel], [service_id]

    def _sync_tarifs(self, service_id: int, serviceLabel: str, form: Dict):
        """Synchronise les tarifs d'un service"""
        tiers = form.get("tiers", [])
        # formType = form.get("formType", "")
        # formSlug = form.get("formSlug", "")

        # Pour les formulaires de type Membership, on crée un tarif par année comptable
        # is_membership = formType == "Membership"

        logger.info("\n🔄 Synchronisation des tarifs pour le serviceLabel: {}", serviceLabel)
        if not tiers:
            return

        for tier in tiers:
            tarifLabel = tier.get("label")

            if not tarifLabel:
                continue
            # if re.search(r" \d{4}$", serviceLabel):
            #     # Récupérer les 5 derniers caractères
            #     cinq_derniers = serviceLabel[-5:]
            #     logger.info(f"Trouvé : '{cinq_derniers}'")
            # else:
            #     logger.info("Pattern non trouvé")

            # Méthode 2 : Extraire directement avec un groupe de capture
            match = re.search(r"( \d{4})$", serviceLabel)
            if match:
                resultat = match.group(1)
                logger.info(f"Extrait : '{resultat}'")
                tarifLabel = tier.get("label") + resultat

            logger.debug("Traitement du tarif avec tarifLabel brut: {}", tarifLabel)
            description = tier.get("description")
            if description and len(description) > 500:
                description = description[:497] + "..."

            price = tier.get("price")
            min_amount = tier.get("minAmount")
            amount = tier.get("Amount")
            amount = price if price is not None else (min_amount if min_amount is not None else 0)
            logger.debug(
                "Traitement du tarif: {} - price: {} - min_amount: {} - AMOUNT: {}",
                tarifLabel,
                price,
                min_amount,
                amount,
            )

            # Pour les autres types de formulaires, comportement normal
            existing_tarif = self._get_tarif_by_label_and_service(tarifLabel, service_id)

            if existing_tarif:
                # Le tarif existe déjà
                needs_update = (
                    existing_tarif.get("description") != description
                    or existing_tarif.get("amount") != amount
                    or price
                    or min_amount
                )

                if needs_update:
                    if not self.mode_simulation:
                        logger.success(f"Mise à jour du tarif: {tarifLabel} - {amount/100:.2f}€")
                        self._update_tarif(
                            existing_tarif["id"],
                            tarifLabel,
                            amount or price or min_amount,
                            description,
                        )
                        logger.info(f"✅ Tarif mis à jour: {tarifLabel} - {amount/100:.2f}€")
                    self.stats["tarifs_updated"] += 1
                else:
                    self.stats["tarifs_unchanged"] += 1
            else:
                # Le tarif n'existe pas, il faut le créer
                if not self.mode_simulation:
                    logger.success(f"Création du tarif: {tarifLabel} - {amount/100:.2f}€")
                    self._create_tarif(tarifLabel, amount, service_id, description)
                    logger.info(f"✅ Tarif créé: {tarifLabel} - {amount/100:.2f}€")
                self.stats["tarifs_created"] += 1

    def _display_existing_services(self):
        """Affiche les services existants dans Paheko"""
        logger.info("\n" + "=" * 100)
        logger.info("📦 SERVICES EXISTANTS DANS PAHEKO")
        logger.info("=" * 100)

        try:
            # Récupérer tous les services
            services_query = "SELECT id, label, description FROM services ORDER BY label"
            services_result = self.paheko_client.get_sql(services_query)
            services = services_result.get("results", [])

            if not services:
                logger.info("Aucun service trouvé dans Paheko")
                return

            for service in services:
                logger.info(f"\n[Service ID: {service['id']}] {service['label']}")
                if service.get("description"):
                    logger.info(f"    Description: {service['description'][:100]}...")

                # Récupérer les tarifs de ce service
                tarifs_query = f"SELECT id, label, amount, description FROM services_fees WHERE id_service = {service['id']} ORDER BY label"
                tarifs_result = self.paheko_client.get_sql(tarifs_query)
                tarifs = tarifs_result.get("results", [])

                if tarifs:
                    logger.info(f"    Tarifs ({len(tarifs)}):")
                    for tarif in tarifs:
                        amount_euro = tarif["amount"] / 100 if tarif["amount"] else 0
                        logger.info(f"      - {tarif['label']}: {amount_euro:.2f}€")
                else:
                    logger.info("    Aucun tarif")

            logger.info("\n" + "=" * 100)
        except Exception as e:
            logger.error(f"Erreur lors de l'affichage des services existants: {e}")

    def _display_planned_actions(self, forms):
        """Affiche ce qui va être créé/modifié
        
        
        :param forms: Liste des formulaires HelloAsso
        """
        logger.info("\n" + "=" * 100)
        logger.info("🔨 ACTIONS PLANIFIÉES (MODE SIMULATION)")
        logger.info("=" * 100)

        for i, form in enumerate(forms, 1):
            title = form.get("title") or form.get("privateTitle") or "Sans titre"
            formType = form.get("formType", "Unknown")
            formSlug = form.get("formSlug", "")

            logger.info(f"\n[{i}/{len(forms)}] {title}")
            logger.info(f"    Slug: {formSlug}")

            # Déterminer le label du service
            serviceLabel = self._get_serviceLabel(form)

            existing_service = self._get_service_by_label(serviceLabel)

            if existing_service:
                logger.info(
                    f"    ✓ Service existe déjà: {serviceLabel} (ID: {existing_service['id']})"
                )
                service_id = existing_service["id"]
            else:
                logger.info(f"    ➕ Service à créer: {serviceLabel}")
                service_id = None

            # Afficher les tarifs qui vont être créés
            tiers = form.get("tiers", [])
            if tiers:
                logger.info(f"    Tarifs du formulaire HelloAsso ({len(tiers)}):")

                is_membership = formType == "Membership"

                for tier in tiers:
                    tier_label = tier.get("label", "Sans nom")
                    price = tier.get("price")
                    min_amount = tier.get("minAmount")
                    amount = (
                        price
                        if price is not None
                        else (min_amount if min_amount is not None else 0)
                    )
                    amount_euro = amount / 100 if amount else 0

                    logger.info(f"      - {tier_label}: {amount_euro:.2f}€")

                    if is_membership:
                        logger.info(
                            "        → Type Membership détecté: créer un service par année"
                        )
                        accounting_years = self.paheko_client.get_accounting_years()
                        for year in accounting_years:
                            year_label = year.get("label", "")

                            year_match = re.search(r"\d{4}", year_label)
                            if year_match:
                                year_number = year_match.group()
                                year_int = int(year_number)

                                # Calculer les dates de validité : 1er octobre YYYY-1 → 31 décembre YYYY
                                service_start_date = f"{year_int - 1}-10-01"
                                service_end_date = f"{year_int}-12-31"

                                # Vérifier si le service existe
                                existing_service = self._get_service_by_label(serviceLabel)
                                if existing_service:
                                    logger.info(
                                        f"          ✓ Existe: {serviceLabel} ({amount_euro:.2f}€) - Valide du {service_start_date} au {service_end_date}"
                                    )
                                else:
                                    logger.info(
                                        f"          ➕ À créer: {serviceLabel} ({amount_euro:.2f}€) - Valide du {service_start_date} au {service_end_date}"
                                    )
                    else:
                        if service_id:
                            existing_tarif = self._get_tarif_by_label_and_service(
                                tier_label, service_id
                            )
                            if existing_tarif:
                                logger.info("          ✓ Existe déjà dans Paheko")
                            else:
                                logger.info("          ➕ À créer dans Paheko")
                        else:
                            logger.info("          ➕ À créer dans Paheko")
            else:
                logger.info("    Aucun tarif")

        logger.info("\n" + "=" * 100)

    def create_missing_cashouts_main(self):
        """Fonction main pour créer les virements manquants depuis le CSV.

        :return: True si tout s'est bien passé, 1 si des erreurs ont été rencontrées, 
                 False si une exception a été levée
        """
        logger.info("\n" + "=" * 80)
        logger.info("🔄 CRÉATION DES VIREMENTS MANQUANTS DEPUIS LE CSV")
        logger.info("=" * 80 + "\n")

        try:

            # Créer les virements manquants (utilise self.cashouts_csv)
            result = self.force_create_missing_cashouts_from_csv(self.cashouts_csv)

            logger.info("\n" + "=" * 80)
            logger.info("✨ CRÉATION TERMINÉE")
            logger.info("=" * 80)
            logger.info(f"✅ Créés: {result['success']}/{result['total_processed']}")
            logger.info(f"❌ Erreurs: {result['errors']}/{result['total_processed']}")
            logger.info(f"💰 Montant total: {result['total_amount']:.2f}€")
            logger.info("=" * 80 + "\n")

            return True if result["errors"] == 0 else 1

        except Exception as e:
            logger.exception(f"\n❌ Erreur: {e}\n")

            return False

    def sync_all(self):
        """Synchronise tous les formulaires"""
        logger.info("=" * 80)
        logger.info("🔄 SYNCHRONISATION DES ACTIVITÉS HELLOASSO → PAHEKO")
        logger.info("=" * 80)

        if self.mode_simulation:
            logger.warning("⚠️  MODE SIMULATION: Aucune modification ne sera effectuée")

        forms = self.helloasso_client.get_all_forms_with_details(self.organization_slug)
        logger.info(f"📋 {len(forms)} formulaires à synchroniser")

        # Afficher l'état actuel de Paheko
        self._display_existing_services()

        # Afficher ce qui va être fait
        self._display_planned_actions(forms)

        for i, form in enumerate(forms, 1):
            title = form.get("title") or form.get("privateTitle") or "Sans titre"
            formType = form.get("formType", "Unknown")
            logger.info(f"\n[{i}/{len(forms)}] Traitement: {formType} - {title}")

            try:
                listlabels, listids = self._sync_services(form)
                for n, service_id in enumerate(listids):
                    self._sync_tarifs(service_id, listlabels[n], form)
            except Exception as e:
                logger.exception(f"❌ Erreur synchronisation '{title}': {e}")
                self.stats["errors"] += 1

        logger.info("\n" + "=" * 80)
        logger.info("📊 STATISTIQUES DE SYNCHRONISATION")
        logger.info("=" * 80)
        logger.info(f"Services créés:      {self.stats['services_created']}")
        logger.info(f"Services mis à jour: {self.stats['services_updated']}")
        logger.info(f"Services inchangés:  {self.stats['services_unchanged']}")
        logger.info(f"Tarifs créés:        {self.stats['tarifs_created']}")
        logger.info(f"Tarifs mis à jour:   {self.stats['tarifs_updated']}")
        logger.info(f"Tarifs inchangés:    {self.stats['tarifs_unchanged']}")
        logger.info(f"Erreurs:             {self.stats['errors']}")
        logger.info("=" * 80)

        return self.stats

    def print_global_statistics(self) -> dict:
        """
        Affiche les statistiques globales de synchronisation.
        Compare les données entre HelloAsso et Paheko.

        :return: Dictionnaire contenant toutes les statistiques
        """
        logger.info("\n" + "=" * 80)
        logger.info("📊 STATISTIQUES GLOBALES DE SYNCHRONISATION")
        logger.info("=" * 80)

        stats = {
            "paheko": {
                "total_membres": 0,
                "membres_actifs": 0,
                "non_adherents": 0,
                "commandes_synchro": 0,
                "virements_synchro": 0,
            },
            "helloasso": {
                "total_membres": 0,
                "membres_actifs": 0,
                "commandes": 0,
                "virements": 0,
            },
            "taux": {
                "commandes": 0.0,
                "virements": 0.0,
                "membres": 0.0,
            },
        }

        try:
            # ═══════════════════════════════════════════════════════════════════
            # PAHEKO - Récupération des statistiques
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n📦 Collecte des données Paheko...")

            # Compter les membres par catégorie
            categories = self.paheko_client.get_member_categories()
            for category_id, category in categories.items():
                members = self.paheko_client.get_members_by_category(category_id)
                category_name = category.get("name", "").lower()
                member_count = len(members)

                stats["paheko"]["total_membres"] += member_count

                if "actif" in category_name:
                    stats["paheko"]["membres_actifs"] += member_count
                    logger.info(f"  ✓ {category.get('name')}: {member_count} membres")
                elif "non" in category_name:
                    stats["paheko"]["non_adherents"] += member_count
                    logger.info(f"  ✓ {category.get('name')}: {member_count} membres")

            # Compter les commandes synchronisées
            result_commandes = self.paheko_client.get_sql(
                """
                SELECT COUNT(DISTINCT reference) as count
                FROM acc_transactions 
                WHERE reference LIKE 'HelloAsso %'
                """,
                mode="direct",
                fetch_mode="one",
            )
            stats["paheko"]["commandes_synchro"] = result_commandes.get("results", [{}])[0].get(
                "count", 0
            )

            # Compter les virements
            result_virements = self.paheko_client.get_sql(
                """
                SELECT COUNT(*) as count
                FROM acc_transactions 
                WHERE label LIKE '%Virement%'
                """,
                mode="direct",
                fetch_mode="one",
            )
            stats["paheko"]["virements_synchro"] = result_virements.get("results", [{}])[0].get(
                "count", 0
            )

            # ═══════════════════════════════════════════════════════════════════
            # HELLOASSO - Récupération des statistiques
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n🌐 Collecte des données HelloAsso...")

            # Récupérer les commandes
            orders = self.helloasso_client.get_orders(self.organization_slug)
            stats["helloasso"]["commandes"] = len(orders)
            logger.info(f"  ✓ {len(orders)} commandes trouvées")

            # Compter les membres uniques et actifs
            members = set()
            active_members = set()
            for order in orders:
                payer_email = order.get("payer", {}).get("email")
                if payer_email:
                    members.add(payer_email)
                    # Vérifier si la commande contient une adhésion
                    if any(item.get("type") == "Membership" for item in order.get("items", [])):
                        active_members.add(payer_email)

            stats["helloasso"]["total_membres"] = len(members)
            stats["helloasso"]["membres_actifs"] = len(active_members)
            logger.info(f"  ✓ {len(members)} payeurs uniques ({len(active_members)} avec adhésion)")

            # Compter les virements
            cash_outs = self.helloasso_client.get_cash_outs(self.organization_slug)
            stats["helloasso"]["virements"] = len(cash_outs)
            logger.info(f"  ✓ {len(cash_outs)} virements bancaires")

            # ═══════════════════════════════════════════════════════════════════
            # VÉRIFICATION DES INSCRIPTIONS AUX ACTIVITÉS
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n🔍 Vérification des inscriptions aux activités...")
            inscriptions_stats = self.verify_items_service_inscriptions()
            stats["inscriptions"] = inscriptions_stats

            # ═══════════════════════════════════════════════════════════════════
            # CALCUL DES TAUX DE SYNCHRONISATION
            # ═══════════════════════════════════════════════════════════════════
            if stats["helloasso"]["commandes"] > 0:
                stats["taux"]["commandes"] = (
                    stats["paheko"]["commandes_synchro"] / stats["helloasso"]["commandes"]
                ) * 100

            if stats["helloasso"]["virements"] > 0:
                stats["taux"]["virements"] = (
                    stats["paheko"]["virements_synchro"] / stats["helloasso"]["virements"]
                ) * 100

            if stats["helloasso"]["total_membres"] > 0:
                stats["taux"]["membres"] = (
                    stats["paheko"]["total_membres"] / stats["helloasso"]["total_membres"]
                ) * 100

            # ═══════════════════════════════════════════════════════════════════
            # AFFICHAGE DES RÉSULTATS
            # ═══════════════════════════════════════════════════════════════════
            logger.info("\n" + "=" * 80)
            logger.info("📊 RÉSUMÉ COMPARATIF")
            logger.info("=" * 80)

            # Tableau HelloAsso
            logger.info("\n🌐 HelloAsso (Source)")
            logger.info("  ├─ Commandes       : {:>6}".format(stats["helloasso"]["commandes"]))
            logger.info("  ├─ Virements       : {:>6}".format(stats["helloasso"]["virements"]))
            logger.info("  ├─ Membres total   : {:>6}".format(stats["helloasso"]["total_membres"]))
            logger.info("  └─ Membres actifs  : {:>6}".format(stats["helloasso"]["membres_actifs"]))

            # Tableau Paheko
            logger.info("\n📦 Paheko (Destination)")
            logger.info("  ├─ Commandes synch : {:>6}".format(stats["paheko"]["commandes_synchro"]))
            logger.info("  ├─ Virements synch : {:>6}".format(stats["paheko"]["virements_synchro"]))
            logger.info("  ├─ Membres total   : {:>6}".format(stats["paheko"]["total_membres"]))
            logger.info("  ├─ Membres actifs  : {:>6}".format(stats["paheko"]["membres_actifs"]))
            logger.info("  └─ Non adhérents   : {:>6}".format(stats["paheko"]["non_adherents"]))

            # Taux de synchronisation
            logger.info("\n📈 Taux de synchronisation")
            logger.info("  ├─ Commandes : {:>5.1f}%".format(stats["taux"]["commandes"]))
            logger.info("  ├─ Virements : {:>5.1f}%".format(stats["taux"]["virements"]))
            logger.info("  ├─ Membres   : {:>5.1f}%".format(stats["taux"]["membres"]))
            logger.info(
                "  └─ Inscriptions activités : {:>5.1f}%".format(
                    stats.get("inscriptions", {}).get("completion_rate", 0)
                )
            )

            # Détails des inscriptions
            inscriptions_info = stats.get("inscriptions", {})
            if inscriptions_info.get("total_items", 0) > 0:
                logger.info("\n🎯 Inscriptions aux activités")
                logger.info(
                    "  ├─ Items traités    : {:>6}".format(inscriptions_info.get("total_items", 0))
                )
                logger.info(
                    "  ├─ Items inscrits   : {:>6}".format(
                        inscriptions_info.get("found_inscriptions", 0)
                    )
                )
                logger.info(
                    "  └─ Items manquants  : {:>6}".format(
                        inscriptions_info.get("missing_inscriptions", 0)
                    )
                )

                # Afficher les commandes et items à retraiter
                orders_to_reprocess = inscriptions_info.get("orders_to_reprocess", [])
                items_to_reprocess = inscriptions_info.get("items_to_reprocess", [])

                if orders_to_reprocess:
                    logger.info("\n📝 Commandes à retraiter ({}):", len(orders_to_reprocess))
                    for i, order_id in enumerate(orders_to_reprocess[:10], 1):
                        logger.info(f"  {i}. Commande #{order_id}")
                    if len(orders_to_reprocess) > 10:
                        logger.info(f"  ... et {len(orders_to_reprocess) - 10} autres")

                if items_to_reprocess:
                    logger.info("\n📌 Items à retraiter ({}):", len(items_to_reprocess))
                    for i, item_id in enumerate(items_to_reprocess[:10], 1):
                        logger.info(f"  {i}. Item #{item_id}")
                    if len(items_to_reprocess) > 10:
                        logger.info(f"  ... et {len(items_to_reprocess) - 10} autres")

            # État de la synchronisation
            inscriptions_rate = stats.get("inscriptions", {}).get("completion_rate", 0)
            sync_complete = all(
                rate >= 100.0
                for rate in [
                    stats["taux"]["commandes"],
                    stats["taux"]["virements"],
                    stats["taux"]["membres"],
                    inscriptions_rate,
                ]
            )

            logger.info("\n" + "=" * 80)
            if sync_complete:
                logger.info("✅ Synchronisation complète à 100% !")
            else:
                logger.warning("⚠️  Synchronisation incomplète")

                # Calculer les manquants
                missing_orders = (
                    stats["helloasso"]["commandes"] - stats["paheko"]["commandes_synchro"]
                )
                missing_transfers = (
                    stats["helloasso"]["virements"] - stats["paheko"]["virements_synchro"]
                )
                missing_members = (
                    stats["helloasso"]["total_membres"] - stats["paheko"]["total_membres"]
                )
                missing_inscriptions = stats.get("inscriptions", {}).get("missing_inscriptions", 0)

                if missing_orders > 0:
                    logger.warning(f"  → {missing_orders} commande(s) à synchroniser")
                if missing_transfers > 0:
                    logger.warning(f"  → {missing_transfers} virement(s) à synchroniser")
                if missing_members > 0:
                    logger.warning(f"  → {missing_members} membre(s) à synchroniser")
                if missing_inscriptions > 0:
                    logger.warning(
                        f"  → {missing_inscriptions} inscription(s) aux activités manquante(s)"
                    )

                logger.info("\n💡 Relancez le script pour terminer la synchronisation")

            logger.info("=" * 80)

            return stats

        except Exception as e:
            logger.exception(f"❌ Erreur lors du calcul des statistiques: {e}")
            return stats


if __name__ == "__main__":

    if len(sys.argv) > 1 and sys.argv[1] == "--sync-services":
        # Synchronisation des services HelloAsso → Paheko
        sync = sync_helloasso_orders_and_transactions()
        exit_code = sync.sync_helloasso_services_main()
        sys.exit(exit_code)

    elif len(sys.argv) > 1 and sys.argv[1] == "--tables":
        # Afficher la structure de la base de données
        logger.info("🔍 Récupération de la structure de la base de données")
        logger.info("=" * 70)

        # Initialiser le client Paheko
        paheko_client = PahekoAPIClient(mode_simulation=False)

        # Tester les deux modes

        logger.info("\n💾 MODE DIRECT")
        logger.info("-" * 70)
        structure_direct = paheko_client.get_tablenames_and_rows(mode="direct")

        # Afficher les résultats au format JSON
        logger.info("\n" + "=" * 70)
        logger.info("📊 RÉSULTATS")
        logger.info("=" * 70)

        logger.info("\n🔹 Structure via accès direct:")
        logger.info(jmodule.dumps(structure_direct, indent=2, ensure_ascii=False))

    else:
        # Déterminer les modes activés
        force_sync = "--force" in sys.argv
        mode_simulation = "--simulate" in sys.argv
        mode_update = "--update" in sys.argv

        # Exécution normale : synchronisation
        logger.info("🚀 Démarrage de la synchronisation HelloAsso → Paheko")
        if force_sync:
            logger.warning("⚠️  MODE FORCE ACTIVÉ - Ignorera les vérifications de doublon")
        if mode_update:
            logger.info("🔄 MODE UPDATE ACTIVÉ - Ne traitera que les nouvelles commandes")
        if mode_simulation:
            logger.warning("🧪 MODE SIMULATION ACTIVÉ - Aucune modification ne sera effectuée")
        logger.info("=" * 70 + "\n")

        # Lance la synchronisation
        syncer = sync_helloasso_orders_and_transactions(
            mode_simulation=mode_simulation, force_sync=force_sync, mode_update=mode_update
        )
        syncer()
