"""Utility functions for knowledge import/export."""
import yaml


def parse_frontmatter(text: str) -> tuple[str, list[str], str]:
    """Parse YAML frontmatter from a markdown document.

    Expected format:
        ---
        title: Some Title
        tags: [metric, finance]
        ---
        Body content here...

    Returns:
        (title, tags, content_body)
    """
    text = text.strip()
    if not text.startswith("---"):
        # No frontmatter - use first line as title
        lines = text.split("\n", 1)
        title = lines[0].lstrip("# ").strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        return title, [], body

    # Find closing ---
    end_idx = text.index("---", 3)
    frontmatter_str = text[3:end_idx].strip()
    body = text[end_idx + 3:].strip()

    meta = yaml.safe_load(frontmatter_str) or {}
    title = meta.get("title", "")
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    return title, tags, body


def render_frontmatter(title: str, tags: list[str], content: str) -> str:
    """Render a markdown document with YAML frontmatter."""
    meta = {"title": title}
    if tags:
        meta["tags"] = tags
    frontmatter = yaml.dump(meta, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{frontmatter}\n---\n\n{content}\n"
