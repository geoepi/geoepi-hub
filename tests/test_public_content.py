import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import yaml


SCRIPT = Path(__file__).parents[1] / "scripts" / "public_content.py"
SPEC = importlib.util.spec_from_file_location("public_content", SCRIPT)
public_content = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(public_content)


def valid_record(project_id="a-project", **overrides):
    record = {
        "schema_version": 1,
        "project_id": project_id,
        "publish": True,
        "content_status": "scaffold",
        "title": "A project",
        "short_summary": "A concise summary.",
        "abstract": "A short public description.",
        "themes": ["epidemiology"],
        "keywords": ["example"],
        "featured": False,
        "image": None,
        "links": [],
    }
    record.update(overrides)
    return record


def row(project_id, subproject_id, title):
    return {
        "project_id": project_id,
        "subproject_id": subproject_id,
        "title": title,
        "summary": f"Summary for {subproject_id}.",
        "repository": f"geoepi/{subproject_id}",
        "repository_visibility": "public",
        "status": "active",
        "lead_name": "Private lead",
        "current_focus": "Private operational focus",
        "compute": "Private compute",
        "next_milestone": "Private milestone",
        "metadata_stale": True,
    }


class PublicRecordValidationTests(unittest.TestCase):
    def assert_error(self, record, text):
        errors = public_content.validate_public_record(record, record["project_id"])
        self.assertTrue(any(text in error for error in errors), errors)

    def test_valid_record(self):
        self.assertEqual(
            public_content.validate_public_record(valid_record(), "a-project"), []
        )

    def test_project_folder_mismatch(self):
        errors = public_content.validate_public_record(
            valid_record(), "other-project"
        )
        self.assertTrue(any("expected 'other-project'" in error for error in errors))

    def test_invalid_schema_version(self):
        self.assert_error(valid_record(schema_version=2), "schema_version")

    def test_publish_must_be_boolean(self):
        self.assert_error(valid_record(publish="true"), "publish")

    def test_invalid_content_status(self):
        self.assert_error(valid_record(content_status="draft"), "content_status")

    def test_invalid_theme(self):
        self.assert_error(valid_record(themes=["virology"]), "invalid theme")

    def test_invalid_keywords_type(self):
        self.assert_error(valid_record(keywords="example"), "keywords must be a list")

    def test_invalid_image_url(self):
        self.assert_error(valid_record(image="http://example.org/image.png"), "image")

    def test_invalid_links(self):
        self.assert_error(
            valid_record(links=[{"label": "Resource", "url": "http://example.org"}]),
            "links[0].url",
        )

    def test_published_record_requires_public_text(self):
        self.assert_error(valid_record(short_summary=""), "short_summary")

    def test_public_record_is_optional_for_future_projects(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_dir = Path(temporary) / "future-project"
            project_dir.mkdir()
            records, errors = public_content.load_public_records(Path(temporary))
        self.assertEqual(records, [])
        self.assertEqual(errors, [])


class PublicFeedTests(unittest.TestCase):
    def test_publish_false_is_excluded(self):
        records = [valid_record("a-project"), valid_record("b-project", publish=False)]
        feed = public_content.build_public_research_feed(records, [])
        self.assertEqual([item["project_id"] for item in feed["projects"]], ["a-project"])

    def test_projects_and_subprojects_are_sorted(self):
        records = [valid_record("z-project"), valid_record("a-project")]
        rows = [
            row("z-project", "z-two", "Z two"),
            row("a-project", "a-two", "A two"),
            row("a-project", "a-one", "A one"),
        ]
        feed = public_content.build_public_research_feed(records, rows)
        self.assertEqual(
            [item["project_id"] for item in feed["projects"]], ["a-project", "z-project"]
        )
        self.assertEqual(
            [item["subproject_id"] for item in feed["projects"][0]["subprojects"]],
            ["a-one", "a-two"],
        )

    def test_scaffold_and_reviewed_statuses_are_preserved(self):
        records = [
            valid_record("a-project", content_status="scaffold"),
            valid_record("b-project", content_status="reviewed"),
        ]
        feed = public_content.build_public_research_feed(records, [])
        self.assertEqual(feed["projects"][0]["content_status"], "scaffold")
        self.assertEqual(feed["projects"][1]["content_status"], "reviewed")

    def test_subproject_public_fields_exclude_operational_metadata(self):
        feed = public_content.build_public_research_feed(
            [valid_record()], [row("a-project", "a-subproject", "A subproject")]
        )
        subproject = feed["projects"][0]["subprojects"][0]
        self.assertEqual(subproject["summary"], "Summary for a-subproject.")
        self.assertEqual(subproject["repository_url"], "https://github.com/geoepi/a-subproject")
        self.assertEqual(subproject["repository_visibility"], "public")
        for field in ("lead_name", "current_focus", "compute", "next_milestone", "metadata_stale"):
            self.assertNotIn(field, subproject)

    def test_unknown_repository_visibility_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "repository_visibility"):
            public_content.build_public_research_feed(
                [valid_record()],
                [row("a-project", "a-subproject", "A subproject") | {"repository_visibility": "unknown"}],
            )

    def test_json_is_deterministic(self):
        records = [valid_record()]
        rows = [row("a-project", "a-subproject", "A subproject")]
        first = public_content.build_public_research_json(
            public_content.build_public_research_feed(records, rows)
        )
        second = public_content.build_public_research_json(
            public_content.build_public_research_feed(records, rows)
        )
        self.assertEqual(first, second)

    def test_malformed_record_prevents_loading_complete_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            projects = Path(temporary)
            for project_id, record in (
                ("a-project", valid_record("a-project")),
                ("b-project", valid_record("b-project", themes=["invalid"])),
            ):
                project_dir = projects / project_id
                project_dir.mkdir()
                (project_dir / "public.yml").write_text(
                    yaml.safe_dump(record, sort_keys=False), encoding="utf-8"
                )
            records, errors = public_content.load_public_records(projects)
        self.assertEqual(records, [valid_record("a-project")])
        self.assertTrue(any("invalid theme" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
