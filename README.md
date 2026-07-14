# What's cooking in zh_CN

A static dashboard for pending `docs/zh_CN` Linux documentation patches. It mirrors the sections in the mailing-list “What's cooking” report:

- **Applied** — every patch subject in the series appears in Alex Shi's `docs-next` history, or a previous run already established that it was applied
- **Cooking** — the latest detected reroll is pending and at most 30 days old
- **Cold** — an unapplied series whose latest revision is over 30 days old

The dashboard is regenerated hourly from [linux-doc on lore](https://lore.kernel.org/linux-doc/) and the [`docs-next` tree](https://git.kernel.org/pub/scm/linux/kernel/git/alexs/linux.git/log/?h=docs-next). It uses only Python's standard library and Gitiles, so it does not clone the Linux repository.

## Run locally

```sh
python3 scripts/update.py --verbose
python3 -m http.server 8000
# open http://localhost:8000
```

Run tests with:

```sh
python3 -m unittest discover -s tests -v
```

Status classification is fully automatic. The collector keeps the latest detected reroll and compares every non-cover patch subject with recent `docs-next` commit subjects. Applied status is monotonic: once recorded in `data/status.json`, a shorter future history scan cannot downgrade that series to Cooking.

## GitHub setup

1. Push this repository to GitHub.
2. Under **Settings → Pages**, select **Deploy from a branch**, then `data` and `/ (root)`.
3. Run **Update patch dashboard** once, or wait for the hourly schedule.

`update.yml` restores the last generated status, runs the collector, and publishes a complete site snapshot to the `data` branch. The source branch remains free of hourly generated commits.
