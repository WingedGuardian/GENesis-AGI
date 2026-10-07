### Fixed
- Managed Codebase enablement checks typed backend commands and containment before
  activation and again after native enablement reloads. Custom installs use their
  existing `VENV_PATH` selection. Removal validates the full fixed artifact set
  before unlinking and reports partial removal and reload failures.
