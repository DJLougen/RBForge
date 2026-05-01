# Changelog

All notable changes to RBForge will be documented here.

## [0.2.0] - 2026-05-01

### Added

- Integrated RBMEM v0.4 JSON diagnostics through `RbmemStore.doctor()`.
- Integrated RBMEM v0.4 JSON context retrieval through `RbmemStore.context()`.
- Added RBMEM CLI version checks with `RbmemStore.rbmem_version()`.
- Included RBMEM diagnostics and task-specific context previews in forge results.

### Changed

- Renamed the internal prototype package from the old working name to `rbforge_core`.
- Removed old working-name references from code, docs, examples, configs, and generated graph reports.
- Updated persisted RBMEM record schemas to `rbforge.*`.

## [0.1.0] - 2026-05-01

### Added

- Initial RBForge runtime tool forging, validation, sandboxing, RBMEM persistence, registry, graph patching, and usage metrics.
