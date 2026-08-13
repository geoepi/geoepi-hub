#!/usr/bin/env python3

"""Validate and build the Hub's public research content feed."""

from collections import defaultdict
from pathlib import Path
import json
import re
from urllib.parse import urlparse

import yaml


PUBLIC_SCHEMA_VERSION = 1
CONTENT_STATUS_VALUES = {"scaffold", "reviewed"}
THEME_VALUES = {"geography", "epidemiology", "modeling", "ecology"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _https_url(value):
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def validate_public_record(record, expected_project_id=None):
    """Return a list of validation errors for one public.yml record."""
    errors = []
    if not isinstance(record, dict):
        return ["record root must be a mapping"]

    if record.get("schema_version") != PUBLIC_SCHEMA_VERSION:
        errors.append("schema_version must equal 1")

    project_id = record.get("project_id")
    if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
        errors.append(f"invalid project_id: {project_id!r}")
    if expected_project_id and project_id != expected_project_id:
        errors.append(
            f"project_id is {project_id!r}; expected {expected_project_id!r}"
        )

    if type(record.get("publish")) is not bool:
        errors.append("publish must be a boolean")
    if record.get("content_status") not in CONTENT_STATUS_VALUES:
        errors.append("content_status must be scaffold or reviewed")

    publish = record.get("publish") is True
    for field in ("title", "short_summary", "abstract"):
        value = record.get(field)
        if not isinstance(value, str):
            errors.append(f"{field} must be a string")
        elif publish and not value.strip():
            errors.append(f"{field} is required when publish=true")

    themes = record.get("themes")
    if not isinstance(themes, list):
        errors.append("themes must be a list")
    else:
        for theme in themes:
            if theme not in THEME_VALUES:
                errors.append(f"invalid theme: {theme!r}")

    keywords = record.get("keywords")
    if not isinstance(keywords, list):
        errors.append("keywords must be a list")
    else:
        for keyword in keywords:
            if not isinstance(keyword, str):
                errors.append("keywords must contain only strings")

    if type(record.get("featured")) is not bool:
        errors.append("featured must be a boolean")

    image = record.get("image")
    if image is not None and not _https_url(image):
        errors.append("image must be null or an HTTPS URL")

    links = record.get("links")
    if not isinstance(links, list):
        errors.append("links must be a list")
    else:
        for index, link in enumerate(links):
            if not isinstance(link, dict):
                errors.append(f"links[{index}] must be a mapping")
                continue
            if not isinstance(link.get("label"), str) or not link["label"].strip():
                errors.append(f"links[{index}].label is required")
            if not _https_url(link.get("url")):
                errors.append(f"links[{index}].url must be an HTTPS URL")

    return errors


def load_public_records(projects_dir=Path("projects")):
    """Load and validate public.yml records present in the Hub project tree.

    A record is required for the current public-project collection, but the
    file remains optional for future projects until they are ready to publish.
    """
    projects_dir = Path(projects_dir)
    records = []
    errors = []
    project_dirs = sorted(path for path in projects_dir.iterdir() if path.is_dir())
    if not project_dirs:
        return [], [f"{projects_dir}: no project directories found"]

    for project_dir in project_dirs:
        path = project_dir / "public.yml"
        label = path.as_posix()
        if not path.exists():
            continue
        try:
            record = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            errors.append(f"{label}: cannot read YAML: {exc}")
            continue
        record_errors = validate_public_record(record, project_dir.name)
        errors.extend(f"{label}: {error}" for error in record_errors)
        if not record_errors:
            records.append(record)

    records.sort(key=lambda record: record["project_id"])
    return records, errors


def _subproject_record(row):
    repository = row["repository"]
    return {
        "subproject_id": row["subproject_id"],
        "title": row["title"].strip(),
        "summary": row["summary"].strip(),
        "repository": repository,
        "repository_url": f"https://github.com/{repository}",
        "status": row["status"],
    }


def build_public_research_feed(records, rows, source="geoepi/geoepi-hub"):
    """Build a deterministic public feed from validated records and live rows."""
    rows_by_project = defaultdict(list)
    for row in rows:
        rows_by_project[row["project_id"]].append(row)

    projects = []
    for record in sorted(records, key=lambda item: item["project_id"]):
        if not record["publish"]:
            continue
        subprojects = [
            _subproject_record(row)
            for row in sorted(
                rows_by_project.get(record["project_id"], []),
                key=lambda item: item["subproject_id"],
            )
        ]
        projects.append(
            {
                "project_id": record["project_id"],
                "title": record["title"].strip(),
                "short_summary": record["short_summary"].strip(),
                "abstract": record["abstract"].strip(),
                "content_status": record["content_status"],
                "themes": list(record["themes"]),
                "keywords": list(record["keywords"]),
                "featured": record["featured"],
                "image": record["image"],
                "links": [dict(link) for link in record["links"]],
                "hub_url": (
                    "https://github.com/geoepi/geoepi-hub/tree/main/projects/"
                    f"{record['project_id']}"
                ),
                "subprojects": subprojects,
            }
        )

    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "source": source,
        "projects": projects,
    }


def build_public_research_json(feed):
    return json.dumps(feed, ensure_ascii=False, indent=2) + "\n"


def build_public_research_markdown(feed):
    lines = [
        "# GeoEpi public research feed",
        "",
        "> Generated from `projects/*/public.yml` and live canonical `.geoepi.yml` metadata. Do not edit manually.",
        "",
    ]
    for project in feed["projects"]:
        lines.extend(
            [
                f"## {project['title']} (`{project['project_id']}`)",
                "",
                f"- Content status: `{project['content_status']}`",
                "- Published: `true`",
                f"- Themes: {', '.join(project['themes']) or 'None'}",
                f"- Subprojects: {len(project['subprojects'])}",
                f"- Hub record: {project['hub_url']}",
                "",
                project["short_summary"],
                "",
            ]
        )
    return "\n".join(lines)
