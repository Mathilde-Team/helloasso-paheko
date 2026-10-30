# HelloAsso–Paheko

Scripts for synchronizing HelloAsso activity with Paheko and importing OFX bank
statements.

## Requirements

- Python 3.11–3.13
- Poetry
- Credentials for HelloAsso and Paheko

## Local setup

1. Install dependencies:

   ```sh
   poetry install
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
poetry run python HelloassoPaheko.py --simulate
poetry run python OfxPaheko.py --simulation
```

For OFX imports, select one file with `--file` or set the input directory with
`--ofx-dir`:

```sh
poetry run python OfxPaheko.py --simulation --file ./ofx/statement.ofx
poetry run python OfxPaheko.py --simulation --ofx-dir ./ofx
```

The scripts still contact their services to read data, including in simulation
mode. Put OFX statements in the local `ofx/` directory; do not add statements or
other member/payment data to version control.
