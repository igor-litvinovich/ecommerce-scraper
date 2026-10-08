# E-commerce scraper

A command-line tool that crawls the
[webscraper.io static e-commerce test site](https://webscraper.io/test-sites/e-commerce/static),
visits every product page, and writes one JSON report: one entry per purchasable product
configuration, plus the total of their prices.

**Result:** 147 products expand to **423 results** with a total of **345,701.52**
(catalogue as of October 2026). Each available HDD option is its own result, priced with a
surcharge the site applies only in JavaScript. Disabled options are excluded. Why, and what
else the site hides, is covered under [Traps](#traps-on-the-target-site) and
[Decisions](#interpretation-decisions).

```console
$ uv run ecommerce-scraper -o report.json
2026-10-08 08:55:35,619 INFO    ecommerce_scraper.crawler: Crawled 1 listing page(s) so far; 3 products found
…
2026-10-08 08:55:39,171 INFO    ecommerce_scraper.crawler: Discovered 147 products across 32 listing pages
2026-10-08 08:55:40,726 INFO    ecommerce_scraper.crawler: Fetched 15/147 product pages (10%)
…
2026-10-08 08:55:53,926 INFO    ecommerce_scraper.crawler: Fetched 147/147 product pages (100%)
2026-10-08 08:55:53,927 INFO    ecommerce_scraper: Scraped 423 results (total 345701.52) with 179 HTTP requests in 18.6s
$ uv run ecommerce-scraper-validate report.json
OK: 423 results, total 345701.52
```

Abridged; the real output is indented with one field per line:

```json
{
  "results": [
    { "name": "Nokia 123", "description": "7 day battery", "price": 24.99, "colors": ["gold", "white", "black"] },
    …
    { "name": "Dell Latitude 5580 128 GB", "description": "Dell Latitude 5580, 15.6\" FHD, Core i5-7300U, 16GB, 256GB SSD, Linux + Windows 10 Home", "price": 1178.19 },
    { "name": "Dell Latitude 5580 256 GB", "description": "…", "price": 1198.19 }
  ],
  "total": 345701.52
}
```

## Quick start

Requires [uv](https://docs.astral.sh/uv/), which installs Python 3.12 if needed.

```bash
uv sync                                         # install the app and dev tools into .venv
uv run ecommerce-scraper                        # report on stdout, progress on stderr
uv run ecommerce-scraper -o report.json         # or write a file (atomically)
uv run ecommerce-scraper-validate report.json   # check a report against the contract
make check                                      # what CI runs: all pre-commit hooks, then tests
make hooks                                      # run those hooks on every commit
```

To install both commands on your `PATH`, use
`uv tool install git+https://github.com/igor-litvinovich/ecommerce-scraper`, or run
`pip install .` from a checkout. With Docker:

```bash
docker build -t ecommerce-scraper .
docker run --rm ecommerce-scraper > report.json
# writing into a mounted directory: on Linux, run as your own uid so it is writable
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/out" ecommerce-scraper -o /out/report.json
```

| Option | Default | Purpose |
|---|---|---|
| `--start-url` | the test site | Where to start; redirects such as http→https are followed. A sub-category such as `…/phones/touch` also works |
| `-o, --output` | stdout | Report file. Written atomically, keeping the existing file's permissions |
| `--compact` | off | Single-line JSON |
| `--rate-limit` | 10 | Maximum requests per second (`0` disables the limit) |
| `--concurrency` | 5 | Maximum simultaneous requests |
| `--timeout` | 15 | Seconds allowed for each network step (connect, send, receive) |
| `--deadline` | none | Give up if the whole run takes longer than this many seconds |
| `--max-attempts` | 3 | Attempts per page on transient errors |
| `--max-pages` | 200 | Limit on listing-page requests (guards against pagination loops) |
| `--log-level` | info | stderr verbosity: `debug` (every request), `info`, `warning`, `error` |

## Traps on the target site

Before writing any code I profiled the whole catalogue with a throwaway crawler.

| # | Pitfall | Naive result | Handling |
|---|---|---|---|
| 1 | The home page shows only **3 random "top items"**, and category pages 3 more. The 147 products sit under sub-categories. | 3 products | Breadth-first crawl of every category, sub-category and pagination link |
| 2 | **Pagination is truncated**: page 1 links only pages 2–10, 19 and 20, so pages 11–18 can't be reached from it. | About 40% of laptops missed | Each crawled page contributes its own page links; visited pages are never re-fetched |
| 3 | **HDD prices exist only in JavaScript.** The HTML shows the 128 GB price; `app.js` adds $20/$40/$60 for 256/512/1024 GB. | Every variant gets the same price; total **337,421.52** | Surcharge table in `catalogue.py`; an unknown size fails the run rather than being guessed. Daily live checks compare the table with `app.js` and click every option of every product in a real browser |
| 4 | **1024 GB is `disabled` on every product.** | 138 phantom products; total **465,253.39** | Excluded: the brief asks for every *available* product |
| 5 | The colour `<select>` has a **"Select color" placeholder**. | A bogus colour | Empty-value options are skipped. Only `select[aria-label=color]` counts as a colour picker |
| 6 | **Listing pages truncate names** (`"Dell Latitude..."`). | Truncated names | All fields come from the product page |
| 7 | **Prices are formatted inconsistently**: `$1178.19`, `$1149`, `$1124.2`. | Parse errors | A strict parser returns an exact `Decimal` and rejects other currencies, sub-cent amounts and garbage |
| 8 | **Float drift.** Adding up the variant prices one by one in floats gives `345701.52000000037`; the error depends on summation order. | A wrong-looking total | `Decimal` end to end. Values become JSON numbers only when written, losslessly |
| 9 | **The site's own JavaScript displays float artefacts**, e.g. `$517.1700000000001` for product 92 at 256 GB. | Garbage prices if read from the rendered page | Surcharges are added in `Decimal`, giving `517.17`. The browser check pins this case |
| 10 | **Names aren't unique**: 8 different "Dell Latitude 5480"s, and 3 "Iphone"s at the same price. | Real products lost to de-duplication | De-duplicated by URL only |
| 11 | **Tablets have both HDD options and colours.** | One of the two lost | Each HDD variant keeps the colour list |
| 12 | **A non-breaking space** (U+00A0) inside one description (product 42). | An invisible odd character | Whitespace is normalised |

The offline snapshot test (below) pins the resulting 423 results and 345,701.52.

## Interpretation decisions

Each of these is a small, contained change if a different reading was intended.

- **`total` is the sum of every result.** This follows the brief's own comment, "Sum of all
  prices in results". Summing only *distinct* price values (307,664.73) would drop real
  products such as the three iPhones at $899.99.
- **Colours are lowercased**, as in the brief's example, and de-duplicated. `colors` is
  omitted (rather than `null` or `[]`) unless there are at least two options. The site's
  JavaScript appends a chosen colour to the title; that is deliberately not copied, because
  colours are an attribute, not a separate product.
- **HDD names are `"<name> <capacity> GB"`**, as in the brief's example. A lone available
  option still gets the suffix. A product with every option disabled can't be bought, so it
  is dropped with a warning.
- **Prices are always JSON floats** (`1149.0`, not `1149`), so a consumer sees one type.
- **Source data is not "fixed".** Product 97's name and description refer to different
  laptops. That is reported as-is.
- **Output is deterministic**: ordered by product id, then by HDD capacity. There are no
  fields beyond the specified schema.

## Design

```
cli.py           arguments, logging, exit codes
 └ crawler.py    discovery (BFS), ordering, de-duplication: orchestration only
    ├ fetcher.py    HTTP: rate limit, concurrency, retries, redirects  (the only network I/O)
    ├ parsing.py    HTML → typed facts, pure                            (every CSS selector)
    └ catalogue.py  facts → results and total, pure                     (pricing and variant rules)
output.py        Report → JSON, atomic file write          models.py   frozen dataclasses
validate.py      stand-alone checker for the output contract (shares no code with the scraper)
```

Each kind of change has one home: markup in `parsing.py`, pricing in `catalogue.py`,
networking in `fetcher.py`.

## Running it in production

- **A wrong report is worse than no report.** The run exits `1`, writes nothing, and leaves
  any previous `--output` file intact if:
  - a page can't be fetched or parsed;
  - a page returns 200 without the store's layout (a maintenance or captcha page);
  - a product can't be priced;
  - no products are found.
- **Exit codes:**
  - `0`: success.
  - `1`: the scrape or write failed (the site or the environment let us down).
  - `2`: usage error.
  - `70`: an internal bug, logged with its traceback.
  - `130`: interrupted (Ctrl-C).
  - `143`: terminated by SIGTERM, for example a scheduler stopping the job; shut down
    cleanly, nothing written.

  Bad arguments are rejected before any network traffic, including an output path that
  is a directory, missing or unwritable. `--deadline` bounds the whole run.
- **Fails fast:** the first failure cancels in-flight work, so a struggling site isn't
  hammered.
- **Retries:** timeouts, connection errors and HTTP 429/500/502/503/504 are retried with
  capped, jittered exponential backoff. A 429 or `Retry-After` pauses every worker, not just
  the one that was told. A server asking for more than 10 s fails the page instead of
  being retried early. Other errors are permanent.
- **Scope:**
  - Navigation stays under the start URL, and products must be on the same host.
  - A redirect that leaves that scope fails the run.
  - Links resolve against the post-redirect URL.
  - `--max-pages` bounds the crawl.
- **Polite:** by default at most 10 requests per second and 5 at a time. The `User-Agent`
  names the tool and links to this repository. Only paths that `robots.txt` permits are
  crawled (checked by hand).
- **Visible progress:** INFO logs report each crawl wave, then every ~10% of product pages,
  then a summary with counts, total and duration.
- **Verifiable output:** `ecommerce-scraper-validate` checks any report without using the
  scraper's code. It checks:
  - required fields and types;
  - positive prices in whole cents;
  - the colour rules;
  - duplicate entries;
  - an exact `total`.

  CI runs it on a fresh live scrape.
- **Deployable:** a multi-stage, non-root Docker image whose code is root-owned. It suits
  cron or a Kubernetes `CronJob`. Base images and CI actions are pinned to digests, and
  Dependabot keeps them current.

## Testing

```bash
make test        # 190 offline tests, about 3 s, 99% statement and branch coverage
make test-live   # 4 checks against the real site, including a headless browser
```

- **Unit tests:** the parser runs against real saved pages plus synthetic edge cases.
  Business rules are tested with literal, hand-derived expectations.
- **Fake-site tests:** the crawler and CLI run against an in-memory store
  (`httpx.MockTransport`) with the real site's traps: duplicate featured items, truncated
  pagination, redirects, off-site links and maintenance pages. The HTTP client is real; only
  the network is fake.
- **Offline end-to-end:** `tests/fixtures/site_snapshot.json.xz` holds all 179 pages of the
  real site in 29 KB. The test asserts the exact result and runs it through the validator.
  `scripts/record_site_snapshot.py` re-records it.
- **Live checks** (daily in CI):
  - A full scrape, validated.
  - A comparison of our surcharge table with the site's `app.js`.
  - A headless-Chromium pass (Playwright) over every product with HDD options. It clicks
    each option and requires the displayed price, rounded to cents, to equal ours. It also
    confirms that 1024 GB is still unavailable on each product.

  GitHub pauses scheduled workflows after 60 days without repository activity, so a
  long-lived deployment should keep the schedule alive.
- **Static checks:** ruff, ruff format, strict mypy and hygiene hooks run through
  pre-commit, locally and in CI. CI runs on Python 3.12, 3.13 and 3.14.
  `make hooks-update` bumps the hook pins, which Dependabot doesn't cover.

## Trade-offs and future improvements

- **The JS pricing rule is copied, not executed.** Scraping every page through a browser
  would follow rule changes automatically. But it would be about 10× slower and much more
  fragile, and it would inherit the site's float artefacts (trap 9). Instead the rule is
  copied, unknown sizes fail loudly, and the daily checks hold the copy to both `app.js`
  and the browser.
- **All-or-nothing vs. best effort.** All-or-nothing is right for a report with a total. An
  `--allow-partial` mode (partial results plus a failure list) would suit other uses.
- **Schema purity vs. usefulness.** Fields such as `url`, `id` and `hdd_gb` would help
  downstream joins. They are left out to match the spec; an opt-in flag is easy to add.
- **Observability.** For scheduled runs I would emit structured JSON logs and metrics
  (duration, requests, retries, product count, total), and alert on sudden shifts.
- **General-purpose crawling** would also want `robots.txt` enforced in code, rate limits
  per host rather than per run, and configuration through environment variables.
- **`?page=1` aliases** cost 3 redundant requests. They are harmless, and canonicalising
  them would mean guessing at the site's URL semantics.

## How this was built

The brief encourages AI assistance; this was built with Claude Code.

- **Investigation first.** Before any code was written, a throwaway crawl profiled the
  whole site. The traps above, the expected 423 results and 345,701.52, and the
  interpretation decisions all come from that step.
- **Test-first.** Tests were written before each implementation. Commits pair each
  capability with its tests, so the red-then-green steps are not separate commits.
- **Verified in several independent ways:**
  - an exact offline replay of the whole site;
  - a contract validator that shares no code with the scraper;
  - daily live checks against `app.js` and a real browser;
  - separate AI review passes that re-derived the figures from the live site and checked
    this README's claims. Their findings were addressed.
