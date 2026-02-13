# API reference

All API endpoints are prefixed with `/api/`. Authentication uses session cookies with CSRF protection.

## Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/auth/csrf/` | Set CSRF cookie. Call this before making any POST requests. |
| GET | `/api/auth/me/` | Get current user info. Returns 401 if not authenticated. |
| POST | `/api/auth/login/` | Email/password login. |
| POST | `/api/auth/logout/` | End session. |

### Login request

```json
POST /api/auth/login/
{
  "email": "user@example.com",
  "password": "password"
}
```

### Login response

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "user@example.com",
  "name": "John Doe"
}
```

### Me response

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "user@example.com",
  "name": "John Doe"
}
```

### Error responses

- `400` -- invalid JSON or missing fields.
- `401` -- invalid credentials or not authenticated.
- `429` -- rate limited (too many failed login attempts).

## Projects

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/projects/` | List projects the current user has access to. |

### Project list response

Returns projects where the current user has a membership:

```json
[
  {
    "id": "...",
    "name": "Sales Analytics",
    "slug": "sales-analytics",
    "description": "...",
    "role": "analyst"
  }
]
```

## Chat

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/chat/` | Send a message and receive a streaming response. |

### Chat request

```json
POST /api/chat/
{
  "messages": [
    {
      "role": "user",
      "content": "How many orders were placed last month?"
    }
  ],
  "data": {
    "projectId": "550e8400-...",
    "threadId": "optional-thread-id"
  }
}
```

- `messages` -- array of messages in the conversation. The last message must be from the user.
- `data.projectId` -- required. The project to query.
- `data.threadId` -- optional. Thread ID for conversation continuity. A new UUID is generated if omitted.

### Chat response

The response is a `text/event-stream` (Server-Sent Events) following the UI Message Stream Protocol. Each event is a JSON object:

```
{"type":"text-delta","id":"msg-1","delta":"The total"}
{"type":"text-delta","id":"msg-1","delta":" number of orders"}
```

### Error responses

- `400` -- invalid JSON, missing messages, or empty message.
- `401` -- not authenticated.
- `403` -- not a member of the project or project is inactive.
- `405` -- method not allowed (must be POST).
- `500` -- agent initialization failed.

### Message length limit

Messages are limited to 10,000 characters.

## Artifacts

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/artifacts/<id>/sandbox/` | Render artifact in a sandboxed iframe. |
| GET | `/api/artifacts/<id>/data/` | Get artifact code and data as JSON. |
| GET | `/api/artifacts/<id>/export/<format>/` | Export artifact (html, png, pdf). |
| GET | `/api/artifacts/shared/<token>/` | View a shared artifact. |
| POST | `/api/artifacts/<id>/share/` | Create a share link. |
| GET | `/api/artifacts/<id>/shares/` | List share links for an artifact. |
| DELETE | `/api/artifacts/<id>/shares/<token>/` | Revoke a share link. |

### Create share link

```json
POST /api/artifacts/<id>/share/
{
  "access_level": "public",
  "expires_at": "2026-03-01T00:00:00Z"
}
```

Access levels: `public`, `project`, `specific`.

## Knowledge

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/projects/<pid>/knowledge/` | List knowledge items (entries and learnings). |
| POST | `/api/projects/<pid>/knowledge/` | Create a knowledge entry. |
| GET | `/api/projects/<pid>/knowledge/<id>/` | Get a single knowledge item. |
| PUT | `/api/projects/<pid>/knowledge/<id>/` | Update a knowledge item. |
| DELETE | `/api/projects/<pid>/knowledge/<id>/` | Delete a knowledge item. |
| GET | `/api/projects/<pid>/knowledge/export/` | Export entries as a zip of markdown files. |
| POST | `/api/projects/<pid>/knowledge/import/` | Import a zip of markdown files as entries. |

### List parameters

| Parameter | Description |
|-----------|-------------|
| `type` | Filter by type: `entry` or `learning`. |
| `search` | Search entries by title/content and learnings by description. |
| `page` | Page number (default: 1). |
| `page_size` | Items per page (default: 50). |

### List response

```json
{
  "results": [
    {
      "id": "...",
      "type": "entry",
      "title": "MRR Definition",
      "content": "Monthly Recurring Revenue is...",
      "tags": ["metric", "finance"],
      "created_at": "2026-01-15T10:00:00Z",
      "updated_at": "2026-01-15T10:00:00Z"
    },
    {
      "id": "...",
      "type": "learning",
      "description": "The events.timestamp column stores Unix epoch milliseconds",
      "category": "type_mismatch",
      "applies_to_tables": ["events"],
      "original_error": "invalid input syntax for type timestamp",
      "original_sql": "SELECT timestamp FROM events",
      "corrected_sql": "SELECT to_timestamp(timestamp / 1000.0) FROM events",
      "confidence_score": 0.9,
      "times_applied": 15,
      "created_at": "2026-01-20T14:30:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 50,
    "total_count": 12,
    "total_pages": 1,
    "has_next": false,
    "has_previous": false
  }
}
```

### Create entry

```json
POST /api/projects/<pid>/knowledge/
{
  "type": "entry",
  "title": "MRR Definition",
  "content": "Monthly Recurring Revenue is the sum of...",
  "tags": ["metric", "finance"]
}
```

### Update learning

```json
PUT /api/projects/<pid>/knowledge/<id>/
{
  "type": "learning",
  "description": "Updated description of the learning",
  "category": "type_mismatch",
  "applies_to_tables": ["events", "sessions"]
}
```

### Export

`GET /api/projects/<pid>/knowledge/export/` returns a zip file (`application/zip`) containing one `.md` file per entry with YAML frontmatter.

### Import

`POST /api/projects/<pid>/knowledge/import/` accepts `multipart/form-data` with a `file` field containing a zip of markdown files. Each file must have YAML frontmatter with at least a `title` field.

## Health check

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health/` | Returns `{"status": "ok"}`. No authentication required. |

## OAuth

OAuth login is handled by django-allauth at `/accounts/`. Available providers depend on configuration:

- `/accounts/google/login/` -- Google OAuth
- `/accounts/github/login/` -- GitHub OAuth
