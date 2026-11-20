# Solar Guitars free-product checker

Every 60 seconds this checks the Solar Guitars store for products priced at €0. When one
appears it emails you the product name, price, status and link.

It runs on **GitHub Actions**, so your PC doesn't need to stay on. It can also run locally on
Windows, where it adds a desktop notification and opens the product page in your browser.

It needs only Python 3.8 or newer. There's nothing to install.

## Run it on GitHub Actions

1. **Create a public repository** on GitHub and push this folder to it.
   It has to be public because the checker runs around the clock, about 1,440 minutes a day.
   Actions minutes are free for public repositories, but a private repository on the free plan
   gets 2,000 minutes a month, which would run out in about a day and a half. The code is the
   only thing made public: your email address and password are stored as secrets, and the logs
   don't print them.
2. **Add secrets.** In the repository, go to **Settings > Secrets and variables > Actions >
   New repository secret** and add:

   | Secret | Value |
   |---|---|
   | `SOLAR_SMTP_USER` | your Gmail address |
   | `SOLAR_SMTP_PASSWORD` | a Gmail App Password from <https://myaccount.google.com/apppasswords> (needs 2-Step Verification). Not your normal password. |
   | `SOLAR_EMAIL_TO` | *(optional)* who gets the alerts, comma-separated. Defaults to `SOLAR_SMTP_USER`. |

3. **Test it.** Go to **Actions > Send test email > Run workflow**. The run first prints what the
   store looks like from GitHub's servers, then emails you the first product from
   `shop/?min=229&max=3999`, marked as a test.
4. **Start watching.** Go to **Actions > Watch for free products > Run workflow**. After that,
   it keeps itself going:
   - Each run checks every minute for about 5 hours 40 minutes. GitHub stops jobs at 6 hours.
   - An hourly schedule keeps the next run queued, so it starts as soon as the previous one ends.

   To stop it, open the **Watch for free products** workflow, use the **...** menu to
   **Disable workflow**, then cancel the running job.

GitHub's servers are in the US, so the store may show prices in $ there. A €0 product is $0 too,
so that doesn't change what gets detected.

## Run it on your PC instead

1. Paste your Gmail App Password into `config.local.ini`. That file is git-ignored, so it never
   gets pushed.
2. Test it:
   `python solar_checker.py --test "https://www.solar-guitars.com/shop/?min=229&max=3999"`
3. Start it by double-clicking `run.bat`. It keeps the PC awake while it runs. Don't run it locally
   while the GitHub version is also running, or you'll get every alert twice.

## How it checks

- **Store pages** listed under `pages` in `config.ini`:
  <https://www.solar-guitars.com/shop/?min=0&max=1> and
  <https://www.solar-guitars.com/black-sabbath/?min=0&max=1>. To watch another page, add it on
  its own indented line.
- **WooCommerce Store API:** covers every product, including accessories the pages don't list.

Three old products already sit at €0 (two Desert Rose guitars and a hoodie), all out of stock.
On the first run they're recorded in `state.json` without an alert. On GitHub, that file is
carried from one run to the next with the Actions cache. After that you get an alert when:

- a product at €0 is **in stock and buyable** (once per product, not every minute), or
- a **new** €0 product is listed but not buyable yet. That's an early heads-up, and a second
  alert follows when it becomes buyable.

If the site can't be reached for 10 checks in a row, you get one warning email.

## Settings

Shared settings are in `config.ini`, which is committed. Private ones go in `config.local.ini`
or in environment variables named `SOLAR_<KEY>`, for example `SOLAR_SMTP_PASSWORD`. The
environment variable wins over both files.

| Command | What it does |
|---|---|
| `python solar_checker.py` | Run until stopped (same as `run.bat`) |
| `python solar_checker.py --max-minutes 340` | Stop after about 340 minutes (used on GitHub) |
| `python solar_checker.py --dry-run` | Check once and print what's at €0, without sending notifications |
| `python solar_checker.py --test ["URL"]` | Send a test alert, using the first product on URL if one is given |
