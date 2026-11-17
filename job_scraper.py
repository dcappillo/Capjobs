"""
Job Aggregator

Pulls listings from the Adzuna API and the USAJobs API, filters and flags
them using the rules in a YAML config file, scores them against your own
keyword lists, and writes a CSV for review.

Credentials come from environment variables (optionally loaded from an env
file). Personal search settings come from config.yaml. See README.md.
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime

import requests
import yaml
from dotenv import load_dotenv

# -- CREDENTIALS --------------------------------------------------------------
REQUIRED_ENV_VARS = [
    "ADZUNA_APP_ID",
    "ADZUNA_APP_KEY",
    "USAJOBS_API_KEY",
    "USAJOBS_EMAIL",
]

ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
USAJOBS_URL = "https://data.usajobs.gov/api/search"

# -- GENERIC FLAGS (not personal, kept in code) -------------------------------
# Patterns that signal an active clearance is required (auto-flag)
CLEARANCE_FLAGS = [
    "active secret clearance",
    "active ts/sci",
    "must possess.*clearance",
    "current.*secret clearance",
    "active.*top secret",
    "clearance will not be sponsored",
    "no clearance sponsorship",
]

# Phrases that signal a contingent role
CONTINGENT_FLAGS = [
    "contingent upon",
    "contingent on contract",
    "pending contract award",
]

CSV_FIELDS = [
    "relevance_score", "source", "title", "company", "location", "salary",
    "active_clearance_required", "contingent", "excluded_location",
    "url", "description_snippet",
]


def fail(message):
    """Print a one-line error and exit non-zero, without a traceback."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


# -- ENVIRONMENT --------------------------------------------------------------
def load_env(env_file_arg):
    """Load the env file by explicit absolute path.

    An explicitly given path must exist. The default ./.env is optional: if it
    is absent the variables are expected to be in the environment already.
    Variables already set in the environment are never overridden by the file.
    """
    explicit = env_file_arg is not None
    path = os.path.abspath(env_file_arg if explicit else ".env")
    if os.path.isfile(path):
        load_dotenv(dotenv_path=path, override=False)
    elif explicit:
        fail(f"env file not found: {path}")


def read_credentials():
    """Return the four credentials, or exit naming whichever are unusable."""
    creds = {}
    bad = []
    for name in REQUIRED_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if not value or re.fullmatch(r"<.*>", value):
            bad.append(name)
        creds[name] = value
    if bad:
        fail(
            "missing, empty or placeholder environment variable(s): "
            + ", ".join(bad)
        )
    return creds


# -- CONFIG -------------------------------------------------------------------
def _lookup(raw, dotted, path):
    node = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node or node[part] is None:
            fail(f"config {path}: missing required key '{dotted}'")
        node = node[part]
    return node


def _number(raw, dotted, path):
    value = _lookup(raw, dotted, path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"config {path}: key '{dotted}' must be a number")
    return value


def _positive_int(raw, dotted, path):
    value = _lookup(raw, dotted, path)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(f"config {path}: key '{dotted}' must be a whole number of 1 or more")
    return value


def _text(raw, dotted, path):
    value = _lookup(raw, dotted, path)
    if not isinstance(value, str) or not value.strip():
        fail(f"config {path}: key '{dotted}' must be a non-empty string")
    return value.strip()


def _string_list(raw, dotted, path, lower=False):
    # A list key must be present, but may be empty ("[]").
    node = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            fail(f"config {path}: missing required key '{dotted}'")
        node = node[part]
    if node is None:
        node = []
    if not isinstance(node, list):
        fail(f"config {path}: key '{dotted}' must be a list")
    # Entries are not stripped: leading/trailing spaces can be meaningful
    # for substring matching (quote them in YAML).
    items = [str(item) for item in node if item is not None]
    return [item.lower() for item in items] if lower else items


def _weights(raw, dotted, path):
    node = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            fail(f"config {path}: missing required key '{dotted}'")
        node = node[part]
    if node is None:
        node = {}
    if not isinstance(node, dict):
        fail(f"config {path}: key '{dotted}' must be a mapping of keyword: weight")
    weights = {}
    for keyword, weight in node.items():
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            fail(f"config {path}: key '{dotted}' has a non-numeric weight")
        weights[str(keyword).lower()] = weight
    return weights


