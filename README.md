# WHOOP price tracker (runs in the cloud, set up entirely in a browser)

Reads prices hourly in a real headless Chrome, converts everything to USD, benchmarks against Noon UAE,
emails/WhatsApps you on any price DROP, and publishes a self-refreshing dashboard.

## Setup (all on github.com, no installs)
1. Create a GitHub account/repo (public is simplest: free GitHub Pages needs a public repo).
   Add file > Upload files: drag in everything from this folder. Keep the `docs` and `.github/workflows` folders.
2. Open `config.json` in the repo, click the pencil, and replace every `PASTE_...` with the product URL
   (Amazon CA, Best Buy CA, Walmart US/CA, Amazon US Peak). Empty or PASTE_ entries are skipped.
3. Settings > Pages > Source: "Deploy from a branch", Branch: main, Folder: /docs. Your dashboard URL appears there.
4. Settings > Secrets and variables > Actions > New repository secret:
   - Email: `ALERT_EMAIL_TO`, `SMTP_USER` (Gmail address), `SMTP_PASS` (Gmail App Password)
   - WhatsApp (pick one): Twilio `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_WHATSAPP_FROM` + `WHATSAPP_TO` (+971...);
     or CallMeBot `CALLMEBOT_KEY` + `WHATSAPP_TO`
   - `BESTBUY_API_KEY` (free at developer.bestbuy.com) for Best Buy USA
   - `PROXY_URL` (strongly recommended, see below), optional `SCRAPER_API_KEY` as a fallback
5. Actions tab > "whoop-price-tracker" > Run workflow. The first run records the baseline (no alerts).
   From the second run on, any drop triggers an alert. Open your Pages URL to see the dashboard.

## Important: bot blocking
Amazon, Walmart, Best Buy Canada and Noon often block GitHub's datacenter IPs. If cells show "no reading",
add a residential proxy as `PROXY_URL` (format `http://user:pass@host:port`) or a `SCRAPER_API_KEY`.
Without one, expect gaps. Selectors that read the on-page price are in `SELECTORS` in tracker.py and may need tuning.

## Notes
- Alerts fire on drops only. To also hear about rises, add a repository *variable* `ALERT_ON_RISE` = `1`.
- "base price" in the dashboard means the sale price wasn't visible on the page.
- Price changes are compared in local currency, so exchange-rate moves never cause alerts.
- Files written each run: `docs/prices.json` (dashboard), `state.json`, `history.csv`.
