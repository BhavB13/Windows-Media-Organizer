import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from duplicate_transfer_manager.core import AppSettings, CancellationToken, OperationCancelled, ServiceError
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.sorting import (
    Association,
    ConflictPolicy,
    ConditionField,
    ConditionOperator,
    FileMetadata,
    MatchMode,
    RuleEvaluator,
    LocalMLService,
    MLSuggestion,
    SortAction,
    SortCondition,
    SortExecutionControl,
    SortExecutor,
    SortScanner,
    MetadataExtractor,
    HybridSortService,
    DEFAULT_SELECTED_CATEGORIES,
    DEFAULT_SORT_CATEGORIES,
    MonitoredFolder,
    SortPlanner,
    SortPlan,
    SortPlanItem,
    SortMonitorService,
    SortScheduleService,
    SortSetup,
    SortWorkflowSession,
    SortWorkflowStage,
    SortExecutionResult,
    QuickSortOptions,
    build_quick_profile,
    parse_extensions,
    SortingProfile,
    SortingProfileStore,
    SortingMigrationService,
)
from duplicate_transfer_manager.scheduled_sort import run_scheduled_sort


def metadata(path: str = "C:/Inbox/report.PDF", **values) -> FileMetadata:
    defaults = dict(
        path=path, name=Path(path).name, extension=Path(path).suffix.lower(), size=1200,
        created=100.0, modified=200.0, media_type="application",
    )
    defaults.update(values)
    return FileMetadata(**defaults)


class SortingModelsAndRulesTests(unittest.TestCase):
    def test_priority_and_exclusions_are_deterministic(self):
        pdf = SortCondition(ConditionField.EXTENSION, ConditionOperator.IN, [".pdf", ".docx"])
        temp_exclusion = SortCondition(ConditionField.SOURCE_PATH, ConditionOperator.CONTAINS, "temp", exclude=True)
        general = Association("Documents", (pdf,), SortAction.MOVE, "C:/Documents", priority=10)
        priority = Association("Reports", (pdf, temp_exclusion), SortAction.MOVE, "C:/Reports", priority=50)
        evaluator = RuleEvaluator()

        match = evaluator.evaluate(metadata(), [general, priority])
        excluded = evaluator.evaluate(metadata("C:/Temp/report.pdf"), [general, priority])

        self.assertEqual(match.association, priority)
        self.assertEqual(excluded.association, general)
        self.assertIn("priority 50", match.explanation)

    def test_same_priority_matches_are_ambiguous_and_do_not_choose_silently(self):
        condition = SortCondition(ConditionField.FILENAME, ConditionOperator.REGEX, r"^IMG_\d+\.jpg$")
        one = Association("Photos", (condition,), SortAction.MOVE, "C:/Photos", priority=20)
        two = Association("Camera", (condition,), SortAction.COPY, "C:/Camera", priority=20)

        result = RuleEvaluator().evaluate(metadata("C:/Inbox/IMG_123.jpg", media_type="image"), [two, one])

        self.assertIsNone(result.association)
        self.assertEqual({item.name for item in result.ambiguous}, {"Photos", "Camera"})

    def test_all_any_numeric_date_and_case_matching(self):
        conditions = (
            SortCondition(ConditionField.MEDIA_TYPE, ConditionOperator.EQUALS, "IMAGE"),
            SortCondition(ConditionField.SIZE, ConditionOperator.BETWEEN, [1000, 2000]),
        )
        association = Association("Images", conditions, SortAction.RENAME, match_mode=MatchMode.ALL)
        self.assertTrue(RuleEvaluator().matches(metadata(media_type="image"), association))
        self.assertFalse(RuleEvaluator().matches(metadata(media_type="video"), association))

    def test_invalid_regex_is_reported_as_validation_error(self):
        association = Association(
            "Broken", (SortCondition(ConditionField.FILENAME, ConditionOperator.REGEX, "["),), SortAction.IGNORE
        )
        with self.assertRaises(ServiceError):
            RuleEvaluator().matches(metadata(), association)


