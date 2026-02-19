"""
Management command to run golden query evaluations.

Usage:
    # Run all golden queries for a project
    python manage.py run_eval --project-slug my-project

    # Filter by tag
    python manage.py run_eval --project-slug my-project --tag finance

    # Filter by difficulty
    python manage.py run_eval --project-slug my-project --difficulty easy

    # Combine filters
    python manage.py run_eval --project-slug my-project --tag finance --difficulty medium
"""

from django.core.management.base import BaseCommand, CommandError

from apps.knowledge.models import GoldenQuery
from apps.knowledge.services.eval_runner import EvalRunner
from apps.projects.models import Project


class Command(BaseCommand):
    help = "Run golden query evaluations for a project"

    def add_arguments(self, parser):
        parser.add_argument(
            "--project-slug",
            type=str,
            required=True,
            help="Project slug to run evaluation for",
        )
        parser.add_argument(
            "--tag",
            type=str,
            action="append",
            help="Filter by tag (can be specified multiple times)",
        )
        parser.add_argument(
            "--difficulty",
            type=str,
            choices=["easy", "medium", "hard"],
            help="Filter by difficulty level",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed output for each query",
        )

    def handle(self, *args, **options):
        # Get project
        try:
            project = Project.objects.get(slug=options["project_slug"])
        except Project.DoesNotExist as err:
            raise CommandError(
                f"Project with slug '{options['project_slug']}' not found"
            ) from err

        # Check for golden queries
        query_count = GoldenQuery.objects.filter(project=project).count()
        if query_count == 0:
            self.stdout.write(
                self.style.WARNING(
                    f"No golden queries found for project '{project.name}'"
                )
            )
            return

        # Build filters
        tags = options.get("tag") or []
        difficulty = options.get("difficulty")
        verbose = options.get("verbose", False)

        # Show what we're doing
        self.stdout.write(f"\nRunning evaluation for project: {project.name}")
        if tags:
            self.stdout.write(f"  Tags filter: {', '.join(tags)}")
        if difficulty:
            self.stdout.write(f"  Difficulty filter: {difficulty}")
        self.stdout.write(f"  Total golden queries: {query_count}")
        self.stdout.write("")

        # Run evaluation
        runner = EvalRunner(
            project=project,
            tags=tags,
            difficulty=difficulty,
        )

        self.stdout.write("Running queries...")
        eval_run = runner.run()

        # Print summary
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("EVALUATION SUMMARY"))
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(f"  Total queries: {eval_run.total_queries}")
        self.stdout.write(
            self.style.SUCCESS(f"  Passed: {eval_run.passed}")
        )
        self.stdout.write(
            self.style.ERROR(f"  Failed: {eval_run.failed}")
            if eval_run.failed > 0
            else f"  Failed: {eval_run.failed}"
        )
        self.stdout.write(
            self.style.WARNING(f"  Errored: {eval_run.errored}")
            if eval_run.errored > 0
            else f"  Errored: {eval_run.errored}"
        )
        self.stdout.write(f"  Accuracy: {eval_run.accuracy:.1%}")
        self.stdout.write("")

        # Print details for failures
        failed_results = [r for r in eval_run.results if not r.get("passed")]
        if failed_results:
            self.stdout.write(self.style.ERROR("FAILURES:"))
            self.stdout.write("-" * 60)
            for i, result in enumerate(failed_results, 1):
                self.stdout.write(f"\n{i}. {result['question'][:80]}...")
                if result.get("error"):
                    self.stdout.write(f"   Error: {result['error']}")
                else:
                    self.stdout.write(f"   Expected: {result['expected']}")
                    self.stdout.write(f"   Actual: {result['actual']}")
                if result.get("sql_executed"):
                    self.stdout.write(f"   SQL: {result['sql_executed'][:100]}...")
                if verbose and result.get("comparison_details"):
                    self.stdout.write(f"   Details: {result['comparison_details']}")
            self.stdout.write("")

        # Print successful queries if verbose
        if verbose:
            passed_results = [r for r in eval_run.results if r.get("passed")]
            if passed_results:
                self.stdout.write(self.style.SUCCESS("\nPASSED QUERIES:"))
                self.stdout.write("-" * 60)
                for i, result in enumerate(passed_results, 1):
                    self.stdout.write(f"\n{i}. {result['question'][:80]}...")
                    self.stdout.write(f"   Latency: {result['latency_ms']}ms")
                    if result.get("sql_executed"):
                        self.stdout.write(
                            f"   SQL: {result['sql_executed'][:100]}..."
                        )

        self.stdout.write(f"\nEval run ID: {eval_run.id}")
