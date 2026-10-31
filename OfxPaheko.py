#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Script pour importer les fichiers OFX (relevés bancaires) dans Paheko.

RÈGLES DE GESTION:
1. Référence OFX ajoutée aux transactions existantes (HelloAsso XXXXX → HelloAsso XXXXX OFX-YYYYY)
2. Nouvelles transactions non reconnues → Compte d'attente 47 (à régulariser manuellement)
3. Remises de chèques → Compte 5112 (Chèques à encaisser)
4. Changement d'exercice (1er janvier) → Écriture automatique avec compte 772 → 512BPBFC

Comportement par transaction:
1. Déjà importée avec OFX ? → Skip
2. Transaction similaire trouvée ? (montant + date ±3 jours) → Ajoute référence OFX
3. Remise de chèque ? → Crée avec 5112 (débit) → 512BPBFC (crédit)
4. Nouvelle recette ? → Crée avec 512BPBFC (débit) → 47 (crédit)
5. Nouvelle dépense ? → Crée avec 47 (débit) → 512BPBFC (crédit)

Régularisation annuelle (1er janvier):
   - 772 (Produits exercices antérieurs) → 512BPBFC
   - Montant à ajuster manuellement avec solde du compte 47

Flux de travail:
1. HelloassoPaheko.py crée les transactions HelloAsso
2. OfxPaheko.py enrichit avec OFX et crée les transactions bancaires non reconnues (compte 47)
3. Régularisation manuelle du compte 47 si nécessaire

Usage:
    python OfxPaheko.py [--simulation] [--file chemin.ofx]
