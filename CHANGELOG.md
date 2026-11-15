# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Phase 1 scaffold: repository layout, MIT license, `.gitignore`.
- `skills/smc-analyst/SKILL.md` with trigger-rich frontmatter.
- Claude Code plugin manifest (`.claude-plugin/plugin.json`) and marketplace (`.claude-plugin/marketplace.json`).
- `scripts/setup.py`: cross-platform, idempotent virtualenv bootstrap with pinned `requirements.txt`.
- `config.default.json`: default engine, trade, risk, data and server settings.
- `tests/test_scaffold.py`: structural checks for manifests, SKILL.md, config and setup.
