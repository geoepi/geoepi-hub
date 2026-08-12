import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "update_hub.py"
SPEC = importlib.util.spec_from_file_location("update_hub", SCRIPT)
hub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hub)


TODAY = date(2026, 8, 12)


def valid_metadata(**overrides):
    metadata = {
        "schema_version": 1,
        "project_id": "nws-risk",
        "subproject_id": "nws-example",
        "title": "Example analysis",
        "summary": "A concise scientific summary.",
        "lead": {"name": "Example Lead", "github": "example"},
        "status": "active",
        "current_focus": "Prospective validation.",
        "repository": {"canonical": "geoepi/ExampleRepo"},
        "compute": ["Atlas"],
        "next_milestone": {
            "description": "Complete validation",
            "target": "2026-09-01",
        },
    }
    metadata.update(overrides)
    return metadata


def row(
    project_id="nws-risk",
    subproject_id="nws-example",
    status="active",
    target="2026-09-01",
    stale=False,
    overdue=False,
    paused_passed=False,
):
    return {
        "project_id": project_id,
        "subproject_id": subproject_id,
        "title": "Example analysis",
        "repository": f"geoepi/{subproject_id}",
        "lead_name": "Example Lead",
        "lead_github": "example",
        "status": status,
        "current_focus": "Prospective validation.",
        "compute": "Atlas",
        "next_milestone": "Complete validation",
        "milestone_target": target,
        "milestone_overdue": overdue,
        "metadata_last_updated": "2026-08-01",
        "metadata_stale": stale,
        "paused_review_passed": paused_passed,
    }


class MetadataValidationTests(unittest.TestCase):
    def validate(self, metadata):
        return hub.validate_metadata(
            metadata, "nws-risk", "nws-example", "geoepi/ExampleRepo"
        )

    def test_valid_registry_metadata_pair(self):
        self.assertEqual(self.validate(valid_metadata()), [])

    def test_uppercase_subproject_id_is_invalid(self):
        errors = self.validate(valid_metadata(subproject_id="NWS-Example"))
        self.assertTrue(any("invalid subproject_id" in error for error in errors))

    def test_registry_metadata_id_mismatch(self):
        errors = self.validate(valid_metadata(subproject_id="other-id"))
        self.assertIn("subproject_id is 'other-id'; expected 'nws-example'", errors)

    def test_canonical_repository_mismatch(self):
        errors = self.validate(
            valid_metadata(repository={"canonical": "geoepi/OtherRepo"})
        )
        self.assertTrue(any("repository.canonical" in error for error in errors))

    def test_invalid_status(self):
        errors = self.validate(valid_metadata(status="underway"))
        self.assertTrue(any("status must be one of" in error for error in errors))

    def test_missing_required_title_summary_and_lead(self):
        errors = self.validate(valid_metadata(title="", summary="", lead={}))
        self.assertIn("title is required", errors)
        self.assertIn("summary is required", errors)
        self.assertIn("lead.name is required", errors)

    def test_invalid_milestone_target(self):
        errors = self.validate(
            valid_metadata(
                next_milestone={"description": "Review", "target": "September"}
            )
        )
        self.assertIn("next_milestone.target must be an ISO date (YYYY-MM-DD)", errors)


class RegistryValidationTests(unittest.TestCase):
    def load(self, files):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for project, contents in files.items():
                folder = root / project
                folder.mkdir()
                (folder / "subprojects.yml").write_text(contents, encoding="utf-8")
            return hub.load_registries(root)

    def test_project_id_folder_mismatch(self):
        _, errors = self.load(
            {"nws-risk": "project_id: other-project\nsubprojects: []\n"}
        )
        self.assertTrue(any("does not match folder" in error for error in errors))

    def test_duplicate_subproject_ids(self):
        _, errors = self.load(
            {
                "nws-risk": """project_id: nws-risk
subprojects:
  - subproject_id: nws-example
    repository: geoepi/One
  - subproject_id: nws-example
    repository: geoepi/Two
"""
            }
        )
        self.assertTrue(any("duplicate subproject_id 'nws-example'" in e for e in errors))

    def test_duplicate_repositories_within_project(self):
        _, errors = self.load(
            {
                "nws-risk": """project_id: nws-risk
subprojects:
  - subproject_id: nws-one
    repository: geoepi/Example
  - subproject_id: nws-two
    repository: GEOEPI/example
"""
            }
        )
        self.assertTrue(any("duplicate canonical repository" in e for e in errors))

    def test_repository_with_incompatible_cross_project_identities(self):
        _, errors = self.load(
            {
                "nws-risk": """project_id: nws-risk
subprojects:
  - subproject_id: nws-one
    repository: geoepi/Example
""",
                "fmd-risk": """project_id: fmd-risk
subprojects:
  - subproject_id: fmd-one
    repository: geoepi/example
""",
            }
        )
        self.assertTrue(any("incompatible identities" in e for e in errors))