"""

import os
import sys
import re
import tomllib
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from loguru import logger
from ofxtools.Parser import OFXTree


# Configuration du logger
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="DEBUG",
)
logger.add(
    "ofx_import.log",
    rotation="10 MB",
    retention="30 days",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    level="INFO",
)


class OfxPahekoImporter:
    """
    Classe pour importer les fichiers OFX dans Paheko.
    Analyse les fichiers OFX et crée les écritures comptables correspondantes.
    """

    def __init__(self, ofx_directory: str = "./ofx", mode_simulation: bool = False):
        """
        Initialise l'importeur OFX.

        :param ofx_directory: Répertoire contenant les fichiers OFX
        :param mode_simulation: Si True, aucune écriture n'est créée (mode test)
        """
        self.ofx_directory = Path(ofx_directory)
        self.mode_simulation = mode_simulation

        # Charger la configuration
        self.config_path = "config.toml"
        self._load_config()

        # Importer le client Paheko depuis HelloassoPaheko.py
        from HelloassoPaheko import PahekoAPIClient

        self.paheko_client = PahekoAPIClient(mode_simulation=mode_simulation)

        # Statistiques
        self.stats = {
            "files_processed": 0,
            "files_failed": 0,
            "transactions_imported": 0,
            "transactions_skipped": 0,
            "transactions_failed": 0,
            "total_debit": 0.0,
            "total_credit": 0.0,
        }

        logger.info("=" * 80)
        logger.info("💳 IMPORTEUR OFX → PAHEKO")
        logger.info("=" * 80)
        logger.info(f"Répertoire OFX: {self.ofx_directory}")
        logger.info(f"Mode simulation: {self.mode_simulation}")
        logger.info("=" * 80)

    def _load_config(self):
        """Charge la configuration depuis config.toml"""
        try:
            with open(self.config_path, "rb") as f:
                config = tomllib.load(f)

            # Configuration pour l'import OFX
            ofx_config = config.get("ofx", {})
            self.default_account_debit = ofx_config.get(
                "account_debit", "512BPBFC"
            )  # Compte bancaire
            self.default_account_credit = ofx_config.get(
                "account_credit", "706"
            )  # Compte à déterminer
            self.organization_slug = config.get("helloasso", {}).get("org_slug", "transcende")

            # Mapping des libellés vers les comptes comptables
            self.account_mapping = ofx_config.get("account_mapping", {})

            logger.info("Configuration chargée depuis {}", self.config_path)
            logger.debug("Compte bancaire par défaut: {}", self.default_account_debit)
            logger.debug("Mapping de comptes: {} règles", len(self.account_mapping))

        except Exception as e:
            logger.error("Erreur lors du chargement de la configuration: {}", e)
            raise

    def _preprocess_ofx_file(self, file_path: Path) -> Path:
        """
        Prétraite un fichier OFX français pour le rendre compatible :
        - Remplace les virgules par des points dans les montants
        - Calcule et injecte le solde (LEDGERBAL) s'il est vide
        
        Sauvegarde le fichier modifié dans /tmp/
        
        Returns:
            Path vers le fichier OFX modifié
        """
        logger.debug(f"🔧 Prétraitement du fichier: {file_path.name}")
        
        # Lire le fichier
        with open(file_path, "r", encoding="latin-1") as f:
            content = f.read()
        
        # 1. Remplacer les virgules par des points dans les montants
        # Format: <TRNAMT>-80,00 → <TRNAMT>-80.00
        content = re.sub(r"<TRNAMT>([+-]?\d+),(\d+)", r"<TRNAMT>\1.\2", content)
        content = re.sub(r"<BALAMT>([+-]?\d+),(\d+)", r"<BALAMT>\1.\2", content)
        
        # 2. Calculer le solde si LEDGERBAL est vide
        if "<BALAMT></BALAMT>" in content or "<BALAMT>" not in content:
            logger.debug("💰 Calcul du solde (LEDGERBAL manquant ou vide)")
            
            # Extraire toutes les transactions
            balance = 0.0
            for match in re.finditer(r"<TRNAMT>([+-]?\d+\.\d+)", content):
                amount = float(match.group(1))
                balance += amount
                logger.debug(f"  Transaction: {amount:>10.2f}€ → Solde: {balance:>10.2f}€")
            
            logger.info(f"� Solde calculé: {balance:.2f}€")
            
            # Obtenir la date de fin
            dtend_match = re.search(r"<DTEND>(\d{8})", content)
            dtend = dtend_match.group(1) if dtend_match else datetime.now().strftime("%Y%m%d")
            
            # Injecter le LEDGERBAL calculé
            ledgerbal = f"""<LEDGERBAL>
