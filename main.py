"""Quick entry point: scrape the MOH drug list and save medications.csv.

This is a convenience wrapper around the syrian_medication package for users
who only need the Ministry of Health table without manufacturer enrichment.

Usage:
    python main.py
"""

from __future__ import annotations

import sys

# Re-use the full scraper but skip manufacturer sites
sys.argv.extend(["--skip-manufacturers"])

from syrian_medication import main  # noqa: E402

if __name__ == "__main__":
    main()