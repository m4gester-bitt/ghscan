# ghscan

A recon/scanning tool for GitHub organizations. Point it at an org and it
will:

1. enumerate every public repo the org owns
2. figure out who's contributed to those repos (or, in "org-members"
   mode, pull the org's actual member list instead)
3. enumerate every public repo each of those people personally owns
4. dedupe everything into one scan queue
5. run [TruffleHog](https://github.com/trufflesecurity/trufflehog)'s
   `--only-verified` mode against every repo in that queue
6. spit out a report of anything it found

An org's real secret-leak exposure isn't just its own repos, it's also
every employee's personal side-projects that happen to include a
copy-pasted API key or an old `.env` file. This tool maps that whole
footprint and scans it in one pass. Only point it at orgs/repos/accounts
you own or are explicitly authorized to test -- see `LICENSE.md`.

Everything is cached in a local SQLite file as it goes, so a run can be
killed (Ctrl-C, a crash, whatever) and picked back up later without
re-querying the API or re-scanning repos it already finished.

#### Requirements

- Python 3.9+
- [TruffleHog](https://github.com/trufflesecurity/trufflehog) installed
  and on your `PATH` (or pass `--trufflehog-path` to point at it)
- A GitHub personal access token with at least public read access
  (optional -- see below)

#### Install

```bash
git clone https://github.com/<your-username>/ghscan.git
cd ghscan
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Or install it as a proper console command:

```bash
pip install -e .
ghscan --org some-org
```

Either way, make sure you `cd` into the folder you cloned/unzipped
before running anything below -- the commands assume you're already
inside it.

## Quick start

```bash
export GITHUB_TOKEN=ghp_yourtokenhere
python3 -m ghscan --org some-org
```

That runs with the defaults: skips archived repos and forks, derives
contributors from commit history, no volume caps. For a first look at a
big org, it's worth doing a dry run first:

```bash
python3 -m ghscan --org some-org --dry-run
```

This runs discovery, builds the scan queue, prints roughly how many
repos it would scan and how many API calls that took, then stops before
touching TruffleHog at all.

## Running without a token

Don't need `GITHUB_TOKEN` set anymore to start it up. If it's missing it just asks:

```
No GITHUB_TOKEN environment variable detected.
Would you like to enter one now? [Y/n]
```

Say yes, type it in (hidden like a password), it carries on. Say no and it checks once more:

```
Are you sure you want to continue with no GitHub token? [y/N]
```

Confirm and it runs unauthenticated (60 req/hr cap from GitHub, fine for small orgs, slow for big ones). Only prompts when you're at an actual terminal -- in CI/scripts it just exits and tells you to set `GITHUB_TOKEN`.

## A few tips

- Start with `--dry-run` on any org you haven't scanned before, just to see the queue size before it touches trufflehog.
- `--exclude-bot-logins` is basically free -- always worth passing on `org-members` mode.
- Big org and rate limits hurting? Bump `--api-workers` down, not up, counterintuitively -- fewer parallel calls means fewer 403s to back off from.
- Findings live in the cache even without `--save`, so if you forgot the flag you can just re-run with it added -- nothing gets rescanned.

## Resuming

Everything lands in `ghscan_cache.sqlite3` (or wherever `--db-path`
points). Just re-run the same command and it'll pick up where it left
off -- already-discovered repos, contributors, and scan results are
never redone. Pass `--fresh` to wipe the cache and start over.

Repos that were already scanned in an earlier run also get automatically
checked for new pushes since their last scan (comparing the repo's
`pushed_at` against when the last scan finished) and get requeued if
they've changed. So re-running the same command a week later mostly just
scans what's new.

## Output

By default the full report (summary + findings + skipped/failed repos)
just prints to your terminal at the end of the run -- nothing is written
to disk unless you pass `--save`:

```bash
python3 -m ghscan --org some-org --save
python3 -m ghscan --org some-org --save --report-json-path findings.json
```

## All the flags

**Filtering**
- `--org <name>` -- target org (required)
- `--include-archived` -- include archived repos (default: skipped)
- `--include-forks` -- include forked repos (default: skipped)
- `--include-forks-if-ahead` -- with `--include-forks`, only scan forks
  that actually have commits ahead of their upstream, instead of every
  fork (skips the huge number of forks that are byte-identical mirrors)
- `--org-repos-only` -- only scan the org's own repos; also skips the
  contributor-discovery API calls entirely, not just the final scan
- `--contributor-repos-only` -- only scan contributor-owned repos, skip
  the org's own repos
- `--member-mode {contributors,org-members}` -- `contributors` (default)
  derives people from commit history across every org repo; `org-members`
  uses the org's actual membership roster instead (tighter, but misses
  active outside contributors who aren't org members)
- `--allow-private` -- by default any repo the API returns marked
  private gets dropped as a safety guard; pass this to allow them through

**Exclusions**
- `--exclude-repo <owner/name>` -- skip a specific repo, glob patterns
  allowed (e.g. `--exclude-repo 'someupstream/*'`), repeatable
- `--exclude-login <login>` -- never expand this contributor/member into
  their personal repos, repeatable
- `--exclude-bot-logins` -- auto-skip any login matching `*[bot]`
- `--language <lang>` -- only scan repos whose primary language matches
  (repeatable)
- `--exclude-language <lang>` -- skip repos whose primary language
  matches (repeatable)

**Volume control**
- `--max-contributor-repos <N>` -- cap how many repos are pulled per
  contributor
- `--min-stars <N>` -- skip repos under this star count
- `--max-repo-size-kb <N>` -- skip repos bigger than this
- `--pushed-within-days <N>` -- skip repos with no push in the last N
  days
- `--min-repo-age-days <N>` -- skip repos created less than N days ago
  (throwaway/test repo filter, inverse of `--pushed-within-days`)
- `--max-queue-size <N>` -- hard cap on total repos scanned
- `--sort-queue-by {pushed,stars,size}` -- what order the queue is
  scanned in, and what `--max-queue-size` keeps first (default: `pushed`)

**Concurrency**
- `--api-workers <N>` -- concurrent GitHub API discovery threads
  (default: 8)
- `--scan-workers <N>` -- concurrent TruffleHog processes (default: 3)

**TruffleHog**
- `--trufflehog-path <path>` -- path to the binary (default: `trufflehog`
  on `PATH`)
- `--trufflehog-timeout <seconds>` -- per-repo timeout (default: 1800)
- `--no-json-output` -- don't append `--json` (findings won't be parsed
  into the report)
- `--no-trufflehog-token` -- don't pass `GITHUB_TOKEN` through to
  TruffleHog itself
- `--trufflehog-arg <arg>` -- pass an extra raw argument through,
  repeatable
- `--shallow-depth <N>` -- adds `--depth=<N>` to the TruffleHog call to
  limit how much history gets pulled on huge repos (trades scan speed for
  missing older secrets; only has an effect if your TruffleHog build
  honors `--depth`)

**Persistence & output**
- `--db-path <path>` -- cache database location (default:
  `ghscan_cache.sqlite3`)
- `--report-path <path>` -- markdown report path, only written if `--save`
  is also passed (default: `ghscan_report.md`)
- `--report-json-path <path>` -- optional JSON report path, same rule
- `--save` -- actually write the report file(s) to disk
- `--fresh` -- wipe the cache database and start over

**Safety**
- `--dry-run` -- run discovery, print the estimated queue size and API
  call count, stop before scanning
- `--yes` -- skip the confirmation prompt that otherwise shows up before
  a wide-open `org-members` run with no volume filters set

**Logging**
- `--log-level {DEBUG,INFO,WARNING,ERROR}`
- `--json-logs` -- structured JSON log lines instead of plain text

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

## Publishing this to your own GitHub

1. Unzip this archive somewhere and `cd` into it.
2. Initialize a repo and make the first commit:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   ```
3. Create an empty repository on GitHub (no README/license/gitignore --
   this project already has all three), then point your local repo at it:
   ```bash
   git remote add origin https://github.com/<your-username>/ghscan.git
   git branch -M main
   git push -u origin main
   ```
4. Double check `.gitignore` is doing its job -- you don't want
   `ghscan_cache.sqlite3` or a saved report accidentally committed.

## License

See `LICENSE.md`. Short version: free to use and modify for personal,
educational, and internal/authorized-testing purposes, but you can't
resell it, repackage it under another name, or strip the attribution.

## Author

m4gester-bitt
