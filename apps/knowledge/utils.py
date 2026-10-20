"""Utility functions for knowledge import/export."""

import yaml


def parse_frontmatter(text: str) -> tuple[str, list[str], str]:
    """Parse YAML frontmatter from a markdown document; see ``parse_frontmatter_meta``."""
    title, tags, body, _meta = parse_frontmatter_meta(text)
    return title, tags, body


def parse_frontmatter_meta(text: str) -> tuple[str, list[str], str, dict]:
    """Parse YAML frontmatter from a markdown document.

    Expected format:
        ---
        title: Some Title
        tags: [metric, finance]
        ---
        Body content here...

    Returns:
        (title, tags, content_body, frontmatter_mapping)
    """
    text = text.strip()
    if not text.startswith("---"):
        # No frontmatter: fall back to first line as title.
        lines = text.split("\n", 1)
        title = lines[0].lstrip("# ").strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        return title, [], body, {}

    end_idx = text.find("---", 3)
    if end_idx == -1:
        raise ValueError("Frontmatter is missing a closing '---' fence.")
    frontmatter_str = text[3:end_idx].strip()
    body = text[end_idx + 3 :].strip()

    meta = yaml.safe_load(frontmatter_str) or {}
    title = meta.get("title", "")
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    return title, tags, body, meta


def render_frontmatter(title: str, tags: list[str], content: str, extra: dict | None = None) -> str:
    """Render a markdown document with YAML frontmatter; ``extra`` adds more keys."""
    meta = {"title": title, **({"tags": tags} if tags else {}), **(extra or {})}
    frontmatter = yaml.dump(meta, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{frontmatter}\n---\n\n{content}\n"
