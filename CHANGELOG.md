# Journal des versions

## [Non publié]

### Corrigé

- Téléchargement des PDF : quand le navigateur ne démarre pas (Chromium non
  installé), l'instance Playwright est arrêtée et les PDF sont désactivés
  pour l'exécution, avec une seule erreur qui indique
  `playwright install chromium`. Avant, chaque PDF suivant échouait avec
  « Playwright Sync API inside the asyncio loop » (#38).
- Une commande sans date ne fait plus planter toute la synchronisation :
  repli sur la date du jour (#5).
- Un item de type absent du mapping (Bonus, MonthlyDonation…) est ignoré
  proprement au lieu de lever `AttributeError` sur `None.get` (#5).
- `logger.debug("Service existant ID: {}")` levait `UnboundLocalError`
  quand le service était créé : le message pointe désormais l'ID créé (#5).
- `_sync_tarifs` : condition de mise à jour morte (`tarifs_unchanged`
  jamais atteint) simplifiée (#5).

### Modifié

- Palier 6 de couverture (issue #5) : chemins d'erreur de `PahekoAPIClient`
  (retry HTTP, `get_activity`, `ensure_member_exists`, transactions,
  `get_sql` direct) et branches restantes de la création forcée des
  virements et des vérifications (réponses `{"data": …}`, année en erreur,
  CSV vide ou latin-1) par `tests/test_palier6_couverture.py`. Couverture
  globale 92,44 % → 95,84 %, seuil `fail_under` relevé de 92 à 95 (cible
  de l'issue).

- Palier 5 de couverture (issue #5) : la classe
  `sync_helloasso_orders_and_transactions` (initialisation, références,
  virements, exports JSON, vérifications, services, statistiques) et le
  point d'entrée `__main__` sont couverts par
  `tests/test_palier5_synchro.py`. Couverture globale 50,85 % → 92,44 %,
  seuil `fail_under` relevé de 50 à 92.

- Migration de Poetry vers uv : métadonnées PEP 621, contraintes Python et
  dépendances conservées, environnement verrouillé dans `uv.lock`, commandes
  d'installation et d'exécution du README actualisées (#13).
- CI : installation et exécution avec `--locked`, sans dépendances de test
  éphémères. Le palier de couverture existant reste inchangé.

### Ajouté

- Tests de non-régression des métadonnées, contraintes, verrou et commandes uv.
