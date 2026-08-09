#!/usr/bin/env python3

import base64
import csv
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml


API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
TOKEN = os.environ["GH_READ_TOKEN"]
STALE_DAYS = int(os.environ.get("STALE_DAYS", "45"))

STATUS_VALUES = {
    "planned",
    "active",
    "paused",
    "complete",
    "archived",
}

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REPO_RE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)


def github_get(path):
    """GET JSON from the GitHub REST API."""
    url = f"{API}{path}"

    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "geoepi-hub",
        },
    )

    try:
        with urlopen(request) as response:
            import json
            return json.load(response)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API request failed: {exc.code} {url}\n{body}"
        ) from exc


def read_remote_yaml(repo, path=".geoepi.yml"):
    """Read and parse a YAML file from a GitHub repository."""
    payload = github_get(
        f"/repos/{repo}/contents/{path}"
    )

    if payload.get("encoding") != "base64":
        raise RuntimeError(
            f"{repo}/{path}: unexpected GitHub content encoding"
        )

    text = base64.b64decode(
        payload["content"]
    ).decode("utf-8")

    data = yaml.safe_load(text)

    if not isinstance(data, dict):
        raise RuntimeError(
            f"{repo}/{path}: YAML root must be a mapping"
        )

    return data


def metadata_commit_date(repo):
    """Return the date .geoepi.yml last changed."""
    commits = github_get(
        f"/repos/{repo}/commits?path=.geoepi.yml&per_page=1"
    )

    if not commits:
        return None

    stamp = commits[0]["commit"]["committer"]["date"]
    return datetime.fromisoformat(
        stamp.replace("Z", "+00:00")
    ).date()


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def md_escape(value):
    return (
        as_text(value)
        .replace("|", "\\|")
        .replace("\n", " ")
        .strip()
    )


def validate_metadata(
    metadata,
    expected_project,
    expected_subproject,
    expected_repo,
):
    errors = []

    if metadata.get("schema_version") != 1:
        errors.append("schema_version must equal 1")

    project_id = metadata.get("project_id")
    subproject_id = metadata.get("subproject_id")

    if project_id != expected_project:
        errors.append(
            f"project_id is {project_id!r}; "
            f"expected {expected_project!r}"
        )

    if subproject_id != expected_subproject:
        errors.append(
            f"subproject_id is {subproject_id!r}; "
            f"expected {expected_subproject!r}"
        )

    if project_id and not ID_RE.match(project_id):
        errors.append(
            f"invalid project_id: {project_id!r}"
        )

    if subproject_id and not ID_RE.match(subproject_id):
        errors.append(
            f"invalid subproject_id: {subproject_id!r}"
        )

    if not metadata.get("title"):
        errors.append("title is required")

    if not metadata.get("summary"):
        errors.append("summary is required")

    lead = metadata.get("lead")

    if not isinstance(lead, dict):
        errors.append("lead must be a mapping")
    elif not lead.get("name"):
        errors.append("lead.name is required")

    status = metadata.get("status")

    if status not in STATUS_VALUES:
        errors.append(
            "status must be one of: "
            + ", ".join(sorted(STATUS_VALUES))
        )

    repository = metadata.get("repository")

    if not isinstance(repository, dict):
        errors.append("repository must be a mapping")
    else:
        canonical = repository.get("canonical")

        if canonical != expected_repo:
            errors.append(
                f"repository.canonical is {canonical!r}; "
                f"expected {expected_repo!r}"
            )

        if canonical and not REPO_RE.match(canonical):
            errors.append(
                f"invalid canonical repository: {canonical!r}"
            )

        upstream = repository.get("upstream")
        if upstream and not REPO_RE.match(upstream):
            errors.append(
                f"invalid upstream repository: {upstream!r}"
            )

    compute = metadata.get("compute")

    if compute is not None and not isinstance(compute, list):
        errors.append("compute must be a YAML list")

    milestone = metadata.get("next_milestone")

    if milestone is not None:
        if not isinstance(milestone, dict):
            errors.append(
                "next_milestone must be a mapping"
            )
        elif (
            milestone.get("target") is not None
            and not milestone.get("description")
        ):
            errors.append(
                "next_milestone.description is required "
                "when target is supplied"
            )

    return errors


def milestone_overdue(metadata):
    milestone = metadata.get("next_milestone") or {}
    target = milestone.get("target")

    if not target:
        return False

    if isinstance(target, datetime):
        target = target.date()
    elif isinstance(target, str):
        try:
            target = date.fromisoformat(target)
        except ValueError:
            return False

    if not isinstance(target, date):
        return False

    return (
        metadata.get("status") in {"planned", "active"}
        and target < date.today()
    )