class SortingProfileStoreTests(unittest.TestCase):
    def test_profile_crud_duplicate_enable_import_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir) / "data")
            store = SortingProfileStore(paths)
            association = Association(
                "PDF", (SortCondition(ConditionField.EXTENSION, ConditionOperator.EQUALS, ".pdf"),),
                SortAction.MOVE, str(Path(temp_dir) / "Documents"), conflict_policy=ConflictPolicy.REVIEW,
            )
            saved = store.save(SortingProfile("Office", (association,)))
            duplicate = store.duplicate(saved.id)
            store.set_enabled(duplicate.id, False)
            export = store.export_profile(saved.id, Path(temp_dir) / "office.json")
            imported = store.import_profile(export)

            self.assertEqual(len(store.list()), 3)
            self.assertFalse(store.get(duplicate.id).enabled)
            self.assertIn("imported", imported.name)
            self.assertEqual(json.loads(export.read_text(encoding="utf-8"))["schema_version"], 1)
            self.assertTrue(store.delete(duplicate.id))
            self.assertFalse(store.delete("missing"))

    def test_profile_validation_and_legacy_preset_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SortingProfileStore(get_runtime_paths(Path(temp_dir) / "data"))
            with self.assertRaises(ServiceError):
                store.save(SortingProfile("", ()))
            settings = AppSettings(organization_presets=[{
                "name": "Downloads", "mode": "type", "destination_root": str(Path(temp_dir) / "sorted"),
                "conflict_policy": "replace", "ml_auto_organize": True,
            }])
            created = store.migrate_organizer_presets(settings)
            repeated = store.migrate_organizer_presets(settings)

            self.assertEqual(len(created), 1)
            self.assertEqual(repeated, [])
            self.assertEqual(created[0].associations[0].conflict_policy, ConflictPolicy.OVERWRITE)
            migrated_plan = SortPlanner(store.paths).build(
                created[0],
                [metadata(str(Path(temp_dir) / "photo.jpg"), media_type="image")],
            )
            self.assertEqual(Path(migrated_plan.items[0].destination).parent.name, "Image")


