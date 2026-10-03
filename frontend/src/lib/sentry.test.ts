import type { ErrorEvent } from "@sentry/react"
import { describe, expect, it } from "vitest"

import { ApiError } from "@/api/client"
import { beforeSend, isNoiseEvent, scrubBreadcrumb, scrubEvent, stripUrl } from "./sentry"

function errorEvent(overrides: Partial<ErrorEvent> = {}): ErrorEvent {
  return {
    type: undefined,
    exception: { values: [{ type: "TypeError", value: "x is not a function" }] },
    ...overrides,
  }
}

describe("stripUrl", () => {
  it("drops query strings and fragments", () => {
    expect(stripUrl("https://scout.example/api/x?token=abc#code=1")).toBe(
      "https://scout.example/api/x",
    )
    expect(stripUrl("/embed#access_token=abc")).toBe("/embed")
    expect(stripUrl("/plain/path")).toBe("/plain/path")
    expect(stripUrl(undefined)).toBeUndefined()
  })
})

describe("scrubEvent", () => {
  it("removes bodies, cookies, auth headers and query strings from the request", () => {
    const event = scrubEvent(
      errorEvent({
        request: {
          url: "https://scout.example/workspaces/1/chat?invite=secret-token",
          query_string: "invite=secret-token",
          cookies: { sessionid_scout: "s3cret" },
          data: { message: "show me patient names" },
          headers: {
            "User-Agent": "Mozilla/5.0",
            Authorization: "Bearer abc",
            Cookie: "sessionid_scout=s3cret",
            Referer: "https://scout.example/?code=oauth-code",
          },
        },
      }),
    )
    expect(event.request).toEqual({
      url: "https://scout.example/workspaces/1/chat",
      headers: { "User-Agent": "Mozilla/5.0" },
    })
  })

  it("keeps only the user id", () => {
    const event = scrubEvent(
      errorEvent({
        user: { id: "user-1", email: "a@example.com", username: "alice", ip_address: "1.2.3.4" },
      }),
    )
    expect(event.user).toEqual({ id: "user-1" })
  })

  it("drops a user with no id", () => {
    expect(scrubEvent(errorEvent({ user: { email: "a@example.com" } })).user).toBeUndefined()
  })

  it("drops extra data and unknown contexts, which can hold API bodies and query results", () => {
    const event = scrubEvent(
      errorEvent({
        extra: { __serialized__: { rows: [["Alice", 42]] } },
        contexts: {
          browser: { name: "Chrome" },
          react: { componentStack: "at ChatPanel" },
          ApiError: { body: { sql: "select * from patients", rows: [] } },
        },
      }),
    )
    expect(event.extra).toBeUndefined()
    expect(event.contexts).toEqual({
      browser: { name: "Chrome" },
      react: { componentStack: "at ChatPanel" },
    })
  })

  it("redacts quoted values in exception and event messages", () => {
    const event = scrubEvent(
      errorEvent({
        message: 'Unexpected token in JSON "{"name":"Alice"}"',
        exception: {
          values: [
            {
              type: "ApiError",
              value: 'invalid input syntax for type integer: "Alice"',
              stacktrace: { frames: [{ filename: "app.js", vars: { message: "secret chat" } }] },
            },
          ],
        },
      }),
    )
    expect(event.message).toBe('Unexpected token in JSON "…"')
    const value = event.exception!.values![0]
    expect(value.value).toBe('invalid input syntax for type integer: "…"')
    expect(value.stacktrace!.frames![0].vars).toBeUndefined()
  })

  it("scrubs breadcrumbs already on the event", () => {
    const event = scrubEvent(
      errorEvent({
        breadcrumbs: [
          { category: "console", message: 'SQL result "Alice"' },
          { category: "fetch", data: { method: "GET", url: "/api/x?token=abc", status_code: 500 } },
        ],
      }),
    )
    expect(event.breadcrumbs).toEqual([
      { category: "fetch", data: { method: "GET", url: "/api/x", status_code: 500 } },
    ])
  })
})

describe("scrubBreadcrumb", () => {
  it("drops console breadcrumbs, which hold raw error text and logged values", () => {
    expect(scrubBreadcrumb({ category: "console", message: 'bad "Alice"' })).toBeNull()
  })

  it("strips query strings from request and navigation URLs", () => {
    expect(
      scrubBreadcrumb({ category: "xhr", data: { url: "/api/a?key=1", status_code: 200 } }),
    ).toEqual({ category: "xhr", data: { url: "/api/a", status_code: 200 } })
    expect(
      scrubBreadcrumb({ category: "navigation", data: { from: "/?code=x", to: "/chat#t=y" } }),
    ).toEqual({ category: "navigation", data: { from: "/", to: "/chat" } })
  })

  it("leaves other breadcrumbs alone", () => {
    const click = { category: "ui.click", message: "button.send" }
    expect(scrubBreadcrumb(click)).toBe(click)
  })
})

describe("isNoiseEvent", () => {
  it("ignores ResizeObserver loop errors", () => {
    const event = errorEvent({
      exception: {
        values: [{ type: "Error", value: "ResizeObserver loop completed with undelivered notifications." }],
      },
    })
    expect(isNoiseEvent(event)).toBe(true)
    expect(isNoiseEvent(errorEvent({ message: "ResizeObserver loop limit exceeded" }))).toBe(true)
  })

  it("ignores errors thrown from browser extensions", () => {
    const event = errorEvent({
      exception: {
        values: [
          {
            type: "TypeError",
            value: "boom",
            stacktrace: { frames: [{ filename: "chrome-extension://abc/content.js" }] },
          },
        ],
      },
    })
    expect(isNoiseEvent(event)).toBe(true)
  })

  it("ignores aborted and dropped requests", () => {
    const abort = new DOMException("The user aborted a request.", "AbortError")
    expect(isNoiseEvent(errorEvent(), { originalException: abort })).toBe(true)
    for (const value of ["Failed to fetch", "NetworkError when attempting to fetch resource.", "Load failed"]) {
      const event = errorEvent({ exception: { values: [{ type: "TypeError", value }] } })
      expect(isNoiseEvent(event)).toBe(true)
    }
  })

  it.each([401, 403, 503])("ignores expected %i API responses", (status) => {
    expect(isNoiseEvent(errorEvent(), { originalException: new ApiError(status, "nope") })).toBe(true)
  })

  it("reports unexpected API failures and ordinary errors", () => {
    expect(isNoiseEvent(errorEvent(), { originalException: new ApiError(500, "boom") })).toBe(false)
    expect(isNoiseEvent(errorEvent(), { originalException: new TypeError("x") })).toBe(false)
    const appFrame = errorEvent({
      exception: {
        values: [
          {
            type: "TypeError",
            value: "Failed to fetch something else",
            stacktrace: { frames: [{ filename: "https://scout.example/assets/index.js" }] },
          },
        ],
      },
    })
    expect(isNoiseEvent(appFrame)).toBe(false)
  })
})

describe("beforeSend", () => {
  it("drops noise and scrubs the rest", () => {
    expect(beforeSend(errorEvent(), { originalException: new ApiError(401, "signed out") })).toBeNull()
    const sent = beforeSend(errorEvent({ user: { id: "u", email: "a@example.com" } }), {})
    expect(sent?.user).toEqual({ id: "u" })
  })
})