<BALAMT>{balance:.2f}</BALAMT>
<DTASOF>{dtend}</DTASOF>
</LEDGERBAL>"""
            
            if "<LEDGERBAL>" in content:
                # Remplacer le LEDGERBAL vide
                content = re.sub(
                    r"<LEDGERBAL>.*?</LEDGERBAL>",
                    ledgerbal,
                    content,
                    flags=re.DOTALL
                )
            else:
                # Injecter avant </STMTRS>
                content = content.replace("</STMTRS>", f"{ledgerbal}\n</STMTRS>")
        
        # Sauvegarder dans /tmp/
        output_path = Path("/tmp") / file_path.name
        with open(output_path, "w", encoding="latin-1") as f:
            f.write(content)
        
        logger.debug(f"✅ Fichier prétraité sauvegardé: {output_path}")
        return output_path

    def _parse_ofx_file(self, file_path: Path) -> Optional[object]:
        """
        Parse un fichier OFX avec ofxtools.
        Prétraite d'abord le fichier pour corriger le format français.
        
        Returns:
            Object OFX parsé
        """
        logger.info(f"📄 Lecture du fichier: {file_path.name}")
        
        # Prétraitement du fichier
        preprocessed_file = self._preprocess_ofx_file(file_path)
        
        # Parser avec ofxtools (SANS try/except pour voir les erreurs)
        parser = OFXTree()
        parser.parse(preprocessed_file)
        ofx = parser.convert()
        
        logger.debug(f"✅ Fichier parsé avec succès")
        return ofx

    def _get_account_for_transaction(self, transaction) -> Dict[str, str]:
        """
        Détermine les comptes comptables pour une transaction OFX.

        :param transaction: Transaction OFX (ofxtools STMTTRN)
        :return: Dict avec 'debit' et 'credit'
        """
        # ofxtools: transaction.name et transaction.memo
        memo = (transaction.memo or "").upper() if hasattr(transaction, 'memo') else ""
        name = (transaction.name or "").upper() if hasattr(transaction, 'name') else ""
        transaction_text = f"{memo} {name}"

        # Vérifier le mapping de configuration
        for pattern, accounts in self.account_mapping.items():
            if pattern.upper() in transaction_text:
                logger.debug("Mapping trouvé pour '{}': {}", pattern, accounts)
                return accounts

        # Déterminer automatiquement selon le montant
        amount = float(transaction.trnamt)
        if amount > 0:
            # Crédit (entrée d'argent)
            return {
                "debit": self.default_account_debit,  # 512BPBFC (Banque)
                "credit": "706",  # À adapter selon le type de recette
            }
        else:
            # Débit (sortie d'argent)
            return {
                "debit": "6",  # Compte de charges (à affiner)
                "credit": self.default_account_debit,  # 512BPBFC (Banque)
            }

    def _transaction_exists(self, fitid: str, date: datetime) -> bool:
        """
        Vérifie si une transaction existe déjà dans Paheko par référence OFX.

        :param fitid: ID unique de la transaction (FITID dans OFX)
        :param date: Date de la transaction
        :return: True si la transaction existe déjà
        """
        reference = f"OFX-{fitid}"
        return self.paheko_client.transaction_exists(reference)
    
    def _find_similar_transaction(self, amount: float, date: datetime, id_year: int) -> Optional[Dict]:
        """
        Cherche une transaction similaire dans Paheko (même montant, date ±3 jours).
        
        :param amount: Montant de la transaction (peut être négatif)
        :param date: Date de la transaction
        :param id_year: ID de l'année comptable
        :return: Dict avec les infos de la transaction trouvée, ou None
        """
        from datetime import timedelta
        
        # Convertir le montant en centimes (valeur absolue)
        amount_cents = int(abs(amount) * 100)
        
        # Calculer la fenêtre de dates (±3 jours)
        date_min = (date - timedelta(days=3)).strftime("%Y-%m-%d")
        date_max = (date + timedelta(days=3)).strftime("%Y-%m-%d")
        
        # Requête SQL pour trouver une transaction similaire
        query = f"""
            SELECT t.id, t.date, t.label, t.reference, l.debit, l.credit
            FROM acc_transactions t
            JOIN acc_transactions_lines l ON l.id_transaction = t.id
            WHERE t.id_year = {id_year}
            AND t.date BETWEEN '{date_min}' AND '{date_max}'
            AND (l.debit = {amount_cents} OR l.credit = {amount_cents})
            LIMIT 1
        """
        
        response = self.paheko_client.get_sql(query)
        
        if response and response.get('results'):
            return response['results'][0]
        return None

    def _is_year_change(self, date1: datetime, date2: datetime) -> bool:
        """
        Détecte si on change d'exercice comptable entre deux dates.
        Un changement d'exercice se produit quand on passe du 31 décembre au 1er janvier.
        
        :param date1: Première date
        :param date2: Deuxième date
        :return: True si changement d'exercice détecté
        """
        # Vérifier si on passe d'une année à la suivante
        if date2.year > date1.year and date2.month == 1 and date2.day == 1:
            return True
        return False

    def _create_year_change_entry(self, new_year_date: datetime, id_year: int):
        """
        Crée une écriture de régularisation au changement d'exercice.
        Utilise le compte 772 (Produits sur exercices antérieurs).
        
        :param new_year_date: Date du nouvel exercice (1er janvier)
        :param id_year: ID de l'année comptable
        """
        date_str = new_year_date.strftime("%Y-%m-%d")
        reference = f"OFX-REGULARISATION-{new_year_date.year}"
        
        # Vérifier si cette écriture existe déjà
        if self.paheko_client.transaction_exists(reference):
            logger.debug("✓ Écriture de régularisation {} déjà créée", reference)
            return
        
        logger.info("📝 Création écriture de régularisation pour le changement d'exercice {}", new_year_date.year)
        
        # Créer l'écriture avec le compte 772 vers le compte bancaire 512BPBFC
        transaction_data = {
            "id_year": id_year,
            "label": f"Régularisation exercice antérieur {new_year_date.year - 1}",
            "date": date_str,
            "type": "VIREMENT",
            "amount": 0,  # Montant symbolique, à ajuster manuellement
            "debit": "772",  # Produits sur exercices antérieurs
            "credit": self.default_account_debit,  # 512BPBFC
            "reference": reference,
            "notes": f"Écriture de régularisation automatique - Changement d'exercice comptable\nÀ compléter avec le solde du compte 47 de l'exercice précédent",
        }
        
        if not self.mode_simulation:
            logger.success("✅ Écriture de régularisation créée : {}", reference)
            self.paheko_client.create_transaction(transaction_data)
        else:
            logger.warning("[SIMULATION] Écriture de régularisation : {}", reference)

    def _import_transaction(self, transaction, account_from: Dict, id_year: int) -> bool:
        """
        Importe une transaction OFX dans Paheko.

        :param transaction: Transaction OFX (ofxtools STMTTRN)
        :param account_from: Informations du compte bancaire
        :param id_year: ID de l'année comptable Paheko
        :return: True si l'import a réussi
        """
        # PAS de try/except pour voir les erreurs
        
        # ofxtools: transaction.fitid, transaction.dtposted, transaction.trnamt, transaction.name, transaction.memo
        fitid = transaction.fitid
        reference = f"OFX-{fitid}"
        
        # Montant (ofxtools: transaction.trnamt)
        amount = float(transaction.trnamt)
        amount_cents = int(abs(amount) * 100)
        
        # Formater la date (ofxtools retourne un objet datetime)
        date_str = transaction.dtposted.strftime("%Y-%m-%d")
        
        # Préparer le libellé
        label = transaction.memo or transaction.name or "Opération bancaire"

        # 1. Vérifier si transaction existe déjà par référence OFX
        if self._transaction_exists(fitid, transaction.dtposted):
            logger.debug("✓ Transaction {} déjà importée (référence OFX)", fitid)
            self.stats["transactions_skipped"] += 1
            return False
        
        # 2. Chercher une transaction similaire par montant et date (±3 jours)
        similar = self._find_similar_transaction(amount, transaction.dtposted, id_year)
        
        if similar:
            # Transaction existante trouvée
            existing_label = similar.get('label', '') or ''
            existing_reference = similar.get('reference', '') or ''
            
            # Vérifier si le nouveau libellé est plus riche (plus long)
            should_update_label = len(label) > len(existing_label) and label != existing_label
            
            if should_update_label:
                logger.success(
                    "✓ Transaction trouvée avec libellé enrichi: {} | {} → {} | {:.2f}€",
                    similar['date'],
                    existing_label[:40],
                    label[:40],
                    amount
                )
            else:
                logger.success(
                    "✓ Transaction existante trouvée: {} | {} | {:.2f}€ → Ajout référence {}",
                    similar['date'],
                    existing_label[:40],
                    amount,
                    reference
                )
            
            if not self.mode_simulation:
                if should_update_label:
                    # Enrichir le libellé et conserver l'ancien dans les notes
                    self.paheko_client.update_transaction_label_and_reference(
                        similar['id'], 
                        label, 
                        reference,
                        f"Ancien libellé : {existing_label}\nRéférence OFX ajoutée : {reference}"
                    )
                    logger.info("  📝 Libellé enrichi + référence OFX ajoutée")
                else:
                    # Juste ajouter la référence OFX
                    self.paheko_client.update_transaction_reference(similar['id'], reference)
            
            self.stats["transactions_updated"] = self.stats.get("transactions_updated", 0) + 1
            return True

        # 3. Nouvelle transaction - essayer d'abord le mapping automatique
        # Vérifier si c'est une remise de chèque
        is_cheque_deposit = "REMISE" in label.upper() and "CHEQUE" in label.upper()
        
        if is_cheque_deposit:
            # Remise de chèque → 5112 au débit, 512BPBFC au crédit
            transaction_type = "VIREMENT"
            debit_account = "5112"  # Chèques à encaisser
            credit_account = self.default_account_debit  # 512BPBFC
            self.stats["total_debit"] += abs(amount)
            mapping_found = True
        else:
            # Essayer le mapping automatique depuis config.toml
            mapped_accounts = self._get_account_for_transaction(transaction)
            
            # Vérifier si le mapping a trouvé un pattern (pas les valeurs par défaut)
            transaction_text = f"{transaction.memo or ''} {transaction.name or ''}".upper()
            mapping_found = False
            for pattern in self.account_mapping.keys():
                if pattern.upper() in transaction_text:
                    mapping_found = True
                    break
            
            if mapping_found:
                # Mapping trouvé - utiliser les comptes configurés
                debit_account = mapped_accounts["debit"]
                credit_account = mapped_accounts["credit"]
                transaction_type = "VIREMENT" if amount > 0 else "DEPENSE"
                if amount > 0:
                    self.stats["total_credit"] += abs(amount)
                else:
                    self.stats["total_debit"] += abs(amount)
            elif amount > 0:
                # Recette non mappée → 512BPBFC au débit, 47 au crédit
                transaction_type = "RECETTE"
                debit_account = self.default_account_debit  # 512BPBFC
                credit_account = "47"  # Compte d'attente
                self.stats["total_credit"] += abs(amount)
            else:
                # Dépense non mappée → 47 au débit, 512BPBFC au crédit
                transaction_type = "DEPENSE"
                debit_account = "47"  # Compte d'attente
                credit_account = self.default_account_debit  # 512BPBFC
                self.stats["total_debit"] += abs(amount)

        # Créer la transaction dans Paheko
        notes_prefix = "Import OFX automatique" if mapping_found else "Import OFX - À régulariser (compte d'attente 47)"
        notes_text = f"{notes_prefix}\nFITID: {fitid}\nName: {transaction.name or 'N/A'}\nMemo: {transaction.memo or 'N/A'}"
        
        # Format ADVANCED avec lignes d'écriture explicites
        transaction_data = {
            "id_year": id_year,
            "label": label,
            "date": date_str,
            "type": "ADVANCED",
            "lines": [
                {
                    "account": debit_account,
                    "debit": amount,
                    "credit": 0,
                    "label": label,
                    "reference": reference,
                },
                {
                    "account": credit_account,
                    "debit": 0,
                    "credit": amount,
                    "label": label,
                    "reference": reference,
                },
            ],
            "reference": reference,
            "notes": notes_text,
        }

        if self.mode_simulation:
            if mapping_found:
                logger.info(
                    "✅ [SIMULATION] Transaction mappée automatiquement: {} | {} | {:.2f}€ | {} → {}",
                    date_str,
                    label[:40],
                    abs(amount),
                    debit_account,
                    credit_account,
                )
            else:
                logger.warning(
                    "🆕 [SIMULATION] Nouvelle transaction (compte d'attente): {} | {} | {:.2f}€ | {} → {}",
                    date_str,
                    label[:40],
                    abs(amount),
                    debit_account,
                    credit_account,
                )
        else:
            logger.warning(
                "🆕 Nouvelle transaction (compte d'attente): {} | {} | {:.2f}€ | {} → {}",
                date_str,
                label[:40],
                abs(amount),
                debit_account,
                credit_account,
            )
            self.paheko_client.create_transaction(transaction_data)

        self.stats["transactions_imported"] += 1
        return True

    def import_ofx_file(self, file_path: Path) -> Dict:
        """
        Importe un fichier OFX complet.

        :param file_path: Chemin du fichier OFX
        :return: Statistiques d'import pour ce fichier
        """
        logger.info("\n" + "=" * 80)
        logger.info("📁 Traitement du fichier: {}", file_path.name)
        logger.info("=" * 80)

        file_stats = {
            "transactions_total": 0,
            "transactions_imported": 0,
            "transactions_skipped": 0,
            "transactions_failed": 0,
        }

        # Parser le fichier OFX (SANS try/except)
        ofx = self._parse_ofx_file(file_path)

        # ofxtools structure: ofx.statements[0] contient le relevé bancaire
        statement = ofx.statements[0]
        account = statement.account
        
        logger.info("Compte bancaire: {}", account.acctid)
        logger.info("Type de compte: {}", account.accttype)
        logger.info("Devise: {}", statement.curdef)

        # Récupérer les transactions (ofxtools: statement.transactions)
        transactions = statement.transactions
        file_stats["transactions_total"] = len(transactions)

        logger.info("Nombre de transactions: {}", len(transactions))
        logger.info(
            "Période: {} → {}",
            statement.dtstart.strftime("%Y-%m-%d"),
            statement.dtend.strftime("%Y-%m-%d"),
        )

        # Informations du compte
        account_info = {
            "bank_id": account.bankid,
            "account_id": account.acctid,
            "account_type": account.accttype,
        }

        # Importer chaque transaction
        logger.info("\n🔄 Import des transactions...")
        
        previous_transaction_date = None
        previous_id_year = None
        
        for i, transaction in enumerate(transactions, 1):
            logger.debug(f"\n[{i}/{len(transactions)}] Transaction {transaction.fitid}")
            
            current_date = transaction.dtposted
            
            # Déterminer l'année comptable POUR CETTE TRANSACTION
            id_year = self.paheko_client.get_current_year_id(current_date, self.organization_slug)
            
            if not id_year:
                logger.error("❌ Impossible de déterminer l'année comptable pour la transaction du {}", current_date)
                file_stats["transactions_failed"] += 1
                continue
            
            # Logger le changement d'année comptable
            if previous_id_year and id_year != previous_id_year:
                logger.info("\n🔄 Changement d'exercice comptable détecté : ID {} → ID {}", 
                           previous_id_year, id_year)
            
            # Détecter le changement d'exercice (passage du 31 décembre au 1er janvier)
            if previous_transaction_date and self._is_year_change(previous_transaction_date, current_date):
                logger.info("\n� Passage du 31/12 au 01/01 détecté : {} → {}", 
                           previous_transaction_date.strftime("%Y-%m-%d"),
                           current_date.strftime("%Y-%m-%d"))
                
                # Créer l'écriture de régularisation avec le compte 772
                self._create_year_change_entry(current_date, id_year)
            
            previous_transaction_date = current_date
            previous_id_year = id_year

            success = self._import_transaction(transaction, account_info, id_year)

            if success:
                file_stats["transactions_imported"] += 1
            elif self._transaction_exists(transaction.fitid, transaction.dtposted):
                file_stats["transactions_skipped"] += 1
            else:
                file_stats["transactions_failed"] += 1

        self.stats["files_processed"] += 1

        # Résumé du fichier
        logger.info("\n" + "-" * 80)
        logger.info("📊 Résumé du fichier {}", file_path.name)
        logger.info("-" * 80)
        logger.info("Total transactions: {}", file_stats["transactions_total"])
        logger.info("🆕 Nouvelles importées: {}", file_stats["transactions_imported"])
        logger.info("✓ Mises à jour: {}", file_stats.get("transactions_updated", 0))
        logger.info("⏭️  Ignorées (déjà existantes): {}", file_stats["transactions_skipped"])
        logger.info("❌ Échecs: {}", file_stats["transactions_failed"])
        logger.info("-" * 80)

        return file_stats

    def import_all_ofx_files(self) -> Dict:
        """
        Importe tous les fichiers OFX du répertoire.

        :return: Statistiques globales
        """
        if not self.ofx_directory.exists():
            logger.error("❌ Le répertoire {} n'existe pas", self.ofx_directory)
            return self.stats

        # Lister les fichiers OFX
        ofx_files = sorted(self.ofx_directory.glob("*.ofx"))

        if not ofx_files:
            logger.warning("⚠️  Aucun fichier OFX trouvé dans {}", self.ofx_directory)
            return self.stats

        logger.info("\n📋 {} fichier(s) OFX trouvé(s)", len(ofx_files))
        for f in ofx_files:
            logger.info(f"  • {f.name}")

        # Importer chaque fichier
        for ofx_file in ofx_files:
            self.import_ofx_file(ofx_file)

        return self.stats

    def print_statistics(self):
        """Affiche les statistiques globales d'import"""
        logger.info("\n" + "=" * 80)
        logger.info("📊 STATISTIQUES GLOBALES D'IMPORT OFX")
        logger.info("=" * 80)
        logger.info("Fichiers traités: {}", self.stats["files_processed"])
        logger.info("🆕 Nouvelles transactions créées (compte 47): {}", self.stats["transactions_imported"])
        logger.info("✓ Transactions mises à jour (référence OFX ajoutée): {}", self.stats.get("transactions_updated", 0))
        logger.info("⏭️  Transactions ignorées (déjà avec OFX): {}", self.stats["transactions_skipped"])
        logger.info("❌ Transactions échouées: {}", self.stats["transactions_failed"])
        logger.info("")
        logger.info("⚠️  ATTENTION: Les nouvelles transactions sont sur le compte d'attente 47")
        logger.info("   → Régulariser manuellement dans Paheko")
        logger.info("")
        logger.info("💰 Montants:")
        logger.info("  Crédits (entrées): {:.2f}€", self.stats["total_credit"])
        logger.info("  Débits (sorties): {:.2f}€", self.stats["total_debit"])
        logger.info("  Solde net: {:.2f}€", self.stats["total_credit"] - self.stats["total_debit"])
        logger.info("=" * 80)


