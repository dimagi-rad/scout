# Scout

## Testing conventions

### data-testid attributes

Interactive UI elements that QA automation (showboat/rodney) targets must have `data-testid` attributes. This decouples tests from CSS classes and DOM structure so styling changes don't break test scenarios.

Naming convention: `{component}-{element}` using kebab-case. Dynamic names use the pattern `{component}-{identifier}`, e.g. `table-item-users`, `schema-group-public`, `column-note-email`.

When adding new interactive elements to pages that have QA scenarios in `tests/qa/`, add a `data-testid` to any element a test might need to click, read, or assert on.
