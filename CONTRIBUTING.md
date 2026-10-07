# Contributing

Use Python 3.11 or newer and install development dependencies from the project root:

```bash
python -m pip install -e ".[dev]"
pre-commit install
```

Before opening a pull request:

```bash
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
python -m build
```

Tests require no Google account or private configuration. Add regression coverage for changes to scheduling, identity matching, or synchronization. Use invented guests, properties, reservation codes, and messages. Keep API calls mocked in unit tests.

Preserve existing Calendar IDs, task notes markers, database mappings, and completed-task state when changing reconciliation. Describe any migration behavior explicitly. Keep operational state outside the package and Git.

Report bugs with the command, Python version, expected result, actual result, and a redacted error. Remove names, addresses, reservation identifiers, feed URLs, OAuth data, and account identifiers from logs and screenshots. For secret-handling guidance, see [SECURITY.md](SECURITY.md).