def load_config(config_arg):
    """Load and validate the YAML config. Exits with a one-line error if bad."""
    path = os.path.abspath(config_arg)
    if not os.path.isfile(path):
        fail(f"config file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError:
        fail(f"config file is not valid YAML: {path}")
    except (OSError, UnicodeDecodeError) as e:
        fail(f"config file could not be read ({type(e).__name__}): {path}")
    if not isinstance(raw, dict):
        fail(f"config file is not a YAML mapping: {path}")

    return {
        "min_salary": _number(raw, "min_salary", path),
        "adzuna_where": _text(raw, "adzuna.where", path),
        "usajobs_location_name": _text(raw, "usajobs.location_name", path),
        "radius_miles": _number(raw, "radius_miles", path),
        "searches_adzuna_local": _string_list(raw, "searches.adzuna_local", path),
        "searches_adzuna_remote": _string_list(raw, "searches.adzuna_remote", path),
        "searches_usajobs": _string_list(raw, "searches.usajobs", path),
        "exclude_locations": _string_list(raw, "exclude.locations", path, lower=True),
        "exclude_title_keywords": _string_list(raw, "exclude.title_keywords", path, lower=True),
        "exclude_companies": _string_list(raw, "exclude.companies", path, lower=True),
        "exclude_usajobs_locations": _string_list(raw, "exclude.usajobs_locations", path, lower=True),
        "scoring_high": _weights(raw, "scoring.high", path),
        "scoring_medium": _weights(raw, "scoring.medium", path),
        "scoring_negative": _weights(raw, "scoring.negative", path),
        "pages_adzuna": _positive_int(raw, "pages.adzuna", path),
        "pages_usajobs": _positive_int(raw, "pages.usajobs", path),
    }


# -- RELEVANCE SCORING --------------------------------------------------------
def score_relevance(title, snippet, cfg):
    combined = (title + " " + snippet).lower()
    score = 0
    for kw, val in cfg["scoring_high"].items():
        if kw in combined:
            score += val
    for kw, val in cfg["scoring_medium"].items():
        if kw in combined:
            score += val
    for kw, val in cfg["scoring_negative"].items():
        if kw in combined:
            score += val
    return max(0, score)


# -- HELPERS ------------------------------------------------------------------
def clean(text):
    if not text:
        return ""
    return re.sub(r'\s+', ' ', str(text)).strip()


def flag_clearance(text):
    text_lower = text.lower()
    for flag in CLEARANCE_FLAGS:
        if re.search(flag, text_lower):
            return True
    return False


def is_excluded(title, company, location, source, cfg):
    title_lower = title.lower()
    company_lower = company.lower()
    location_lower = location.lower()
    for kw in cfg["exclude_title_keywords"]:
        if kw in title_lower:
            return True
    for co in cfg["exclude_companies"]:
        if co in company_lower:
            return True
    if "USAJobs" in source and "Remote" not in location:
        for loc in cfg["exclude_usajobs_locations"]:
            if loc in location_lower:
                return True
    return False


def flag_contingent(text):
    text_lower = text.lower()
    for flag in CONTINGENT_FLAGS:
        if flag in text_lower:
            return True
    return False


def flag_location(location, cfg):
    loc_lower = location.lower()
    for exc in cfg["exclude_locations"]:
        if exc in loc_lower:
            return True
    return False


def dedupe(jobs):
    seen = set()
    unique = []
    for job in jobs:
        key = (job["title"].lower().strip(), job["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


def print_status(label, status_code, rows):
    """One line per request: source label, HTTP status, row count. No URL."""
    print(f"    [{label}] HTTP {status_code} rows={rows}")


def print_request_error(label, exc):
    """Only the exception class name: the message can contain the request URL."""
    print(f"    [{label}] request failed: {type(exc).__name__}")


def adzuna_salary(job):
    salary_min = job.get("salary_min") or 0
    salary_max = job.get("salary_max") or 0
    if salary_min and salary_max:
        return f"${int(salary_min):,} - ${int(salary_max):,}"
    if salary_max:
        return f"up to ${int(salary_max):,}"
    if salary_min:
        return f"from ${int(salary_min):,}"
    return ""


def usajobs_salary(pos):
    salary_range = pos.get("PositionRemuneration", [{}])
    sal_min = salary_range[0].get("MinimumRange", "") if salary_range else ""
    sal_max = salary_range[0].get("MaximumRange", "") if salary_range else ""
    if sal_min and sal_max:
        return f"${float(sal_min):,.0f} - ${float(sal_max):,.0f}"
    return ""


def usajobs_headers(creds):
    return {
        "Host": "data.usajobs.gov",
        "User-Agent": creds["USAJOBS_EMAIL"],
        "Authorization-Key": creds["USAJOBS_API_KEY"],
    }


# -- ADZUNA -------------------------------------------------------------------
def fetch_adzuna(keyword, pages, cfg, creds):
    """Search Adzuna within the configured radius of the configured place."""
    label = "Adzuna (Local)"
    jobs = []
    for page in range(1, pages + 1):
        url = ADZUNA_URL.format(page=page)
        params = {
            "app_id": creds["ADZUNA_APP_ID"],
            "app_key": creds["ADZUNA_APP_KEY"],
            "what": keyword,
            "where": cfg["adzuna_where"],
            "distance": cfg["radius_miles"],
            "results_per_page": 20,
            "salary_min": cfg["min_salary"],
            "content-type": "application/json",
        }
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code != 200:
                print_status(label, r.status_code, 0)
                continue
            data = r.json()
            results = data.get("results", [])
            print_status(label, r.status_code, len(results))
            if not results:
                break
            for j in results:
                description = clean(j.get("description", ""))
                location = clean(j.get("location", {}).get("display_name", ""))
                title = clean(j.get("title", ""))
                company = clean(j.get("company", {}).get("display_name", ""))
                url_link = clean(j.get("redirect_url", ""))
                jobs.append({
                    "source": label,
                    "title": title,
                    "company": company,
                    "location": location,
                    "salary": adzuna_salary(j),
                    "url": url_link,
                    "active_clearance_required": flag_clearance(title + " " + description),
                    "contingent": flag_contingent(description),
                    "excluded_location": flag_location(location, cfg),
                    "relevance_score": score_relevance(title, description, cfg),
                    "description_snippet": description[:300],
                })
        except Exception as e:
            print_request_error(label, e)
    return jobs


def fetch_adzuna_remote(keyword, pages, cfg, creds):
    """Search Adzuna nationally for remote roles, with no location filter."""
    label = "Adzuna (Remote)"
    jobs = []
    for page in range(1, pages + 1):
        url = ADZUNA_URL.format(page=page)
        params = {
            "app_id": creds["ADZUNA_APP_ID"],
            "app_key": creds["ADZUNA_APP_KEY"],
            "what": keyword + " remote",
            "results_per_page": 20,
            "salary_min": cfg["min_salary"],
            "content-type": "application/json",
        }
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code != 200:
                print_status(label, r.status_code, 0)
                continue
            data = r.json()
            results = data.get("results", [])
            print_status(label, r.status_code, len(results))
            if not results:
                break
            for j in results:
                description = clean(j.get("description", ""))
                location = clean(j.get("location", {}).get("display_name", ""))
                combined = (location + " " + description).lower()
                # Skip if remote is not mentioned at all
                if "remote" not in combined:
                    continue
                title = clean(j.get("title", ""))
                company = clean(j.get("company", {}).get("display_name", ""))
                url_link = clean(j.get("redirect_url", ""))
                jobs.append({
                    "source": label,
                    "title": title,
                    "company": company,
                    "location": location if location else "Remote",
                    "salary": adzuna_salary(j),
                    "url": url_link,
                    "active_clearance_required": flag_clearance(title + " " + description),
                    "contingent": flag_contingent(description),
                    "excluded_location": False,
                    "relevance_score": score_relevance(title, description, cfg),
                    "description_snippet": description[:300],
                })
        except Exception as e:
            print_request_error(label, e)
    return jobs


# -- USAJOBS ------------------------------------------------------------------
def fetch_usajobs(keyword, pages, cfg, creds):
    """Search USAJobs within the configured radius of the configured place."""
    label = "USAJobs"
    jobs = []
    headers = usajobs_headers(creds)
    for page in range(1, pages + 1):
        params = {
            "Keyword": keyword,
            "LocationName": cfg["usajobs_location_name"],
            "Radius": cfg["radius_miles"],
            "ResultsPerPage": 25,
            "Page": page,
            "RemunerationMinimumAmount": cfg["min_salary"],
            "Fields": "min",
        }
        try:
            r = requests.get(USAJOBS_URL, headers=headers, params=params, timeout=10)
            if r.status_code != 200:
                print_status(label, r.status_code, 0)
                continue
            data = r.json()
            items = data.get("SearchResult", {}).get("SearchResultItems", [])
            print_status(label, r.status_code, len(items))
            if not items:
                break
            for item in items:
                pos = item.get("MatchedObjectDescriptor", {})
                title = clean(pos.get("PositionTitle", ""))
                company = clean(pos.get("OrganizationName", ""))
                location_list = pos.get("PositionLocation", [])
                location = clean(location_list[0].get("LocationName", "") if location_list else "")
                url_link = clean(pos.get("PositionURI", ""))
                qualifications = clean(pos.get("QualificationSummary", ""))
                description = clean(pos.get("UserArea", {}).get("Details", {}).get("JobSummary", ""))
                full_text = title + " " + qualifications + " " + description
                jobs.append({
                    "source": label,
                    "title": title,
                    "company": company,
                    "location": location,
                    "salary": usajobs_salary(pos),
                    "url": url_link,
                    "active_clearance_required": flag_clearance(full_text),
                    "contingent": flag_contingent(full_text),
                    "excluded_location": flag_location(location, cfg),
                    "relevance_score": score_relevance(title, qualifications or description, cfg),
                    "description_snippet": (qualifications or description)[:300],
                })
        except Exception as e:
            print_request_error(label, e)
    return jobs


def fetch_usajobs_remote(keyword, pages, cfg, creds):
    """Search USAJobs nationally for positions marked as remote."""
    label = "USAJobs (Remote)"
    jobs = []
    headers = usajobs_headers(creds)
    for page in range(1, pages + 1):
        params = {
            "Keyword": keyword,
            "RemoteIndicator": "True",
            "ResultsPerPage": 25,
            "Page": page,
            "RemunerationMinimumAmount": cfg["min_salary"],
            "Fields": "min",
        }
        try:
            r = requests.get(USAJOBS_URL, headers=headers, params=params, timeout=10)
            if r.status_code != 200:
                print_status(label, r.status_code, 0)
                continue
            data = r.json()
            items = data.get("SearchResult", {}).get("SearchResultItems", [])
            print_status(label, r.status_code, len(items))
            if not items:
                break
            for item in items:
                pos = item.get("MatchedObjectDescriptor", {})
                title = clean(pos.get("PositionTitle", ""))
                company = clean(pos.get("OrganizationName", ""))
                url_link = clean(pos.get("PositionURI", ""))
                qualifications = clean(pos.get("QualificationSummary", ""))
                description = clean(pos.get("UserArea", {}).get("Details", {}).get("JobSummary", ""))
                full_text = title + " " + qualifications + " " + description
                jobs.append({
                    "source": label,
                    "title": title,
                    "company": company,
                    "location": "Remote",
                    "salary": usajobs_salary(pos),
                    "url": url_link,
                    "active_clearance_required": flag_clearance(full_text),
                    "contingent": flag_contingent(full_text),
                    "excluded_location": False,
                    "relevance_score": score_relevance(title, qualifications or description, cfg),
                    "description_snippet": (qualifications or description)[:300],
                })
        except Exception as e:
            print_request_error(label, e)
    return jobs


# -- CLI ----------------------------------------------------------------------
def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number")
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Aggregate job listings from Adzuna and USAJobs into a CSV.",
    )
    parser.add_argument(
        "--limit", type=positive_int, metavar="N",
        help="cap the number of result pages fetched per search, for every source",
    )
    parser.add_argument(
        "--query", metavar="KW",
        help="run this single keyword through all four searches instead of the config lists",
    )
    parser.add_argument(
        "--config", default="config.yaml", metavar="PATH",
        help="YAML settings file (default: ./config.yaml)",
    )
    parser.add_argument(
        "--env-file", default=None, metavar="PATH",
        help="env file holding the four API variables (default: ./.env, skipped if absent)",
    )
    parser.add_argument(
        "--output-dir", default="output", metavar="PATH",
        help="directory for the CSV, created if missing (default: ./output/)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    load_env(args.env_file)
    creds = read_credentials()
    cfg = load_config(args.config)

    output_dir = os.path.abspath(args.output_dir)
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as e:
        fail(f"could not create output directory ({type(e).__name__}): {output_dir}")

    pages_adzuna = cfg["pages_adzuna"]
    pages_usajobs = cfg["pages_usajobs"]
    if args.limit is not None:
        pages_adzuna = min(pages_adzuna, args.limit)
        pages_usajobs = min(pages_usajobs, args.limit)

    if args.query is not None:
        query = args.query.strip()
        if not query:
            fail("--query must not be empty")
        adzuna_local = adzuna_remote = usajobs = [query]
    else:
        adzuna_local = cfg["searches_adzuna_local"]
        adzuna_remote = cfg["searches_adzuna_remote"]
        usajobs = cfg["searches_usajobs"]

    all_jobs = []

    print("Fetching from Adzuna (local radius)...")
    for kw in adzuna_local:
        print(f"  Searching: {kw}")
        results = fetch_adzuna(kw, pages_adzuna, cfg, creds)
        print(f"    Found {len(results)} results")
        all_jobs.extend(results)

    print("\nFetching from Adzuna (Remote nationwide)...")
    for kw in adzuna_remote:
        print(f"  Searching: {kw}")
        results = fetch_adzuna_remote(kw, pages_adzuna, cfg, creds)
        print(f"    Found {len(results)} remote results")
        all_jobs.extend(results)

    print("\nFetching from USAJobs (local radius)...")
    for kw in usajobs:
        print(f"  Searching: {kw}")
        results = fetch_usajobs(kw, pages_usajobs, cfg, creds)
        print(f"    Found {len(results)} results")
        all_jobs.extend(results)

    print("\nFetching from USAJobs (Remote nationwide)...")
    for kw in usajobs:
        print(f"  Searching: {kw}")
        results = fetch_usajobs_remote(kw, pages_usajobs, cfg, creds)
        print(f"    Found {len(results)} remote results")
        all_jobs.extend(results)

    print(f"\nTotal before dedup: {len(all_jobs)}")
    all_jobs = [
        j for j in all_jobs
        if not is_excluded(j["title"], j["company"], j["location"], j["source"], cfg)
    ]
    print(f"After title/company exclusions: {len(all_jobs)}")
    all_jobs = dedupe(all_jobs)
    print(f"Total after dedup: {len(all_jobs)}")

    # Sort: clean jobs by relevance score descending, flagged jobs last
    def sort_key(j):
        flagged = int(j["active_clearance_required"]) + int(j["excluded_location"])
        return (flagged, -int(j["relevance_score"]))

    all_jobs.sort(key=sort_key)

    # Write CSV (always, header-only when there are no rows)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    outfile = os.path.join(output_dir, f"jobs_{timestamp}.csv")
    try:
        with open(outfile, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(all_jobs)
    except OSError as e:
        fail(f"could not write CSV ({type(e).__name__}): {outfile}")

    print(f"\nOutput saved to: {outfile}")
    print("\nSummary:")
    print(f"  Total jobs: {len(all_jobs)}")
    print(f"  Clean (no flags): {sum(1 for j in all_jobs if not j['active_clearance_required'] and not j['excluded_location'])}")
    print(f"  Active clearance flagged: {sum(1 for j in all_jobs if j['active_clearance_required'])}")
    print(f"  Excluded location: {sum(1 for j in all_jobs if j['excluded_location'])}")
    print(f"  Contingent roles: {sum(1 for j in all_jobs if j['contingent'])}")
    print(f"\nCSV saved to: {outfile}")

    return outfile


if __name__ == "__main__":
    main()
