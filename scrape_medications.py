"""Backward-compatible wrapper: delegates to the syrian_medication package.

The actual implementation has been moved to the syrian_medication/ package
for better modularity. This file remains for backward compatibility.

Examples:
  python scrape_medications.py
  python scrape_medications.py --moh live
  python scrape_medications.py --only Alpha --only Asia --max-products 20
"""

from __future__ import annotations

from syrian_medication.cli import main

if __name__ == "__main__":
    main()

