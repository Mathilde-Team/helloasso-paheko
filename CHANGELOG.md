# Journal des versions

## [Non publié]

### Modifié

- Migration de Poetry vers uv : métadonnées PEP 621, contraintes Python et
  dépendances conservées, environnement verrouillé dans `uv.lock`, commandes
  d'installation et d'exécution du README actualisées (#13).
- CI : installation et exécution avec `--locked`, sans dépendances de test
  éphémères. Le palier de couverture existant reste inchangé.

### Ajouté

- Tests de non-régression des métadonnées, contraintes, verrou et commandes uv.
