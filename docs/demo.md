# GitHub Pages demo

The browser demo uses a synthetic October 2026 calendar. The demo starts on **Standard**, where all four calendar entries say **Reserved**, including the host block. **Enrich** adds invented guest names, guest counts, check-in/check-out times, and confirmation codes, and identifies the host block as **Host blocked**. Additional presets demonstrate cancellation, an extended host block, and a repeated sync. Selecting a date shows its stays and reminders; the reminder panel can also show the full month's schedule.

## Publish

1. Push the project, including `.github/workflows/pages.yml`, to `main` on GitHub.
2. In the repository, open **Settings → Pages → Build and deployment** and set **Source** to **GitHub Actions**.
3. Under **Actions**, run **Demo / GitHub Pages** manually if the initial push ran before Pages was enabled. Later pushes affecting the demo or application automatically rebuild and publish it.

The expected URL for this repository is `https://satoshilab21.github.io/host-helper/`. It becomes available after the first successful deployment. The workflow uses the standard `github-pages` environment and GitHub's deployment token; no custom credentials or VPS are needed. See [GitHub's custom workflow documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).

Only the generated `_site` directory is uploaded. The workflow never uploads the repository root. Pull requests build and test the demo without deploying it.

## Preview locally

From the project root after installing the Python package:

```bash
python -m scripts.build_demo
python -m http.server 4173 --bind 127.0.0.1 --directory _site
```

Open `http://127.0.0.1:4173`. You can also open `_site/index.html` directly; scripts and assets use relative paths and no remote fetches. The generated directory is ignored by Git.

## How scenarios are generated

`scripts/build_demo.py` starts with four invented past checkouts and four October stays. It runs the real block matcher, database lifecycle functions, Calendar synchronization, trash rules, and Tasks reconciliation using an in-memory SQLite database and local stand-ins for the Google APIs.

The Standard view shows generic reserved intervals before enrichment. Enrich passes invented confirmation emails through the real email parser and booking enrichment functions, then synchronizes the resulting bookings. Each later scenario first builds that enriched state, then applies its change and synchronizes again. This demonstrates cancellation cleanup, preserved Calendar identity when a block changes, and repeat-sync behavior using application code. The sample reference date and rule defaults are fixed so results remain reproducible as time passes. No scheduling logic is duplicated in JavaScript.

The build loads no `.env`, disk database, OAuth files, emails, or live feed. Only explicitly selected fields from synthetic results are serialized to `data.js`. An allowlist copies four frontend assets; arbitrary files from the repository are never copied into the site.

The page switches between these generated snapshots in the browser. It does not run Python on demand, call Google, or accept live booking data. There are no external fonts, analytics, or frontend runtime dependencies. The same Python unit test command used by CI checks the scenarios and public output boundary.
