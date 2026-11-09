# SPDX-License-Identifier: GPL-3.0-or-later
.PHONY: test

# uv utilise .python-version ; UV_PYTHON permet de tester une autre version.
test:
	uv run --locked pytest --cov --cov-report=term-missing --cov-report=json:coverage.json
	uv run --locked python scripts/verifier_couverture.py coverage.json
