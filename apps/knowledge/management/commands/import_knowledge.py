"""
Management command to import knowledge entries from markdown files.

Each .md file should have YAML frontmatter with title and optional tags:

    ---
    title: Some Title
    tags: [metric, finance]
    ---
    Body content here...

Usage:
    python manage.py import_knowledge --project-slug my-project --dir ./knowledge
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.knowledge.models import KnowledgeEntry
from apps.knowledge.utils import parse_frontmatter
from apps.projects.models import Project


class Command(BaseCommand):
    help = "Import knowledge entries from markdown files with YAML frontmatter"

    def add_arguments(self, parser):
        parser.add_argument(
            "--project-slug",
            type=str,
            required=True,
            help="Project slug to import knowledge for",
        )
        parser.add_argument(
            "--dir",
            type=str,
            required=True,
            help="Directory containing .md files (searched recursively)",
        )

    def handle(self, *args, **options):
        try:
            project = Project.objects.get(slug=options["project_slug"])
        except Project.DoesNotExist:
            raise CommandError(f"Project with slug '{options['project_slug']}' not found")

        knowledge_dir = Path(options["dir"])
        if not knowledge_dir.exists():
            raise CommandError(f"Directory '{knowledge_dir}' does not exist")

        created = 0
        updated = 0

        for md_file in sorted(knowledge_dir.glob("**/*.md")):
            try:
                raw = md_file.read_text(encoding="utf-8")
                title, tags, body = parse_frontmatter(raw)
                if not title:
                    self.stderr.write(f"  Skipping {md_file}: no title found")
                    continue

                _, was_created = KnowledgeEntry.objects.update_or_create(
                    project=project,
                    title=title,
                    defaults={"content": body, "tags": tags},
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  Error importing {md_file}: {e}"))

        self.stdout.write(
            self.style.SUCCESS(f"Knowledge entries: {created} created, {updated} updated")
        )
