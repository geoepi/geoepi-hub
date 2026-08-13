#!/usr/bin/env python3

import argparse
import base64
import csv
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import public_content


API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
STALE_DAYS = int(os.environ.get("STALE_DAYS", "45"))

STATUS_VALUES = ("planned", "active", "paused", "complete", "archived")
UPCOMING_STATUS_VALUES = {"planned", "active", "paused"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

FIELDNAMES = [
    "project_id",
    "subproject_id",
    "title",
    "repository",
    "repository_visibility",
    "lead_name",
    "lead_github",
    "status",
    "current_focus",
    "compute",
    "next_milestone",
    "milestone_target",
    "milestone_overdue",
    "metadata_last_updated",
    "metadata_stale",
]


def github_get(path, token=None):
    """GET JSON from the GitHub REST API."""
    token = token or os.environ.get("GH_READ_TOKEN")
    url = f"{API}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "geoepi-hub",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        headers=headers,
    )

    try:
        with urlopen(request) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"GitHub API request failed: HTTP {exc.code}") from exc


def read_remote_yaml(repo, path=".geoepi.yml"):
    """Read and parse a YAML file from a GitHub repository."""
    payload = github_get(f"/repos/{repo}/contents/{quote(path)}")
    if payload.get("encoding") != "base64":
        raise RuntimeError("unexpected GitHub content encoding")

    try:
        text = base64.b64decode(payload["content"]).decode("utf-8")
        data = yaml.safe_load(text)
    except (KeyError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise RuntimeError("invalid YAML content") from exc

    if not isinstance(data, dict):
        raise RuntimeError("YAML root must be a mapping")
    return data


def read_repository_metadata(repo):
    """Read authenticated repository metadata from the GitHub REST API."""
    metadata = github_get(f"/repos/{repo}")
    if not isinstance(metadata, dict):
        raise RuntimeError("repository metadata must be an object")
    return metadata


def normalize_repository_visibility(metadata):
    """Normalize GitHub repository metadata to a supported visibility value."""
    if not isinstance(metadata, dict):
        raise ValueError("repository metadata must be an object")

    visibility = metadata.get("visibility")
    if isinstance(visibility, str):
        normalized = visibility.strip().lower()
        if normalized in {"public", "private", "internal"}:
            return normalized

    private = metadata.get("private")
    if type(private) is bool:
        return "private" if private else "public"

    raise ValueError("repository visibility is missing or unknown")


def metadata_commit_date(repo):
    """Return the date .geoepi.yml last changed."""
    commits = github_get(f"/repos/{repo}/commits?path=.geoepi.yml&per_page=1")
    if not commits:
        return None
    stamp = commits[0]["commit"]["committer"]["date"]
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def md_escape(value):
    return as_text(value).replace("|", "\\|").replace("\n", " ").strip()


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def metadata_is_stale(changed, today=None, stale_days=STALE_DAYS):
    today = today or date.today()
    return changed is not None and (today - changed).days > stale_days


def milestone_overdue(metadata, today=None):
    today = today or date.today()
    milestone = metadata.get("next_milestone") or {}
    target = parse_date(milestone.get("target"))
    return bool(
        target
        and metadata.get("status") in {"planned", "active"}
        and target < today
    )


def paused_review_passed(metadata, today=None):
    today = today or date.today()
    milestone = metadata.get("next_milestone") or {}
    target = parse_date(milestone.get("target"))
    return bool(target and metadata.get("status") == "paused" and target < today)


def milestone_window(row, today=None):
    today = today or date.today()
    if row["status"] not in UPCOMING_STATUS_VALUES:
        return None
    target = parse_date(row["milestone_target"])
    if target is None:
        return None
    days = (target - today).days
    if 0 <= days <= 30:
        return "within 30 days"
    if 31 <= days <= 60:
        return "31-60 days"
    return None


def validate_metadata(metadata, expected_project, expected_subproject, expected_repo):
    errors = []
    if metadata.get("schema_version") != 1:
        errors.append("schema_version must equal 1")

    project_id = metadata.get("project_id")
    subproject_id = metadata.get("subproject_id")
    if project_id != expected_project:
        errors.append(f"project_id is {project_id!r}; expected {expected_project!r}")
    if subproject_id != expected_subproject:
        errors.append(
            f"subproject_id is {subproject_id!r}; expected {expected_subproject!r}"
        )
    if project_id and not ID_RE.fullmatch(project_id):
        errors.append(f"invalid project_id: {project_id!r}")
    if subproject_id and not ID_RE.fullmatch(subproject_id):
        errors.append(f"invalid subproject_id: {subproject_id!r}")

    if not isinstance(metadata.get("title"), str) or not metadata["title"].strip():
        errors.append("title is required")
    if not isinstance(metadata.get("summary"), str) or not metadata["summary"].strip():
        errors.append("summary is required")

    lead = metadata.get("lead")
    if not isinstance(lead, dict):
        errors.append("lead must be a mapping")
    elif not isinstance(lead.get("name"), str) or not lead["name"].strip():
        errors.append("lead.name is required")

    if metadata.get("status") not in STATUS_VALUES:
        errors.append("status must be one of: " + ", ".join(STATUS_VALUES))

    repository = metadata.get("repository")
    if not isinstance(repository, dict):
        errors.append("repository must be a mapping")
    else:
        canonical = repository.get("canonical")
        if canonical != expected_repo:
            errors.append(
                f"repository.canonical is {canonical!r}; expected {expected_repo!r}"
            )
        if canonical and not REPO_RE.fullmatch(canonical):
            errors.append(f"invalid canonical repository: {canonical!r}")
        upstream = repository.get("upstream")
        if upstream and not REPO_RE.fullmatch(upstream):
            errors.append(f"invalid upstream repository: {upstream!r}")

    compute = metadata.get("compute")
    if compute is not None and not isinstance(compute, list):
        errors.append("compute must be a YAML list")

    milestone = metadata.get("next_milestone")
    if milestone is not None:
        if not isinstance(milestone, dict):
            errors.append("next_milestone must be a mapping")
        else:
            description = milestone.get("description")
            target = milestone.get("target")
            if target is not None and not description:
                errors.append(
                    "next_milestone.description is required when target is supplied"
                )
            if target is not None and parse_date(target) is None:
                errors.append("next_milestone.target must be an ISO date (YYYY-MM-DD)")
    return errors


def load_registries(projects_dir=Path("projects")):
    paths = sorted(projects_dir.glob("*/subprojects.yml"))
    if not paths:
        return [], ["No projects/*/subprojects.yml files found"]

    entries = []
    errors = []
    identities_by_repo = defaultdict(set)

    for path in paths:
        label = path.as_posix()
        try:
            registry = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            errors.append(f"{label}: cannot read registry: {exc}")
            continue
        if not isinstance(registry, dict):
            errors.append(f"{label}: registry root must be a mapping")
            continue

        project_id = registry.get("project_id")
        folder_id = path.parent.name
        if project_id != folder_id:
            errors.append(
                f"{label}: project_id {project_id!r} does not match folder {folder_id!r}"
            )
        if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
            errors.append(f"{label}: invalid project_id {project_id!r}")

        subprojects = registry.get("subprojects")
        if not isinstance(subprojects, list):
            errors.append(f"{label}: subprojects must be a list")
            continue

        seen_ids = set()
        seen_repos = set()
        for entry in subprojects:
            if not isinstance(entry, dict):
                errors.append(f"{label}: invalid subproject entry")
                continue
            subproject_id = entry.get("subproject_id")
            repo = entry.get("repository")
            if not subproject_id or not repo:
                errors.append(
                    f"{label}: every entry requires subproject_id and repository"
                )
                continue
            if not isinstance(subproject_id, str) or not ID_RE.fullmatch(subproject_id):
                errors.append(f"{label}: invalid subproject_id {subproject_id!r}")
            if subproject_id in seen_ids:
                errors.append(f"{label}: duplicate subproject_id {subproject_id!r}")
            seen_ids.add(subproject_id)

            repo_key = repo.casefold() if isinstance(repo, str) else repo
            if repo_key in seen_repos:
                errors.append(f"{label}: duplicate canonical repository {repo!r}")
            seen_repos.add(repo_key)
            if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
                errors.append(f"{label}: invalid repository {repo!r}")
                continue

            identity = (project_id, subproject_id)
            identities_by_repo[repo.casefold()].add(identity)
            entries.append(
                {
                    "path": label,
                    "project_id": project_id,
                    "subproject_id": subproject_id,
                    "repository": repo,
                }
            )

    for repo_key, identities in sorted(identities_by_repo.items()):
        if len(identities) > 1:
            rendered = ", ".join(f"{p}/{s}" for p, s in sorted(identities))
            errors.append(
                f"canonical repository {repo_key!r} has incompatible identities: {rendered}"
            )

    entries.sort(key=lambda item: (item["project_id"] or "", item["subproject_id"]))
    return entries, errors


def build_row(
    entry,
    metadata,
    changed,
    today=None,
    stale_days=STALE_DAYS,
    repository_visibility=None,
):
    today = today or date.today()
    lead = metadata.get("lead") or {}
    compute = metadata.get("compute") or []
    milestone = metadata.get("next_milestone") or {}
    return {
        "project_id": entry["project_id"],
        "subproject_id": entry["subproject_id"],
        "title": metadata.get("title", ""),
        "summary": metadata.get("summary", ""),
        "repository": entry["repository"],
        "repository_visibility": repository_visibility,
        "lead_name": lead.get("name", ""),
        "lead_github": lead.get("github", ""),
        "status": metadata.get("status", ""),
        "current_focus": metadata.get("current_focus", ""),
        "compute": "; ".join(as_text(item) for item in compute),
        "next_milestone": milestone.get("description", ""),
        "milestone_target": as_text(milestone.get("target")),
        "milestone_overdue": milestone_overdue(metadata, today),
        "metadata_last_updated": changed.isoformat() if changed else "",
        "metadata_stale": metadata_is_stale(changed, today, stale_days),
        "paused_review_passed": paused_review_passed(metadata, today),
    }


def collect_rows(entries, today=None, stale_days=STALE_DAYS, metadata_reader=None,
                 commit_date_reader=None, repository_metadata_reader=None):
    today = today or date.today()
    metadata_reader = metadata_reader or read_remote_yaml
    commit_date_reader = commit_date_reader or metadata_commit_date
    repository_metadata_reader = repository_metadata_reader or read_repository_metadata
    rows = []
    errors = []
    visibility_by_repo = {}
    visibility_errors = {}

    for entry in entries:
        project_id = entry["project_id"]
        subproject_id = entry["subproject_id"]
        repo = entry["repository"]
        context = f"{project_id}/{subproject_id} ({repo})"
        if repo in visibility_errors:
            errors.append(f"{context}: cannot determine repository visibility: {visibility_errors[repo]}")
            continue
        if repo not in visibility_by_repo:
            try:
                repository_metadata = repository_metadata_reader(repo)
                visibility_by_repo[repo] = normalize_repository_visibility(
                    repository_metadata
                )
            except Exception as exc:
                visibility_errors[repo] = str(exc)
                errors.append(f"{context}: cannot determine repository visibility: {exc}")
                continue
        repository_visibility = visibility_by_repo[repo]
        try:
            metadata = metadata_reader(repo)
        except Exception as exc:
            errors.append(f"{context}: cannot read .geoepi.yml: {exc}")
            continue

        item_errors = validate_metadata(metadata, project_id, subproject_id, repo)
        if item_errors:
            errors.extend(f"{context}: {error}" for error in item_errors)
            continue

        try:
            changed = commit_date_reader(repo)
        except Exception as exc:
            errors.append(f"{context}: cannot determine metadata change date: {exc}")
            continue
        if changed is None:
            errors.append(f"{context}: no commit history found for .geoepi.yml")
            continue

        rows.append(
            build_row(
                entry,
                metadata,
                changed,
                today,
                stale_days,
                repository_visibility,
            )
        )

    rows.sort(key=lambda row: (row["project_id"], row["subproject_id"]))
    return rows, errors


def repo_link(row):
    repo = md_escape(row["repository"])
    return f"[{repo}](https://github.com/{row['repository']})"


def build_status_csv(rows):
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: row[key] for key in FIELDNAMES} for row in rows)
    return handle.getvalue()