def main():
    """Point d'entrée principal du script"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Importe les fichiers OFX (relevés bancaires) dans Paheko"
    )
    parser.add_argument(
        "--ofx-dir",
        default="./ofx",
        help="Répertoire contenant les fichiers OFX (défaut: ./ofx)",
    )
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="Mode simulation (aucune écriture créée)",
    )
    parser.add_argument(
        "--file",
        help="Importer un fichier OFX spécifique au lieu de tous les fichiers",
    )

    args = parser.parse_args()

    try:
        # Créer l'importeur
        importer = OfxPahekoImporter(
            ofx_directory=args.ofx_dir,
            mode_simulation=args.simulation,
        )

        # Importer
        if args.file:
            file_path = Path(args.file)
            if not file_path.exists():
                logger.error("❌ Fichier introuvable: {}", file_path)
                return 1
            importer.import_ofx_file(file_path)
        else:
            importer.import_all_ofx_files()

        # Afficher les statistiques
        importer.print_statistics()

        # Code de sortie
        if importer.stats["transactions_failed"] > 0:
            logger.warning(
                "⚠️  Import terminé avec {} erreur(s)", importer.stats["transactions_failed"]
            )
            return 1
        else:
            logger.success("✅ Import terminé avec succès!")
            return 0

    except Exception as e:
        logger.exception("❌ Erreur fatale lors de l'import OFX:")
        return 1


if __name__ == "__main__":
    sys.exit(main())