class DateAndStatusTests(unittest.TestCase):
    def test_stale_metadata_determination(self):
        self.assertTrue(hub.metadata_is_stale(date(2026, 6, 1), TODAY, 45))
        self.assertFalse(hub.metadata_is_stale(date(2026, 7, 1), TODAY, 45))

    def test_overdue_planned_and_active_milestones(self):
        for status in ("planned", "active"):
            metadata = valid_metadata(
                status=status,
                next_milestone={"description": "Review", "target": "2026-08-01"},
            )
            self.assertTrue(hub.milestone_overdue(metadata, TODAY))

    def test_paused_milestone_is_review_needed_not_overdue(self):
        metadata = valid_metadata(
            status="paused",
            next_milestone={"description": "Review", "target": "2026-08-01"},
        )
        self.assertFalse(hub.milestone_overdue(metadata, TODAY))
        self.assertTrue(hub.paused_review_passed(metadata, TODAY))

    def test_complete_and_archived_milestones_are_not_upcoming_or_overdue(self):
        for status in ("complete", "archived"):
            metadata = valid_metadata(
                status=status,
                next_milestone={"description": "Old date", "target": "2026-08-01"},
            )
            self.assertFalse(hub.milestone_overdue(metadata, TODAY))
            self.assertIsNone(hub.milestone_window(row(status=status), TODAY))

    def test_30_and_60_day_milestone_groups(self):
        self.assertEqual(
            hub.milestone_window(row(target="2026-09-01"), TODAY),
            "within 30 days",
        )
        self.assertEqual(
            hub.milestone_window(row(target="2026-10-01"), TODAY),
            "31-60 days",
        )


class ReportTests(unittest.TestCase):
    def test_summary_counts_and_project_sorting(self):
        rows = [
            row("z-project", "z-one", "paused"),
            row("a-project", "a-one", "active"),
            row("a-project", "a-two", "complete"),
        ]
        output = hub.build_portfolio_summary(rows, TODAY, 45)
        self.assertIn("- Projects represented: 2", output)
        self.assertIn("- Registered subprojects: 3", output)
        self.assertIn("- Active: 1", output)
        self.assertIn("- Paused: 1", output)
        self.assertIn("- Complete: 1", output)
        self.assertLess(output.index("| a-project |"), output.index("| z-project |"))

    def test_upcoming_groups_exclude_complete_and_archived(self):
        rows = [
            row(subproject_id="active-30", target="2026-09-01"),
            row(subproject_id="paused-60", status="paused", target="2026-10-01"),
            row(subproject_id="complete-30", status="complete", target="2026-09-01"),
            row(subproject_id="archived-60", status="archived", target="2026-10-01"),
        ]
        output = hub.build_portfolio_summary(rows, TODAY, 45)
        self.assertIn("active-30", output)
        self.assertIn("paused-60", output)
        self.assertNotIn("complete-30", output)
        self.assertNotIn("archived-60", output)

    def test_attention_needed_generation(self):
        rows = [
            row(subproject_id="overdue", target="2026-08-01", overdue=True),
            row(
                subproject_id="paused-review",
                status="paused",
                target="2026-08-01",
                paused_passed=True,
            ),
            row(subproject_id="stale", stale=True),
        ]
        output = hub.build_attention_needed(rows, 45)
        self.assertIn("## Overdue milestones", output)
        self.assertIn("## Paused work with review dates passed", output)
        self.assertIn("not ordinary overdue milestones", output)
        self.assertIn("## Metadata needing review", output)

    def test_no_attention_needed_case(self):
        output = hub.build_attention_needed([row()], 45)
        self.assertIn(
            "No portfolio items currently require automated attention.", output
        )
        self.assertNotIn("## Overdue milestones", output)

    def test_outputs_are_deterministic_for_unchanged_inputs(self):
        rows = [row("b-project", "b-one"), row("a-project", "a-one")]
        first = hub.build_outputs(rows, TODAY, 45)
        second = hub.build_outputs(rows, TODAY, 45)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
