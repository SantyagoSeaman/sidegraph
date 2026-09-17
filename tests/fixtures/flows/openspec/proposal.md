## Why

The current importer duplicates every archived proposal on re-import.

## What Changes

- Add ref normalization so the archived path collapses onto the live-path ref.
- Add a degenerate-parent write-path rule for split-produced parents.

## Impact

- Affected specs: importer
- Affected code: `src/sidegraph/doc_import.py`
