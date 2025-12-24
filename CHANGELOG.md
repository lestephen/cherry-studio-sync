# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2025-12-24

### Added
- Initial release
- CLI tool for merging Cherry Studio backups across multiple computers
- GUI mode with `--gui` flag for non-CLI users
- Cross-platform launcher scripts (`run_gui.bat` for Windows, `run_gui.sh` for Mac/Linux)
- Automatic Python environment detection (uv, conda, system Python)
- Timestamp-based conflict resolution for conversations
- Per-computer merged backups preserving machine-specific settings
