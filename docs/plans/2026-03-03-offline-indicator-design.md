# Offline Indicator Design

**Date:** 2026-03-03
**Branch:** bdr/offline-indicator

## Problem

When the dev server is offline, the app silently fails in inconsistent ways:
- Some pages show resource-specific errors ("Failed to load recipes")
- Chat silently does nothing
- No indication that the root cause is server connectivity

## Solution: Global Banner + Per-Page Passthrough (Option A)

A single global connectivity indicator that surfaces the root cause clearly, lets users browse cached content freely, and auto-recovers when the server comes back.

## Architecture

### Network Detection (`useNetworkStatus` hook)

- Polls `GET /health/` every 5 seconds
- Uses `navigator.onLine` as a fast first signal for immediate browser-level detection
- Tracks `status: "online" | "offline" | "reconnecting"`
- On reconnect: triggers re-fetch of errored store slices, then transitions banner to "Reconnected" for ~2s before dismissing

### Banner Component (`OfflineBanner`)

Fixed position banner (bottom of viewport to avoid blocking top nav), three states:

| State | Icon | Message |
|-------|------|---------|
| `offline` | `WifiOff` | "Server unreachable — attempting to reconnect..." + pulse spinner |
| `reconnecting` | `Wifi` | "Reconnected" + green checkmark, auto-dismisses after 2s |
| `online` | hidden | — |

Uses existing shadcn/Tailwind design tokens. Slides in/out with a smooth transition.

### Per-Page Error Suppression

- When banner is showing (`status !== "online"`), per-page error states are suppressed — no redundant "Failed to load X" messages
- When server is back online, any store slice in `"error"` state auto-retries
- For errors that occur when server *is* online (real server errors), pages show a simplified generic message

## Key Files

- `frontend/src/hooks/useNetworkStatus.ts` — new hook
- `frontend/src/components/OfflineBanner/OfflineBanner.tsx` — new banner component
- `frontend/src/components/AppLayout/AppLayout.tsx` — mount banner here
- Store slices — add re-fetch trigger on reconnect
- Per-page error UIs — suppress when offline

## Health Endpoint

`GET /health/` already exists at `config/urls.py`. No backend changes needed.

## Non-Goals

- Blocking navigation when offline (users can freely browse cached content)
- Queueing mutations for replay when back online
- Service worker / true offline mode