def build_status_markdown(rows, stale_days=STALE_DAYS):
    lines = [
        "# GeoEpi subproject status",
        "",
        "> Generated from the canonical repositories' `.geoepi.yml` files. Do not edit manually.",
        "",
        f"Metadata are flagged as stale after {stale_days} days without a change.",
        "",
        "| Project | Subproject | Status | Lead | Current focus | Compute | Next milestone | Metadata | Repository |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lead = md_escape(row["lead_name"])
        if row["lead_github"]:
            lead += f" (@{md_escape(row['lead_github'])})"
        milestone = md_escape(row["next_milestone"])
        if row["milestone_target"]:
            milestone += f" ({md_escape(row['milestone_target'])})"
        if row["milestone_overdue"]:
            milestone += " **OVERDUE**"
        metadata_state = md_escape(row["metadata_last_updated"])
        if row["metadata_stale"]:
            metadata_state += " **STALE**"
        lines.append(
            "| "
            + " | ".join(
                [
                    md_escape(row["project_id"]),
                    md_escape(row["subproject_id"]),
                    md_escape(row["status"]),
                    lead,
                    md_escape(row["current_focus"]),
                    md_escape(row["compute"]),
                    milestone,
                    metadata_state,
                    repo_link(row),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def milestone_table(rows):
    lines = [
        "| Project | Subproject | Milestone | Target | Status | Repository |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    md_escape(row["project_id"]),
                    md_escape(row["subproject_id"]),
                    md_escape(row["next_milestone"]),
                    md_escape(row["milestone_target"]),
                    md_escape(row["status"]),
                    repo_link(row),
                ]
            )
            + " |"
        )
    return lines


def build_portfolio_summary(rows, today=None, stale_days=STALE_DAYS):
    today = today or date.today()
    counts = Counter(row["status"] for row in rows)
    projects = sorted({row["project_id"] for row in rows})
    lines = [
        "# GeoEpi portfolio summary",
        "",
        "> Generated from registered subproject `.geoepi.yml` metadata.",
        "> Do not edit manually.",
        "",
        "## Portfolio totals",
        "",
        f"- Projects represented: {len(projects)}",
        f"- Registered subprojects: {len(rows)}",
    ]
    lines.extend(f"- {status.title()}: {counts[status]}" for status in STATUS_VALUES)
    lines.extend(
        [
            "",
            "## Status by project",
            "",
            "| Project | Planned | Active | Paused | Complete | Archived | Total |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for project_id in projects:
        project_counts = Counter(
            row["status"] for row in rows if row["project_id"] == project_id
        )
        values = [project_counts[status] for status in STATUS_VALUES]
        lines.append(
            f"| {md_escape(project_id)} | "
            + " | ".join(str(value) for value in values)
            + f" | {sum(values)} |"
        )

    upcoming = {"within 30 days": [], "31-60 days": []}
    for row in rows:
        group = milestone_window(row, today)
        if group:
            upcoming[group].append(row)
    for group in upcoming:
        upcoming[group].sort(
            key=lambda row: (
                parse_date(row["milestone_target"]),
                row["project_id"],
                row["subproject_id"],
            )
        )

    lines.extend(["", "## Upcoming milestones"])
    for title in ("Within 30 days", "31-60 days"):
        key = title.lower()
        lines.extend(["", f"### {title}", ""])
        if upcoming[key]:
            lines.extend(milestone_table(upcoming[key]))
        else:
            lines.append("No milestones in this window.")

    stale = sorted(
        (row for row in rows if row["metadata_stale"]),
        key=lambda row: (row["project_id"], row["subproject_id"]),
    )
    lines.extend(
        [
            "",
            "## Metadata freshness",
            "",
            f"Metadata are considered stale after {stale_days} days without a change.",
            "",
            f"Stale metadata records: {len(stale)}",
        ]
    )
    if stale:
        lines.append("")
        lines.extend(
            f"- {md_escape(row['project_id'])}/{md_escape(row['subproject_id'])}: {repo_link(row)}"
            for row in stale
        )
    lines.append("")
    return "\n".join(lines)


def build_attention_needed(rows, stale_days=STALE_DAYS):
    overdue = sorted(
        (row for row in rows if row["milestone_overdue"]),
        key=lambda row: (
            parse_date(row["milestone_target"]),
            row["project_id"],
            row["subproject_id"],
        ),
    )
    paused = sorted(
        (row for row in rows if row.get("paused_review_passed")),
        key=lambda row: (
            parse_date(row["milestone_target"]),
            row["project_id"],
            row["subproject_id"],
        ),
    )
    stale = sorted(
        (row for row in rows if row["metadata_stale"]),
        key=lambda row: (row["project_id"], row["subproject_id"]),
    )
    lines = [
        "# GeoEpi attention needed",
        "",
        "> Generated from registered subproject `.geoepi.yml` metadata.",
        "> Do not edit manually.",
    ]
    if not overdue and not paused and not stale:
        lines.extend(["", "No portfolio items currently require automated attention.", ""])
        return "\n".join(lines)

    if overdue:
        lines.extend(["", "## Overdue milestones", ""])
        lines.extend(milestone_table(overdue))
    if paused:
        lines.extend(["", "## Paused work with review dates passed", ""])
        lines.append(
            "These dates are review points for paused work, not ordinary overdue milestones."
        )
        lines.append("")
        lines.extend(milestone_table(paused))
    if stale:
        lines.extend(["", "## Metadata needing review", ""])
        lines.append(f"Metadata are considered stale after {stale_days} days without a change.")
        lines.append("")
        lines.extend(
            f"- {md_escape(row['project_id'])}/{md_escape(row['subproject_id'])}: "
            f"last changed {md_escape(row['metadata_last_updated'])}; {repo_link(row)}"
            for row in stale
        )
    lines.append("")
    return "\n".join(lines)


def build_outputs(rows, today=None, stale_days=STALE_DAYS, public_records=None):
    outputs = {
        "subproject-status.csv": build_status_csv(rows),
        "subproject-status.md": build_status_markdown(rows, stale_days),
        "portfolio-summary.md": build_portfolio_summary(rows, today, stale_days),
        "attention-needed.md": build_attention_needed(rows, stale_days),
    }
    if public_records is not None:
        feed = public_content.build_public_research_feed(public_records, rows)
        outputs["public-research.json"] = public_content.build_public_research_json(feed)
        outputs["public-research.md"] = public_content.build_public_research_markdown(feed)
    return outputs


def write_outputs(outputs, generated=Path("generated")):
    generated.mkdir(exist_ok=True)
    paths = []
    for name, content in outputs.items():
        path = generated / name
        path.write_text(content, encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def write_step_summary(outputs, errors):
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    with open(summary_file, "a", encoding="utf-8") as handle:
        if errors:
            handle.write("## Hub validation errors\n\n")
            for error in errors:
                handle.write(f"- {error}\n")
        else:
            handle.write(outputs["portfolio-summary.md"])
            handle.write("\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Validate and generate GeoEpi Hub status. "
            "Use --validate-local for credential-free PR checks, "
            "--validate-only for complete live validation without writing, "
            "or no flag for live validation and generation."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--validate-local",
        action="store_true",
        help=(
            "validate local Hub registries only; no network, credentials, "
            "remote metadata, or generated-file writes"
        ),
    )
    mode.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "perform complete live remote validation and report generation "
            "without writing generated files"
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    entries, errors = load_registries()
    public_records, public_errors = public_content.load_public_records()
    errors.extend(public_errors)

    if args.validate_local:
        if errors:
            print_validation_errors(errors)
            return 1
        print(f"Validated {len(entries)} registered subproject entries locally.")
        print("Local Hub validation passed.")
        return 0

    rows = []
    if not errors:
        rows, remote_errors = collect_rows(entries)
        errors.extend(remote_errors)

    outputs = build_outputs(rows, public_records=public_records) if not errors else {}
    write_step_summary(outputs, errors)
    if errors:
        print("\nHub validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"Validated {len(rows)} subproject(s).")
    if args.validate_only:
        print("Generation logic completed without writing generated files.")
    else:
        for path in write_outputs(outputs):
            print(f"Wrote {path}")
    return 0


def print_validation_errors(errors):
    print("\nHub validation failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
