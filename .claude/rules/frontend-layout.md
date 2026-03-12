---
paths:
  - "frontend/**/*.{ts,tsx}"
---

# Frontend Page Layout Conventions

## Page-level layout

All top-level page views must use **full-width, flush-left layout**. Do not center page content with `max-w-*` + `mx-auto`.

**Correct pattern** (used by Knowledge, Recipes, Connections, etc.):
```tsx
<div className="p-6">
  <div className="mb-6 flex items-center justify-between">
    <div>
      <h1>Page Title</h1>
      <p className="text-muted-foreground">Subtitle</p>
    </div>
    {/* actions */}
  </div>
  {/* content fills full width */}
</div>
```

**Incorrect pattern** — do not use:
```tsx
<div className="mx-auto max-w-2xl px-6 py-8">  {/* ❌ centered, constrained */}
```

The sidebar provides the left boundary. Page content fills the remaining viewport width.
