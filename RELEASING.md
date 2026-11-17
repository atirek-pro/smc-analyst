# Releasing smc-analyst

## Checklist for a release (`vX.Y.Z`)
1. **Green CI on `main`** (lint + tests on Linux, Windows and macOS).
2. **Version:** bump it in `.claude-plugin/plugin.json` and `skills/smc-analyst/scripts/smc/__init__.py`, and move the `CHANGELOG.md` "Unreleased" notes under a dated `## [X.Y.Z]` heading.
3. **Local checks:**
   ```bash
   python -m pytest && python -m ruff check . && claude plugin validate .
   ```
4. **Fresh-install test** from GitHub in an empty folder:
   ```bash
   npx skills add atirek-pro/smc-analyst -a claude-code -y
   claude -p "Do an SMC analysis on ETHUSDT"
   ```
   Expect setup → report → dashboard link, and the dashboard still running afterwards.
5. **Media** (when the UI changed): `python tools/make_media.py`, then commit `docs/images/`.
   Also run `python tools/check_navigation.py <dashboard URL>`: every check must pass.
6. **Merge** the release PR into `main`.
7. **Tag and release** from `main`:
   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "smc-analyst vX.Y.Z"
   git push origin vX.Y.Z
   gh release create vX.Y.Z --title "smc-analyst vX.Y.Z" --notes-file docs/release-notes/vX.Y.Z.md
   ```
8. **Distribution** (manual; each uses the maintainer's own account):
   - **skills CLI** (`npx skills add atirek-pro/smc-analyst`): works straight from the public GitHub repo, so nothing needs submitting. The skills.sh directory picks up public skills as they're installed.
   - **Claude Code plugin marketplace:** this repo *is* a marketplace (`.claude-plugin/marketplace.json`). Users add it with `/plugin marketplace add atirek-pro/smc-analyst`.
   - **Anthropic's plugin directory:** submit through the official form (see the Claude Code docs, "Submit to Anthropic's directory"). Run `claude plugin validate .` first.
   - **Community lists** (e.g. awesome-claude-skills style repos): open a PR on those repos, following their contribution guidelines.

## Versioning
[Semantic Versioning](https://semver.org/):
- **major** for breaking changes to the `analyze.py` JSON contract (`smc/schema.py`), the CLI flags or `config.json` keys
- **minor** for new concepts and features
- **patch** for fixes
