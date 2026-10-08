"""Allow ``python -m ecommerce_scraper``."""

import sys

from ecommerce_scraper.cli import main

if __name__ == "__main__":
    sys.exit(main())