def main():
    registries = sorted(
        Path("projects").glob("*/subprojects.yml")
    )

    if not registries:
        raise RuntimeError(
            "No projects/*/subprojects.yml files found"
        )

    rows = []
    errors = []

    for registry_path in registries:
        registry = yaml.safe_load(
            registry_path.read_text(encoding="utf-8")
        )

        if not isinstance(registry, dict):
            errors.append(
                f"{registry_path}: registry root must be a mapping"
            )
            continue

        project_id = registry.get("project_id")
        folder_id = registry_path.parent.name

        if project_id != folder_id:
            errors.append(
                f"{registry_path}: project_id {project_id!r} "
                f"does not match folder {folder_id!r}"
            )

        subprojects = registry.get("subprojects")

        if not isinstance(subprojects, list):
            errors.append(
                f"{registry_path}: subprojects must be a list"
            )
            continue

        for entry in subprojects:
            if not isinstance(entry, dict):
                errors.append(
                    f"{registry_path}: invalid subproject entry"
                )
                continue

            subproject_id = entry.get("subproject_id")
            repo = entry.get("repository")

            if not subproject_id or not repo:
                errors.append(
                    f"{registry_path}: every entry requires "
                    "subproject_id and repository"
                )
                continue

            if not REPO_RE.match(repo):
                errors.append(
                    f"{registry_path}: invalid repository {repo!r}"
                )
                continue

            try:
                metadata = read_remote_yaml(repo)
            except Exception as exc:
                errors.append(
                    f"{project_id}/{subproject_id}: "
                    f"cannot read {repo}/.geoepi.yml: {exc}"
                )
                continue

            item_errors = validate_metadata(
                metadata,
                project_id,
                subproject_id,
                repo,
            )

            if item_errors:
                for error in item_errors:
                    errors.append(
                        f"{project_id}/{subproject_id}: {error}"
                    )
                continue

            try:
                changed = metadata_commit_date(repo)
            except Exception as exc:
                errors.append(
                    f"{project_id}/{subproject_id}: "
                    f"cannot determine metadata change date: {exc}"
                )
                continue

            stale = False
            if changed:
                stale = (
                    date.today() - changed
                ).days > STALE_DAYS

            lead = metadata.get("lead") or {}
            compute = metadata.get("compute") or []
            milestone = (
                metadata.get("next_milestone") or {}
            )

            rows.append(
                {
                    "project_id": project_id,
                    "subproject_id": subproject_id,
                    "title": metadata.get("title", ""),
                    "repository": repo,
                    "lead_name": lead.get("name", ""),
                    "lead_github": lead.get("github", ""),
                    "status": metadata.get("status", ""),
                    "current_focus": metadata.get(
                        "current_focus", ""
                    ),
                    "compute": "; ".join(
                        as_text(x) for x in compute
                    ),
                    "next_milestone": milestone.get(
                        "description", ""
                    ),
                    "milestone_target": as_text(
                        milestone.get("target")
                    ),
                    "milestone_overdue": milestone_overdue(
                        metadata
                    ),
                    "metadata_last_updated": (
                        changed.isoformat()
                        if changed
                        else ""
                    ),
                    "metadata_stale": stale,
                }
            )

    rows.sort(
        key=lambda x: (
            x["project_id"],
            x["subproject_id"],
        )
    )

    generated = Path("generated")
    generated.mkdir(exist_ok=True)

    fieldnames = [
        "project_id",
        "subproject_id",
        "title",
        "repository",
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

    csv_path = generated / "subproject-status.csv"

    with csv_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# GeoEpi subproject status",
        "",
        "> Generated from the canonical repositories' "
        "`.geoepi.yml` files. Do not edit manually.",
        "",
        f"Metadata are flagged as stale after "
        f"{STALE_DAYS} days without a change.",
        "",
        "| Project | Subproject | Status | Lead | "
        "Current focus | Compute | Next milestone | "
        "Metadata | Repository |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for row in rows:
        lead = md_escape(row["lead_name"])

        if row["lead_github"]:
            lead += (
                f" (@{md_escape(row['lead_github'])})"
            )

        milestone = md_escape(
            row["next_milestone"]
        )

        if row["milestone_target"]:
            milestone += (
                f" ({md_escape(row['milestone_target'])})"
            )

        if row["milestone_overdue"]:
            milestone += " **OVERDUE**"

        metadata_state = md_escape(
            row["metadata_last_updated"]
        )

        if row["metadata_stale"]:
            metadata_state += " **STALE**"

        repo_link = (
            f"[{md_escape(row['repository'])}]"
            f"(https://github.com/{row['repository']})"
        )

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
                    repo_link,
                ]
            )
            + " |"
        )

    lines.append("")

    markdown = "\n".join(lines)

    md_path = generated / "subproject-status.md"
    md_path.write_text(
        markdown,
        encoding="utf-8",
    )

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")

    if summary_file:
        with open(
            summary_file,
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write(markdown)
            handle.write("\n")

            if errors:
                handle.write(
                    "\n## Validation errors\n\n"
                )
                for error in errors:
                    handle.write(f"- {error}\n")

    if errors:
        print(
            "\nHub validation failed:",
            file=sys.stderr,
        )
        for error in errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )
        return 1

    print(
        f"Validated {len(rows)} subproject(s)."
    )
    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
