# Job Aggregator

A small command-line script that collects job listings from two public APIs,
filters and flags them with your own rules, scores them against your own
keyword lists, and writes one CSV for you to review.

## What it does

For each keyword it runs four searches:

| Source label       | API     | Scope                                             |
| ------------------ | ------- | ------------------------------------------------- |
| `Adzuna (Local)`   | Adzuna  | Within a radius of the place in your config       |
| `Adzuna (Remote)`  | Adzuna  | Nationwide, keeping only listings that say remote |
| `USAJobs`          | USAJobs | Within a radius of the place in your config       |
| `USAJobs (Remote)` | USAJobs | Nationwide, positions marked as remote            |

It then:

1. drops listings whose title, employer or (for USAJobs) location matches your exclusion lists;
2. removes duplicates that share the same title and employer;
3. flags listings that appear to need an active security clearance, look contingent on a contract award, or sit in a place you listed as unwanted;
4. scores every listing against your keyword weights;
5. sorts unflagged listings first, highest score first, and writes the CSV.

## The two APIs and where to get free keys

- **Adzuna** — register at <https://developer.adzuna.com/> to get an application ID and an application key.
- **USAJobs** — request a key at <https://developer.usajobs.gov/apirequest/>. The API also needs the email address you registered with; it is sent as the `User-Agent` header.

Both are free.

## Setup

Requires Python 3.9 or newer.

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

Then create your two private files from the examples:

```
cp .env.example .env
cp config.example.yaml config.yaml
```

Fill in `.env`:

| Variable          | Value                                   |
| ----------------- | --------------------------------------- |
| `ADZUNA_APP_ID`   | Adzuna application ID                   |
| `ADZUNA_APP_KEY`  | Adzuna application key                  |
| `USAJOBS_API_KEY` | USAJobs API key                         |
| `USAJOBS_EMAIL`   | Email address registered with USAJobs   |

Edit `config.yaml` to hold your own place, radius, minimum salary, search
keywords, exclusion lists and keyword weights. Every key in the example is
required; lists may be empty.

`.env`, `config.yaml` and the CSV output are git-ignored. Do not commit them.

## Running

```
python job_scraper.py
```

With no arguments it runs every search in `config.yaml`.

| Flag                | Default         | Meaning                                                              |
| ------------------- | --------------- | -------------------------------------------------------------------- |
| `--limit N`         | config values   | Cap the number of result pages fetched per search, for every source  |
| `--query "KW"`      | config lists    | Run this single keyword through all four searches                    |
| `--config PATH`     | `./config.yaml` | Settings file                                                        |
| `--env-file PATH`   | `./.env`        | File holding the four variables                                      |
| `--output-dir PATH` | `./output/`     | Where the CSV is written; created if missing                         |

Relative paths are resolved against the directory you run the command from.

A quick trial run:

```
python job_scraper.py --query "office manager" --limit 1
```

While it runs, each request prints one status line with the source label, the
HTTP status code and the number of rows returned.

### Where the credentials come from

- A variable **already set in your environment wins** over the same variable in the env file. The file only fills in what is not already set.
- If you pass `--env-file` and that file does not exist, the script stops with an error.
- If you do not pass `--env-file` and there is no `./.env`, the file is skipped and all four variables must already be in the environment.
- If any of the four is missing, empty, or still a `<placeholder>`, the script names the variable and stops before making any request.

## The CSV

The file is named `jobs_YYYYMMDD_HHMM.csv` and is always written. If nothing
matched, it contains only the header row.

| Column                      | Meaning                                                                          |
| --------------------------- | -------------------------------------------------------------------------------- |
| `relevance_score`           | Keyword score, see below. Never below 0.                                         |
| `source`                    | One of the four source labels above                                              |
| `title`                     | Job title                                                                        |
| `company`                   | Employer, or the hiring organisation for USAJobs                                 |
| `location`                  | Location as given by the API, or `Remote`                                        |
| `salary`                    | Salary range when the API provides one, otherwise blank                          |
| `active_clearance_required` | `True` if the text says an active security clearance is needed or not sponsored  |
| `contingent`                | `True` if the text says the role depends on a contract award                     |
| `excluded_location`         | `True` if the location matches `exclude.locations`                               |
| `url`                       | Link to the listing                                                              |
| `description_snippet`       | First 300 characters of the description                                          |

Rows are sorted so that listings with neither `active_clearance_required` nor
`excluded_location` set come first, and by `relevance_score` descending within
each group.

## How relevance scoring works

The title and the description text are joined and lower-cased. Every keyword
under `scoring.high`, `scoring.medium` and `scoring.negative` in your config is
then checked as a plain substring:

- each keyword that appears adds its weight once, however many times it occurs;
- the example config uses `2` for high, `1` for medium and `-1` for negative keywords;
- the total is floored at `0`.

For Adzuna the text scored is the listing description. For USAJobs it is the
qualification summary, falling back to the job summary. The score is a rough
ordering aid, not a judgement of fit; tune the lists until the top of the CSV
looks right to you.

## Filtering rules

- `exclude.title_keywords` — a listing is dropped if its title contains one.
- `exclude.companies` — a listing is dropped if the employer name contains one.
- `exclude.usajobs_locations` — a USAJobs listing is dropped if its location contains one (nationwide listings are not affected).
- `exclude.locations` — the listing is kept, but `excluded_location` is set and it sorts to the bottom.

All matching is by lower-case substring.

## Adzuna attribution

Listings in the CSV whose `source` begins with `Adzuna` are provided by
[Adzuna](https://www.adzuna.com/). Their `url` column links back to the
listing on Adzuna. If you republish or display those listings, keep the
"Adzuna" label and the link, and follow Adzuna's API terms.
