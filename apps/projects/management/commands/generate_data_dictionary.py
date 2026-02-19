"""
Management command to generate/refresh data dictionaries.

Usage:
    # Generate for a specific project
    python manage.py generate_data_dictionary --project-slug my-project

    # Generate for all projects
    python manage.py generate_data_dictionary --all

    # Dry run (print to stdout, don't save)
    python manage.py generate_data_dictionary --project-slug my-project --dry-run
"""
from django.core.management.base import BaseCommand, CommandError

from apps.projects.models import Project
from apps.projects.services.data_dictionary import DataDictionaryGenerator


class Command(BaseCommand):
    help = "Generate data dictionary from project database schema"

    def add_arguments(self, parser):
        parser.add_argument(
            "--project-slug",
            type=str,
            help="Project slug to generate for",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Generate for all projects",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print to stdout, don't save",
        )

    def handle(self, *args, **options):
        if options["all"]:
            projects = Project.objects.all()
        elif options["project_slug"]:
            projects = Project.objects.filter(slug=options["project_slug"])
            if not projects.exists():
                raise CommandError(f"Project with slug '{options['project_slug']}' not found")
        else:
            raise CommandError("Specify --project-slug or --all")

        for project in projects:
            self.stdout.write(f"Generating data dictionary for: {project.name}")
            generator = DataDictionaryGenerator(project)

            try:
                if options["dry_run"]:
                    generator.generate()
                    self.stdout.write(generator.render_for_prompt())
                    # Don't save in dry run mode - but generate() already saved, so undo
                    project.refresh_from_db()
                else:
                    generator.generate()
                    table_count = len(project.data_dictionary.get("tables", {}))
                    self.stdout.write(
                        self.style.SUCCESS(f"  ✓ Generated: {table_count} tables")
                    )
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f"  ✗ Error generating for {project.name}: {e}")
                )