class QuickSortPresetTests(unittest.TestCase):
    def test_default_categories_focus_on_media_and_documents(self):
        labels = {category.key: category.label for category in DEFAULT_SORT_CATEGORIES}

        self.assertEqual(DEFAULT_SELECTED_CATEGORIES, ("pictures", "videos", "audio", "documents"))
        self.assertEqual([labels[key] for key in DEFAULT_SELECTED_CATEGORIES], ["Pictures", "Videos", "Audio", "Documents"])
        self.assertNotIn("archives", DEFAULT_SELECTED_CATEGORIES)

    def test_extension_parser_normalizes_common_user_input(self):
        self.assertEqual(parse_extensions("jpg, *.PDF; docx jpg"), (".jpg", ".pdf", ".docx"))
        with self.assertRaises(ServiceError):
            parse_extensions("not/an/extension")

    def test_quick_profile_builds_deterministic_category_and_custom_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = build_quick_profile(QuickSortOptions(
                destination_root=str(root / "Sorted"),
                selected_categories=("pictures", "documents"),
                custom_extensions=(".psd",), custom_category="pictures",
                action=SortAction.COPY, conflict_policy=ConflictPolicy.SKIP,
            ))

            self.assertFalse(profile.ml_enabled)
            self.assertEqual(profile.associations[0].priority, 1000)
            self.assertEqual(profile.associations[0].action, SortAction.COPY)
            self.assertEqual(profile.associations[0].conflict_policy, ConflictPolicy.SKIP)
            self.assertTrue(profile.associations[0].destination.endswith("Pictures"))
            self.assertEqual([rule.name for rule in profile.associations[1:]], ["Pictures", "Documents"])

    def test_quick_profile_leaves_unselected_file_types_in_place(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Sorted"
            profile = build_quick_profile(QuickSortOptions(str(destination), selected_categories=("pictures", "documents")))
            files = [
                metadata(str(root / "photo.jpg"), media_type="image"),
                metadata(str(root / "notes.pdf"), media_type="application"),
                metadata(str(root / "program.exe"), media_type="application"),
            ]

            plan = SortPlanner(get_runtime_paths(root / "data")).build(profile, files, sources=[str(root)])

            self.assertTrue(plan.items[0].destination.endswith(str(Path("Pictures") / "photo.jpg")))
            self.assertTrue(plan.items[1].destination.endswith(str(Path("Documents") / "notes.pdf")))
            self.assertEqual(len(plan.items), 2)
            self.assertNotIn("program.exe", {item.metadata.name for item in plan.items})


class SortWorkflowSessionTests(unittest.TestCase):
    def test_guided_session_requires_setup_review_and_approval_in_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.txt"
            source.write_text("source", encoding="utf-8")
            setup = SortSetup.create("profile", [str(source)], dry_run=False)
            item = SortPlanItem(
                metadata(str(source), size=6), "rule", SortAction.MOVE,
                str(Path(temp_dir) / "sorted" / source.name), ConflictPolicy.RENAME,
                1.0, "Matched a deterministic rule.", requires_review=False,
            )
            plan = SortPlan("profile", setup.sources, (item,), dry_run=False)
            session = SortWorkflowSession()

            with self.assertRaises(ServiceError):
                session.begin_preview()
            session.configure(setup)
            self.assertEqual(session.stage, SortWorkflowStage.RULES)
            session.begin_preview()
            session.accept_plan(plan)
            self.assertEqual(session.stage, SortWorkflowStage.REVIEW)
            with self.assertRaises(ServiceError):
                session.begin_processing()
            self.assertEqual(session.approve([str(source)]), (str(source.resolve()),))
            session.begin_processing()
            session.complete(SortExecutionResult("run", "completed", "journal.json", completed=1))
            self.assertEqual(session.stage, SortWorkflowStage.RESULTS)

    def test_setup_change_invalidates_the_previous_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.txt"
            second = Path(temp_dir) / "second.txt"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            initial = SortSetup.create("profile", [str(first)])
            session = SortWorkflowSession()
            session.configure(initial)
            session.begin_preview()
            session.accept_plan(SortPlan("profile", initial.sources, (), dry_run=True))

            session.configure(SortSetup.create("profile", [str(second)]))

            self.assertIsNone(session.plan)
            self.assertEqual(session.stage, SortWorkflowStage.RULES)
            with self.assertRaises(ServiceError):
                session.approve([])


class SortScannerTests(unittest.TestCase):
    def test_scans_manual_files_and_folders_once_and_excludes_destinations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbox = root / "inbox"
            destination = inbox / "sorted"
            nested = inbox / "nested"
            destination.mkdir(parents=True)
            nested.mkdir()
            one = inbox / "one.txt"
            two = nested / "two.txt"
            ignored = destination / "ignored.txt"
            one.write_text("one", encoding="utf-8")
            two.write_text("two", encoding="utf-8")
            ignored.write_text("ignored", encoding="utf-8")

            result = SortScanner().scan([inbox, one], excluded_roots=[destination])

            self.assertEqual({Path(item.path).name for item in result}, {"one.txt", "two.txt"})

    def test_video_metadata_uses_optional_local_ffprobe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "clip.mp4"
            video.write_bytes(b"video")
            completed = Mock(returncode=0, stdout='{"streams":[{"width":1920,"height":1080,"duration":"12.5"}]}')
            with patch("duplicate_transfer_manager.sorting.metadata.shutil.which", return_value="ffprobe"), patch(
                "duplicate_transfer_manager.sorting.metadata.subprocess.run", return_value=completed
            ):
                item = MetadataExtractor().extract(video)

            self.assertEqual((item.width, item.height), (1920, 1080))
            self.assertEqual(item.duration, 12.5)

    def test_cancelled_scan_stops_before_discovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = CancellationToken()
            token.cancel()
            with self.assertRaises(OperationCancelled):
                SortScanner().scan([temp_dir], cancellation=token)


class SortMLAndPlannerTests(unittest.TestCase):
    def test_user_rule_overrides_ml_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "invoice.pdf"
            source.write_bytes(b"pdf")
            called = []

            def classifier(*_args):
                called.append(True)
                return MLSuggestion("Wrong", str(root / "wrong"), 0.99, "model")

            rule = Association(
                "PDF rule", (SortCondition(ConditionField.EXTENSION, ConditionOperator.EQUALS, ".pdf"),),
                SortAction.MOVE, str(root / "documents"), priority=100,
            )
            profile = SortingProfile("Rules first", (rule,))
            planner = SortPlanner(get_runtime_paths(root / "data"), ml_service=LocalMLService(get_runtime_paths(root / "data"), classifier))

            plan = planner.build(profile, [metadata(str(source), size=3)], sources=[str(source)])

            self.assertEqual(plan.items[0].decision_source, "rule")
            self.assertEqual(plan.items[0].matched_association_name, "PDF rule")
            self.assertEqual(called, [])

    def test_ml_fallback_confidence_and_feedback_require_review_without_retraining(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = get_runtime_paths(root / "data")
            source = root / "receipt.jpg"
            source.write_bytes(b"image")
            item = metadata(str(source), size=5, media_type="image")
            profile = SortingProfile("ML", (), ml_enabled=True, high_confidence=0.92)
            ml = LocalMLService(paths)
            planner = SortPlanner(paths, ml_service=ml)

            plan = planner.build(profile, [item], default_destination=str(root / "sorted"))
            suggestion = ml.predict(item, profile, default_destination=str(root / "sorted"))
            ml.record_correction(item, suggestion, "Finance", str(root / "Finance"))
            corrected = ml.predict(item, profile, default_destination=str(root / "sorted"))

            self.assertEqual(plan.items[0].decision_source, "ml")
            self.assertTrue(plan.items[0].requires_review)
            self.assertEqual(plan.items[0].category, "Receipts")
            self.assertIn("Medium-confidence", plan.items[0].warnings[0])
            self.assertEqual(corrected.category, "Finance")
            self.assertEqual(corrected.provider, "local_feedback")
            self.assertFalse(corrected.available)

    def test_ml_disabled_leaves_unmatched_files_for_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "unknown.bin"
            source.write_bytes(b"x")
            profile = SortingProfile("No ML", (), ml_enabled=False)
            plan = SortPlanner(get_runtime_paths(root / "data")).build(profile, [metadata(str(source), size=1)])

            self.assertEqual(plan.items[0].action, SortAction.IGNORE)
            self.assertEqual(plan.items[0].decision_source, "unmatched")
            self.assertTrue(plan.items[0].requires_review)

    def test_conflict_policies_and_duplicate_destinations_are_planned_safely(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "sorted"
            destination.mkdir()
            occupied = destination / "same.txt"
            occupied.write_bytes(b"existing")
            one = root / "one" / "same.txt"
            two = root / "two" / "same.txt"
            one.parent.mkdir()
            two.parent.mkdir()
            one.write_bytes(b"one")
            two.write_bytes(b"two")
            association = Association("Text", (), SortAction.MOVE, str(destination), conflict_policy=ConflictPolicy.RENAME)
            profile = SortingProfile("Conflicts", (association,))
            plan = SortPlanner(get_runtime_paths(root / "data")).build(
                profile,
                [metadata(str(one), size=3), metadata(str(two), size=3)],
            )

            self.assertEqual([item.conflict for item in plan.items], ["rename", "rename"])
            self.assertNotEqual(plan.items[0].destination, plan.items[1].destination)

    def test_keep_newest_and_keep_largest_choose_source_or_existing_deterministically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "destination"
            source_root = root / "source"
            destination.mkdir()
            source_root.mkdir()
            source = source_root / "file.txt"
            existing = destination / "file.txt"
            source.write_bytes(b"source-is-larger")
            existing.write_bytes(b"old")
            source_metadata = MetadataExtractor().extract(source)
            newest_rule = Association("Newest", (), SortAction.MOVE, str(destination), conflict_policy=ConflictPolicy.KEEP_NEWEST)
            largest_rule = Association("Largest", (), SortAction.MOVE, str(destination), conflict_policy=ConflictPolicy.KEEP_LARGEST)

            newest = SortPlanner(get_runtime_paths(root / "data")).build(SortingProfile("Newest", (newest_rule,), ml_enabled=False), [replace(source_metadata, modified=0)])
            largest = SortPlanner(get_runtime_paths(root / "data")).build(SortingProfile("Largest", (largest_rule,), ml_enabled=False), [source_metadata])

            self.assertEqual(newest.items[0].conflict, "skip")
            self.assertFalse(newest.items[0].selected)
            self.assertEqual(largest.items[0].conflict, "overwrite")
            self.assertTrue(largest.items[0].requires_review)

    def test_recursive_destination_is_not_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = source / "sorted"
            source.mkdir()
            destination.mkdir()
            item = source / "file.txt"
            item.write_text("safe", encoding="utf-8")
            association = Association("Recursive", (), SortAction.MOVE, str(destination))
            plan = SortPlanner(get_runtime_paths(root / "data")).build(
                SortingProfile("Recursive", (association,), ml_enabled=False),
                [MetadataExtractor().extract(item)], sources=[str(source)], dry_run=False,
            )
            self.assertEqual(plan.items[0].conflict, "recursive")
            self.assertFalse(plan.items[0].selected)

    def test_manual_destination_prevents_sorting_onto_itself(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "file.txt"
            source.write_text("x", encoding="utf-8")
            plan_item = SortPlanner(get_runtime_paths(root / "data")).build(
                SortingProfile("Manual", (), ml_enabled=False), [metadata(str(source), size=1)]
            ).items[0]
            with self.assertRaises(ServiceError):
                SortPlanner(get_runtime_paths(root / "data")).with_manual_destination(plan_item, str(source))


class SortExecutorTests(unittest.TestCase):
    def _plan(self, root: Path, action: SortAction, *, conflict=ConflictPolicy.RENAME, dry_run=False):
        source = root / "source" / "file.txt"
        destination = root / "destination"
        source.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"safe payload")
        association = Association("Everything", (), action, str(destination), conflict_policy=conflict)
        profile = SortingProfile("Execution", (association,), ml_enabled=False)
        paths = get_runtime_paths(root / "data")
        plan = SortPlanner(paths).build(
            profile, [MetadataExtractor().extract(source)], sources=[str(source.parent)], dry_run=dry_run
        )
        return paths, source, destination / "file.txt", plan

    def test_dry_run_writes_preview_journal_without_changing_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, source, destination, plan = self._plan(Path(temp_dir), SortAction.MOVE, dry_run=True)

            result = SortExecutor(paths).execute(plan)

            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())
            self.assertEqual(result.status, "completed")
            journal = json.loads(Path(result.journal_path).read_text(encoding="utf-8"))
            self.assertTrue(journal["dry_run"])
            self.assertEqual(journal["records"][0]["status"], "previewed")
            json_export = SortExecutor(paths).export_run(result.run_id, Path(temp_dir) / "report.json")
            csv_export = SortExecutor(paths).export_run(result.run_id, Path(temp_dir) / "report.csv")
            self.assertEqual(json.loads(json_export.read_text(encoding="utf-8"))["run_id"], result.run_id)
            self.assertIn("source,destination,action", csv_export.read_text(encoding="utf-8").splitlines()[0])

    def test_live_move_requires_confirmation_and_can_be_undone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, source, destination, plan = self._plan(Path(temp_dir), SortAction.MOVE)
            executor = SortExecutor(paths)
            approved = [plan.items[0].metadata.path]
            with self.assertRaises(ServiceError):
                executor.execute(plan, approved_sources=approved)

            result = executor.execute(plan, approved_sources=approved, confirmed=True)
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"safe payload")
            self.assertEqual(result.verified, 1)
            undo = executor.undo(result.run_id)
            self.assertEqual(undo.failed, 0)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_copy_and_overwrite_backup_are_reversible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths, source, destination, _plan = self._plan(root, SortAction.COPY, conflict=ConflictPolicy.OVERWRITE)
            destination.write_bytes(b"old payload")
            association = Association("Copy", (), SortAction.COPY, str(destination.parent), conflict_policy=ConflictPolicy.OVERWRITE)
            profile = SortingProfile("Copy", (association,), ml_enabled=False)
            plan = SortPlanner(paths).build(profile, [MetadataExtractor().extract(source)], dry_run=False)
            executor = SortExecutor(paths)

            with self.assertRaises(ServiceError):
                executor.execute(plan, approved_sources=[str(source)], confirmed=False)

            result = executor.execute(plan, approved_sources=[str(source)], confirmed=True)
            self.assertTrue(source.exists())
            self.assertEqual(destination.read_bytes(), b"safe payload")
            executor.undo(result.run_id)
            self.assertEqual(destination.read_bytes(), b"old payload")
            self.assertTrue(source.exists())

    def test_pre_cancel_and_per_file_skip_are_journaled_safely(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, source, destination, plan = self._plan(Path(temp_dir), SortAction.MOVE)
            executor = SortExecutor(paths)
            cancelled = SortExecutionControl()
            cancelled.cancel()
            result = executor.execute(plan, approved_sources=[str(source)], confirmed=True, control=cancelled)
            self.assertEqual(result.status, "cancelled")
            self.assertTrue(source.exists())

            skip = SortExecutionControl()
            skip.skip(str(source))
            skipped = executor.execute(plan, approved_sources=[str(source)], confirmed=True, control=skip)
            self.assertEqual(skipped.skipped, 1)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_retry_recovers_from_transient_item_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, source, destination, plan = self._plan(Path(temp_dir), SortAction.MOVE)
            executor = SortExecutor(paths)
            original = executor._execute_item
            calls = []

            def flaky(*args, **kwargs):
                calls.append(True)
                if len(calls) == 1:
                    raise OSError("temporary lock")
                return original(*args, **kwargs)

            executor._execute_item = flaky
            result = executor.execute(plan, approved_sources=[str(source)], confirmed=True, retry_attempts=1)

            self.assertEqual(result.failed, 0)
            self.assertEqual(len(calls), 2)
            self.assertTrue(destination.exists())

    def test_unchecked_deterministic_rule_is_not_executed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, source, destination, plan = self._plan(Path(temp_dir), SortAction.MOVE)

            result = SortExecutor(paths).execute(plan, approved_sources=[], confirmed=True)

            self.assertEqual(result.completed, 0)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_failed_and_cancelled_journals_can_retry_or_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths, source, destination, plan = self._plan(root, SortAction.MOVE)
            failing = SortExecutor(paths)
            failing._execute_item = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked"))
            failed = failing.execute(plan, approved_sources=[str(source)], confirmed=True, retry_attempts=0)
            retried = SortExecutor(paths).retry_failed(failed.run_id, confirmed=True)
            self.assertEqual(retried.failed, 0)
            self.assertTrue(destination.exists())

            # Create a second source and cancel before its first checkpoint.
            paths2, source2, destination2, plan2 = self._plan(root / "second", SortAction.MOVE)
            control = SortExecutionControl()
            control.cancel()
            cancelled = SortExecutor(paths2).execute(plan2, approved_sources=[str(source2)], confirmed=True, control=control)
            resumed = SortExecutor(paths2).resume_run(cancelled.run_id, confirmed=True)
            self.assertEqual(resumed.failed, 0)
            self.assertTrue(destination2.exists())

    def test_rename_ignore_quarantine_and_recycle_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = get_runtime_paths(root / "data")
            executor = SortExecutor(paths)

            rename_source = root / "rename.txt"
            rename_source.write_text("rename", encoding="utf-8")
            rename_rule = Association("Rename", (), SortAction.RENAME, "", rename_template="renamed-{stem}{suffix}")
            rename_plan = SortPlanner(paths).build(SortingProfile("Rename", (rename_rule,), ml_enabled=False), [MetadataExtractor().extract(rename_source)], dry_run=False)
            renamed = executor.execute(rename_plan, approved_sources=[str(rename_source)], confirmed=True)
            self.assertTrue((root / "renamed-rename.txt").exists())
            executor.undo(renamed.run_id)
            self.assertTrue(rename_source.exists())

            ignored_source = root / "ignored.txt"
            ignored_source.write_text("ignore", encoding="utf-8")
            ignore_rule = Association("Ignore", (), SortAction.IGNORE)
            ignore_plan = SortPlanner(paths).build(SortingProfile("Ignore", (ignore_rule,), ml_enabled=False), [MetadataExtractor().extract(ignored_source)], dry_run=False)
            ignored = executor.execute(ignore_plan, approved_sources=[str(ignored_source)], confirmed=True)
            self.assertEqual(ignored.skipped, 1)
            self.assertTrue(ignored_source.exists())

            quarantine_source = root / "quarantine.txt"
            quarantine_source.write_text("quarantine", encoding="utf-8")
            quarantine_rule = Association("Quarantine", (), SortAction.QUARANTINE)
            quarantine_plan = SortPlanner(paths).build(SortingProfile("Quarantine", (quarantine_rule,), ml_enabled=False), [MetadataExtractor().extract(quarantine_source)], dry_run=False)
            quarantined = executor.execute(quarantine_plan, approved_sources=[str(quarantine_source)], confirmed=True)
            self.assertFalse(quarantine_source.exists())
            executor.undo(quarantined.run_id)
            self.assertTrue(quarantine_source.exists())

            recycle_source = root / "recycle.txt"
            recycle_source.write_text("recycle", encoding="utf-8")
            recycle_rule = Association("Recycle", (), SortAction.RECYCLE)
            recycle_plan = SortPlanner(paths).build(SortingProfile("Recycle", (recycle_rule,), ml_enabled=False), [MetadataExtractor().extract(recycle_source)], dry_run=False)
            with patch("duplicate_transfer_manager.sorting.executor._send_to_recycle", side_effect=lambda value: Path(value).unlink()):
                recycled = executor.execute(recycle_plan, approved_sources=[str(recycle_source)], confirmed=True)
            self.assertFalse(recycle_source.exists())
            self.assertFalse(recycled.undo_available)

    def test_low_disk_preflight_stops_before_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, source, destination, plan = self._plan(Path(temp_dir), SortAction.COPY)
            usage = type("Usage", (), {"free": 0})()
            with patch("duplicate_transfer_manager.sorting.executor.shutil.disk_usage", return_value=usage):
                with self.assertRaises(ServiceError):
                    SortExecutor(paths).execute(plan, approved_sources=[str(source)], confirmed=True)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_cross_volume_rename_is_included_in_space_preflight(self):
        item = SortPlanItem(
            metadata("C:/source/file.bin", size=4096),
            "rule",
            SortAction.RENAME,
            "D:/destination/file.bin",
            ConflictPolicy.RENAME,
            1.0,
            "cross-volume rename",
        )
        usage = type("Usage", (), {"free": 0})()
        with patch("duplicate_transfer_manager.sorting.executor.shutil.disk_usage", return_value=usage):
            with self.assertRaises(ServiceError):
                SortExecutor._validate_space([item])

    def test_verification_failure_restores_moved_source_without_partial_loss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, source, destination, plan = self._plan(Path(temp_dir), SortAction.MOVE)
            executor = SortExecutor(paths)
            original_verify = executor._verify_fingerprint
            calls = []

            def fail_after_move(path, expected):
                calls.append(path)
                raise OSError("verification failed")

            executor._verify_fingerprint = fail_after_move
            result = executor.execute(plan, approved_sources=[str(source)], confirmed=True, retry_attempts=0)

            self.assertEqual(result.failed, 1)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_undo_refuses_destination_changed_after_sorting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, source, destination, plan = self._plan(Path(temp_dir), SortAction.MOVE)
            executor = SortExecutor(paths)
            result = executor.execute(plan, approved_sources=[str(source)], confirmed=True)
            destination.write_bytes(b"changed after sorting")

            undo = executor.undo(result.run_id)

            self.assertEqual(undo.failed, 1)
            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())

    def test_overwrite_undo_failure_preserves_incumbent_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, source, destination, plan = self._plan(Path(temp_dir), SortAction.MOVE)
            executor = SortExecutor(paths)
            result = executor.execute(plan, approved_sources=[str(source)], confirmed=True)
            source.write_bytes(b"incumbent")

            with patch(
                "duplicate_transfer_manager.sorting.executor.shutil.move",
                side_effect=OSError("forced undo failure"),
            ):
                undo = executor.undo(result.run_id, conflict_policy=ConflictPolicy.OVERWRITE)

            self.assertEqual(undo.failed, 1)
            self.assertEqual(source.read_bytes(), b"incumbent")
            self.assertTrue(destination.exists())

    def test_history_retention_removes_only_expired_app_owned_run_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = get_runtime_paths(root / "data")
            old_run = paths.sorting / "runs" / "old"
            current_run = paths.sorting / "runs" / "current"
            old_run.mkdir(parents=True)
            current_run.mkdir(parents=True)
            old_journal = old_run / "journal.json"
            current_journal = current_run / "journal.json"
            old_journal.write_text("{}", encoding="utf-8")
            current_journal.write_text("{}", encoding="utf-8")
            user_file = root / "sorted-user-file.txt"
            user_file.write_text("keep", encoding="utf-8")
            expired = time.time() - (10 * 24 * 60 * 60)
            os.utime(old_journal, (expired, expired))

            removed = SortExecutor(paths).prune_runs(retention_days=5)

            self.assertEqual(removed, 1)
            self.assertFalse(old_run.exists())
            self.assertTrue(current_journal.exists())
            self.assertEqual(user_file.read_text(encoding="utf-8"), "keep")


class SortWorkflowAndMonitorTests(unittest.TestCase):
    def test_full_workflow_scans_plans_executes_and_undoes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "inbox"
            destination = root / "sorted"
            source.mkdir()
            destination.mkdir()
            original = source / "notes.txt"
            original.write_text("notes", encoding="utf-8")
            association = Association(
                "Text", (SortCondition(ConditionField.EXTENSION, ConditionOperator.EQUALS, ".txt"),),
                SortAction.MOVE, str(destination),
            )
            profile = SortingProfile("Workflow", (association,), ml_enabled=False)
            service = HybridSortService(get_runtime_paths(root / "data"))

            preview = service.prepare(profile, [str(source)], dry_run=False)
            result = service.execute(
                preview, approved_sources=[str(original)], confirmed=True, control=SortExecutionControl()
            )

            self.assertEqual(result.completed, 1)
            self.assertTrue((destination / "notes.txt").exists())
            service.executor.undo(result.run_id)
            self.assertTrue(original.exists())

    def test_workflow_uses_prior_verified_destination_for_unmatched_ml_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = get_runtime_paths(root / "data")
            first = root / "first" / "one.xyz"
            prior_destination = root / "Archive"
            first.parent.mkdir()
            prior_destination.mkdir()
            first.write_text("one", encoding="utf-8")
            association = Association("Archive XYZ", (), SortAction.COPY, str(prior_destination))
            first_profile = SortingProfile("First", (association,), ml_enabled=False)
            service = HybridSortService(paths)
            first_plan = service.prepare(first_profile, [str(first)], dry_run=False)
            service.execute(first_plan, approved_sources=[str(first)], confirmed=True, control=SortExecutionControl())
            second = root / "second" / "two.xyz"
            second.parent.mkdir()
            second.write_text("two", encoding="utf-8")

            suggestion_plan = service.prepare(SortingProfile("History", (), ml_enabled=True), [str(second)], dry_run=True)

            self.assertIn("prior local destinations", suggestion_plan.items[0].explanation)
            self.assertEqual(Path(suggestion_plan.items[0].destination).parent, prior_destination)

    def test_monitor_detects_new_and_changed_files_without_repeating_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watched = root / "watched"
            watched.mkdir()
            item = watched / "one.txt"
            item.write_text("one", encoding="utf-8")
            service = SortMonitorService(get_runtime_paths(root / "data"))
            folder = MonitoredFolder(str(watched), scan_mode="filesystem_change")

            first = service.poll(folder)
            second = service.poll(folder)
            item.write_text("changed", encoding="utf-8")
            third = service.poll(folder)

            self.assertEqual([Path(value.path).name for value in first], ["one.txt"])
            self.assertEqual(second, ())
            self.assertEqual([Path(value.path).name for value in third], ["one.txt"])
            self.assertTrue(service.reset(folder.id))

    def test_live_monitoring_requires_explicit_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            watched = Path(temp_dir) / "watched"
            watched.mkdir()
            folder = MonitoredFolder(str(watched), dry_run=False, live_approved=False)
            with self.assertRaises(ServiceError):
                SortMonitorService(get_runtime_paths(Path(temp_dir) / "data")).poll(folder)

    def test_scheduler_builds_profile_scoped_windows_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            watched = Path(temp_dir) / "watched"
            watched.mkdir()
            monitor = MonitoredFolder(str(watched), scan_mode="scheduled", schedule="daily", dry_run=True)
            profile = SortingProfile("Scheduled", (), monitored_folders=(monitor,))
            completed = Mock(returncode=0, stdout="", stderr="")
            with patch("duplicate_transfer_manager.sorting.scheduler.os.name", "nt"), patch(
                "duplicate_transfer_manager.sorting.scheduler.subprocess.run", return_value=completed
            ) as run:
                SortScheduleService().configure(profile, monitor, data_root=str(Path(temp_dir) / "data"))

            command = run.call_args.args[0]
            self.assertIn("/Create", command)
            self.assertIn(profile.id, command[command.index("/TN") + 1])
            self.assertIn("duplicate_transfer_manager.scheduled_sort", command[-1])

    def test_scheduled_sort_defaults_to_dry_run_and_records_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = get_runtime_paths(root / "data")
            watched = root / "watched"
            destination = root / "destination"
            watched.mkdir()
            destination.mkdir()
            source = watched / "notes.txt"
            source.write_text("notes", encoding="utf-8")
            monitor = MonitoredFolder(str(watched), scan_mode="scheduled", schedule="daily", dry_run=True)
            association = Association("Text", (), SortAction.MOVE, str(destination))
            profile = SortingProfile("Scheduled", (association,), monitored_folders=(monitor,), ml_enabled=False)
            SortingProfileStore(paths).save(profile)

            status = run_scheduled_sort(profile.id, monitor.id, data_root=str(paths.root))

            self.assertEqual(status, 0)
            self.assertTrue(source.exists())
            self.assertFalse((destination / "notes.txt").exists())
            self.assertTrue(any(run.get("dry_run") for run in SortExecutor(paths).list_runs()))

    def test_legacy_organizer_manifest_is_imported_without_removal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = get_runtime_paths(root / "data")
            operation = paths.organization / "organize_old"
            operation.mkdir()
            source = root / "source.txt"
            destination = root / "destination.txt"
            destination.write_text("safe", encoding="utf-8")
            manifest = operation / "manifest.json"
            manifest.write_text(json.dumps({
                "operation_id": "organize_old", "created_at": "2026-01-01T00:00:00+00:00", "dry_run": False,
                "settings": {"source_root": str(root), "conflict_policy": "rename"},
                "plan": [{"source_path": str(source), "destination_path": str(destination), "size": 4, "modified": 1, "selected": True}],
                "records": [{"source_path": str(source), "destination_path": str(destination), "size": 4, "source_fingerprint": ""}],
            }), encoding="utf-8")

            imported = SortingMigrationService(paths).migrate_legacy_runs()

            self.assertEqual(imported, 1)
            self.assertTrue(manifest.exists())
            self.assertTrue(any(run.get("legacy_manifest") == str(manifest) for run in SortExecutor(paths).list_runs()))
            self.assertEqual(SortingMigrationService(paths).migrate_legacy_runs(), 0)


if __name__ == "__main__":
    unittest.main()
