# Contributing to olivetin-mcp

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/vaddisrinivas/olivetin-mcp.git
cd olivetin-mcp

# Install dependencies (using uv)
uv sync --all-extras

# Or with pip
pip install -e ".[test,lint]"

# Or use make
make install
```

### Pre-commit Hooks (recommended)

```bash
pip install pre-commit
pre-commit install
```

This runs ruff lint + format on every commit automatically.

## Running Tests

```bash
# Full test suite with coverage
make test
# or
pytest --cov=main --cov-report=term-missing --cov-fail-under=95 tests/
```

## Linting & Type Checking

```bash
# Run all checks (what CI runs)
make ci

# Or individually
ruff check .            # lint
ruff format --check .   # format check
mypy main.py            # type check
ruff format .           # auto-format
```

## Making Changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Make your changes
4. Run `make ci` to verify all checks pass
5. Commit with a descriptive message using [conventional commits](https://www.conventionalcommits.org/) (e.g. `feat:`, `fix:`, `docs:`)
6. Open a pull request against `main`

## Code Style

- Type-annotate all public functions
- Use `structlog` for logging (no `print()` statements)
- Keep test coverage at 100% (CI enforces >= 95%)
- Follow existing patterns in the codebase

## Code Review

- All PRs require passing CI (tests, lint, type check, Docker build)
- Maintainer typically reviews within 1-3 business days
- Please update `CHANGELOG.md` for user-facing changes

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for a walkthrough of the codebase, including class responsibilities and request flow.

## Reporting Issues

Use [GitHub Issues](https://github.com/vaddisrinivas/olivetin-mcp/issues) to report bugs or request features. Include:

- Steps to reproduce
- Expected vs actual behavior
- Python version and OS
- olivetin-mcp and OliveTin versions

## Security Vulnerabilities

**Do NOT open a public issue.** Email vaddisrinivas170497@gmail.com instead. See [SECURITY.md](SECURITY.md) for details.

## Intellectual Property

By submitting a pull request, you agree that your contribution is licensed under the project's [MIT License](LICENSE). See [GOVERNANCE.md](GOVERNANCE.md) for details.
