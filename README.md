# HelloAsso–Paheko

Scripts for synchronizing HelloAsso activity with Paheko and importing OFX bank
statements.

## Requirements

- Python 3.11–3.13
- uv
- Credentials for HelloAsso and Paheko

## Local setup

1. Install dependencies:

   ```sh
   uv sync --locked --python 3.13
   ```

2. Create local configuration files:

   ```sh
   cp .env.example .env
   cp config.example.toml config.toml
   ```

3. Enter credentials only in `.env`, and set the non-secret service URLs and
   organization slug in `config.toml`.

   Required `.env` variables:

   - `HELLOASSO_USERNAME`
   - `HELLOASSO_PASSWORD`
   - `HELLOASSO_API_KEY`
   - `HELLOASSO_API_SECRET`
   - `PAHEKO_USERNAME`
   - `PAHEKO_PASSWORD`

   Do not commit `.env`, `config.toml`, `token-helloasso.json`, financial
   exports, or logs. If a credential was ever committed or shared, rotate it
   with the service provider before using it again.

## Running

Run commands from the project directory so the local configuration is found:

The HelloAsso script accepts `--simulate` to avoid writing changes to Paheko:

```sh
uv run --locked python HelloassoPaheko.py --simulate
uv run --locked python OfxPaheko.py --simulation
```

For OFX imports, select one file with `--file` or set the input directory with
`--ofx-dir`:

```sh
uv run --locked python OfxPaheko.py --simulation --file ./ofx/statement.ofx
uv run --locked python OfxPaheko.py --simulation --ofx-dir ./ofx
```

The scripts still contact their services to read data, including in simulation
mode. Put OFX statements in the local `ofx/` directory; do not add statements or
other member/payment data to version control.

## Socle qualité Mathilde-Team

Ce dépôt suit le [socle qualité de l'équipe](https://github.com/Mathilde-Team/.github/blob/main/SOCLE.md) :

- **`main`** (livraison) et **`dev`** (intégration) protégées. On n'y pousse jamais directement : PR vers `dev`, puis PR `dev` → `main`.
- **CI** : job « Qualité (lint, tests) », requis sur les deux branches (`.github/workflows/ci.yml`).
- **Garde** : `.github/workflows/guard-main.yml` refuse les PR vers `main` qui ne viennent pas de `dev`.
- **Mise à jour des PR** : `.github/workflows/maj-branches.yml` met à jour les PR vers `dev` à chaque avancée de `dev` (SOCLE §11). Label `pas-de-maj-auto` pour exclure une PR ; une PR `resolu-par-ia` repasse en brouillon : la relire avant *Ready for review*.
- **Protections versionnées** : `.github/branch-protection/{main,dev}.json`, vérifiées chaque jour par le workflow « Protection des branches ». Un administrateur les applique via `scripts/appliquer_socle.sh Mathilde-Team/helloasso-paheko`.
- **Licence** : [GPL-3.0-or-later](LICENSE) (SOCLE §1) : `license = "GPL-3.0-or-later"` dans `pyproject.toml`, en-tête `# SPDX-License-Identifier: GPL-3.0-or-later` en tête de chaque fichier de code, de `scripts/` et de `tests/`.
- **Version** : déclarée une seule fois, dans `pyproject.toml`, tag `vX.Y.Z` au commit de release.
- **Couverture des tests** : seuil bloquant de **95 %**, branches comprises (décision d'équipe du 21/10/2026, [.github#9](https://github.com/Mathilde-Team/.github/issues/9)). Dépôt existant sans test : le seuil part de la mesure du jour dans `pyproject.toml` (`[tool.coverage.report] fail_under`) et monte à chaque PR de tests (issue #5) jusqu'à 95.

### Journalisation Python (SOCLE §8)

- Tout le Python du dépôt journalise avec **loguru** : `debug()` en entrée et avant chaque sortie de chaque fonction non triviale, chaque `except` journalise (cause exacte, jamais de secret), messages en français.
- Les scripts applicatifs historiques (`HelloassoPaheko.py`, `OfxPaheko.py`) ne sont pas encore conformes : la mise en conformité est suivie par l'issue #1.
