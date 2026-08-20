"""DrivePrep: secure erase, health test, and listing documentation for used HDDs.

See DRIVEPREP-SPEC-UBUNTU.md revision 2. Section references in docstrings and
comments throughout this package point at that document.
"""

__version__ = "1.0.0"

TOOL_NAME = "driveprep"

# report.json / state.json schema. Bump on any incompatible shape change.
SCHEMA_VERSION = 1
