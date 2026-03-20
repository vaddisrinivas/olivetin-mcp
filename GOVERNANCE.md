# Governance

## Maintainers

- **Srinivas Vaddi** ([@vaddisrinivas](https://github.com/vaddisrinivas)) — Creator & Lead Maintainer

## Decision-Making

- **Small changes** (bug fixes, docs, typos): Merged directly by maintainer after CI passes.
- **Features** (new tools, APIs, config options): Discussed in a GitHub Issue before implementation; maintainer approves.
- **Major changes** (breaking APIs, architecture rewrites, new major version): RFC label on a GitHub Discussion, community feedback period of at least 7 days, maintainer final decision.

## Release Cycle

- **Patch** (`2.0.x`): Critical fixes, released as needed.
- **Minor** (`2.x.0`): New features, released when ready.
- **Major** (`x.0.0`): Breaking changes, planned with deprecation notices in the prior minor release.

All releases follow [Semantic Versioning](https://semver.org/).

## Code Review

- All changes go through pull requests (no direct pushes to `main`).
- PRs require passing CI (tests, lint, type check) before merge.
- Maintainer reviews and merges; community reviewers welcome.
- Typical review turnaround: 1-3 business days.

## Security

See [SECURITY.md](SECURITY.md) for vulnerability reporting and response SLAs.

## Intellectual Property

All contributions are licensed under the project's [MIT License](LICENSE). By submitting a pull request, you agree that your contribution is licensed under the same terms.

## Future: Co-Maintainer Path

As the community grows, we welcome co-maintainers. Path to co-maintainership:

1. 5+ merged pull requests demonstrating code quality and project understanding
2. Active participation in issue triage or code review
3. Discussion with the lead maintainer
4. Added to [CODEOWNERS](.github/CODEOWNERS)
