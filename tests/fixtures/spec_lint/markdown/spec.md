# Feature Specification: Pocket ledger notes

**Branch**: `main` | **Created**: 2026-01-10 | **Status**: Approved

## User Story 1 - Save a note (Priority: P1)

The user saves a note from the command line.

- `AS-1` Given an empty ledger, when the user saves a note, then the ledger holds one note
- `AS-2` Given a ledger holding a note, when the user saves it again, then the ledger still holds one note
- `AC-1` Saving the same note twice is idempotent and never duplicates data

- `TS-1` (unit)
  - Covers: AS-1
  - Then: the ledger file holds one note

## Requirements

- **FR-001**: The tool MUST store each note with a title and a timestamp (US1).
- **FR-002**: The tool MUST ignore a note identical to an existing one (US1).
