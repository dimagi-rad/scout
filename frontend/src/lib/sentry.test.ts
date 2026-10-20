import * as Sentry from "@sentry/react"
import type { ErrorEvent } from "@sentry/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api/client"
import {
  beforeSend,
  initSentry,
  isNoiseEvent,
  scrubBreadcrumb,
  scrubEvent,
  scrubSpan,
  scrubTransaction,
  stripUrl,
  tracesSampleRate,
} from "./sentry"
import type { SpanJSON, TransactionEvent } from "./sentry"

vi.mock("@sentry/react", () => ({
  init: vi.fn(),
  setUser: vi.fn(),
  browserTracingIntegration: vi.fn(() => ({ name: "BrowserTracing" })),
}))

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

  it("masks workspace slugs and dataset names in paths", () => {
    const id = "0b6e9a52-3c1d-4f7a-9e2b-1a2b3c4d5e6f"
    expect(stripUrl(`https://scout.example/workspaces/acme-health/${id}/chat`)).toBe(
      `https://scout.example/workspaces/:slug/${id}/chat`,
    )
    expect(stripUrl(`/workspaces/acme/${id}`)).toBe(`/workspaces/:slug/${id}`)
    expect(stripUrl(`/api/workspaces/${id}/threads/`)).toBe(`/api/workspaces/${id}/threads/`)
    expect(stripUrl(`/workspaces/${id}/chat/${id}`)).toBe(`/workspaces/${id}/chat/${id}`)
    expect(stripUrl("/datasets/hiv_patients?x=1")).toBe("/datasets/:name")
    expect(stripUrl(`/api/workspaces/${id}/datasets/hiv_patients/`)).toBe(
      `/api/workspaces/${id}/datasets/:name/`,
    )
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
              type: "Error",
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

  it("replaces an ApiError's server text with its status", () => {
    const event = scrubEvent(
      errorEvent({ exception: { values: [{ type: "ApiError", value: "Column patient_name not found" }] } }),
      { originalException: new ApiError(400, "Column patient_name not found") },
    )
    expect(event.exception!.values![0].value).toBe("API request failed (HTTP 400)")
  })

  it("keeps the status of an ApiError that is another error's cause", () => {
    const event = scrubEvent(
      errorEvent({ exception: { values: [{ type: "ApiError", value: "Dataset hiv missing" }] } }),
      { originalException: new Error("load failed", { cause: new ApiError(404, "Dataset hiv missing") }) },
    )
    expect(event.exception!.values![0].value).toBe("API request failed (HTTP 404)")
  })

  it("replaces the raw value of a non-Error promise rejection", () => {
    const event = scrubEvent(
      errorEvent({
        exception: {
          values: [
            {
              type: "UnhandledRejection",
              value: "Non-Error promise rejection captured with value: patient Alice",
              mechanism: { type: "auto.browser.global_handlers.onunhandledrejection", handled: false },
            },
          ],
        },
      }),
      { originalException: "patient Alice" },
    )
    expect(event.exception!.values![0].value).toBe("Non-Error promise rejection")
  })

  it("strips query strings from frame URLs", () => {
    const event = scrubEvent(
      errorEvent({
        exception: {
          values: [
            {
              type: "Error",
              value: "x",
              stacktrace: { frames: [{ filename: "https://scout.example/embed?token=a", abs_path: "/a?b" }] },
            },
          ],
        },
      }),
    )
    expect(event.exception!.values![0].stacktrace!.frames![0]).toEqual({
      filename: "https://scout.example/embed",
      abs_path: "/a",
    })
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

  it("drops attribute values from UI breadcrumbs, which can hold thread titles", () => {
    const click = {
      category: "ui.click",
      message: 'nav > button.flex[type="button"][title="how many "HIV+" patients in Kisumu"]',
    }
    expect(scrubBreadcrumb(click)).toEqual({
      category: "ui.click",
      message: "nav > button.flex[type][title]",
    })
  })

  it("drops attribute values that contain brackets or quotes", () => {
    const click = {
      category: "ui.click",
      message: 'li > a[aria-label="Q3 [draft] "x]y" patients"][title="t"] > span.t',
    }
    expect(scrubBreadcrumb(click)?.message).toBe("li > a[aria-label][title] > span.t")
  })

  it("drops the whole selector when a value defeats the attribute pattern", () => {
    const click = { category: "ui.click", message: 'button[aria-label="Open a"][b"]' }
    expect(scrubBreadcrumb(click)?.message).toBe("[redacted selector]")
  })

  it("leaves other breadcrumbs alone", () => {
    const click = { category: "ui.click", message: "button.send" }
    expect(scrubBreadcrumb(click)).toEqual(click)
    const custom = { category: "app", message: 'kept [title="x"]' }
    expect(scrubBreadcrumb(custom)).toBe(custom)
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

  it("reports a Scout error whose stack passes through an extension wrapper", () => {
    const event = errorEvent({
      exception: {
        values: [
          {
            type: "TypeError",
            value: "boom",
            stacktrace: {
              frames: [
                { filename: "chrome-extension://abc/wrap.js" },
                { filename: "https://scout.example/assets/index.js" },
              ],
            },
          },
        ],
      },
    })
    expect(isNoiseEvent(event)).toBe(false)
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
    const wrapped = new Error("chart failed", { cause: new ApiError(status, "nope") })
    expect(isNoiseEvent(errorEvent(), { originalException: wrapped })).toBe(false)
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

describe("initSentry", () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.clearAllMocks()
  })

  it("does nothing without a DSN", () => {
    vi.stubEnv("VITE_SENTRY_DSN", "")
    initSentry()
    expect(Sentry.init).not.toHaveBeenCalled()
  })

  it("reports errors only, without default PII, when no trace rate is set", () => {
    vi.stubEnv("VITE_SENTRY_DSN", "https://key@o0.ingest.sentry.io/1")
    vi.stubEnv("VITE_SENTRY_RELEASE", "abc123")
    vi.stubEnv("VITE_SENTRY_ENVIRONMENT", "production")
    initSentry()
    const options = vi.mocked(Sentry.init).mock.calls[0][0]!
    expect(options).toMatchObject({
      dsn: "https://key@o0.ingest.sentry.io/1",
      release: "abc123",
      environment: "production",
      sendDefaultPii: false,
      beforeSend,
      beforeBreadcrumb: scrubBreadcrumb,
    })
    expect(options).not.toHaveProperty("tracesSampleRate")
    expect(options).not.toHaveProperty("integrations")
  })
})

describe("tracing", () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.clearAllMocks()
  })

  it("samples traces at the configured rate and scrubs them", () => {
    vi.stubEnv("VITE_SENTRY_DSN", "https://key@o0.ingest.sentry.io/1")
    vi.stubEnv("VITE_SENTRY_TRACES_SAMPLE_RATE", "0.1")
    initSentry()
    const options = vi.mocked(Sentry.init).mock.calls[0][0]!
    expect(options.tracesSampleRate).toBe(0.1)
    expect(options.beforeSendTransaction).toBe(scrubTransaction)
    expect(options.beforeSendSpan).toBe(scrubSpan)
    expect(Sentry.browserTracingIntegration).toHaveBeenCalled()
    expect(options).toMatchObject({ sendDefaultPii: false, beforeSend })
  })

  it("treats a missing or malformed rate as off", () => {
    expect(tracesSampleRate(undefined)).toBe(0)
    expect(tracesSampleRate("")).toBe(0)
    expect(tracesSampleRate("abc")).toBe(0)
    expect(tracesSampleRate("-1")).toBe(0)
    expect(tracesSampleRate("2")).toBe(0)
    expect(tracesSampleRate("0.25")).toBe(0.25)
  })

  it("scrubs URLs and stray context from transactions", () => {
    const id = "0b6e9a52-3c1d-4f7a-9e2b-1a2b3c4d5e6f"
    const event = scrubTransaction({
      type: "transaction",
      transaction: `/workspaces/acme-health/${id}/chat`,
      request: {
        url: `https://scout.example/workspaces/acme-health/${id}/chat?invite=tok`,
        headers: { Cookie: "sessionid_scout=s3cret" },
      },
      user: { id: "u", email: "a@example.com" },
      extra: { body: "raw" },
      contexts: {
        trace: { trace_id: "t", span_id: "s", op: "pageload", data: { url: "/x?token=abc" } },
        custom: { secret: "x" },
      },
      spans: [
        {
          op: "ui.interaction.click",
          description: 'body > button[title="show me patient names"]',
          span_id: "2",
          trace_id: "t",
          start_timestamp: 0,
          data: {},
          origin: "auto",
        },
      ],
    } as TransactionEvent)

    expect(event.transaction).toBe(`/workspaces/:slug/${id}/chat`)
    expect(event.request).toEqual({ url: `https://scout.example/workspaces/:slug/${id}/chat` })
    expect(event.user).toEqual({ id: "u" })
    expect(event.extra).toBeUndefined()
    expect(Object.keys(event.contexts ?? {})).toEqual(["trace"])
    expect(event.contexts?.trace?.data).toEqual({ url: "/x" })
    // Scrubbed here too, not only by the SDK's beforeSendSpan pass.
    expect(event.spans?.[0].description).toBe("body > button[title]")
  })
})

describe("scrubEvent transaction name", () => {
  it("masks the route browser tracing names error events after", () => {
    const id = "0b6e9a52-3c1d-4f7a-9e2b-1a2b3c4d5e6f"
    const event = scrubEvent(errorEvent({ transaction: `/workspaces/acme-health/${id}/chat` }))
    expect(event.transaction).toBe(`/workspaces/:slug/${id}/chat`)
  })
})

describe("scrubSpan", () => {
  const id = "0b6e9a52-3c1d-4f7a-9e2b-1a2b3c4d5e6f"

  function span(overrides: Partial<SpanJSON>): SpanJSON {
    return {
      span_id: "1",
      trace_id: "t",
      start_timestamp: 0,
      data: {},
      origin: "auto",
      ...overrides,
    } as SpanJSON
  }

  it("strips query strings and customer names from request spans", () => {
    const scrubbed = scrubSpan(
      span({
        op: "http.client",
        description: `GET /api/workspaces/${id}/datasets/hiv_patients/?q=name`,
        data: {
          url: "/api/x?token=abc",
          "http.url": "https://scout.example/api/x#frag",
          "http.query": "?token=abc",
          "http.fragment": "#a",
          "http.method": "GET",
        },
      }),
    )
    expect(scrubbed.description).toBe(`GET /api/workspaces/${id}/datasets/:name/`)
    expect(scrubbed.data).toEqual({
      url: "/api/x",
      "http.url": "https://scout.example/api/x",
      "http.method": "GET",
    })
  })

  it("drops attribute values from standalone INP span selectors", () => {
    const scrubbed = scrubSpan(
      span({
        op: "ui.interaction.click",
        description: 'body > button[title="show me patient names"]',
        data: { transaction: `/workspaces/acme-health/${id}/datasets/hiv_patients` },
      }),
    )
    expect(scrubbed.description).toBe("body > button[title]")
    expect(scrubbed.data.transaction).toBe(`/workspaces/:slug/${id}/datasets/:name`)
  })

  it("drops attribute values from web-vital element attributes", () => {
    const scrubbed = scrubSpan(
      span({
        op: "pageload",
        description: `/workspaces/acme/${id}/chat`,
        data: {
          "lcp.element": 'body > button[aria-label="Delete Weekly HIV recap"]',
          "lcp.url": "https://scout.example/img.png?sig=abc",
          "cls.source.1": 'div > button[title="show me patient names"]',
          "browser.web_vital.lcp.element": 'p[title="private"]',
          "lcp.size": 120,
        },
      }),
    )
    expect(scrubbed.description).toBe(`/workspaces/:slug/${id}/chat`)
    expect(scrubbed.data).toEqual({
      "lcp.element": "body > button[aria-label]",
      "lcp.url": "https://scout.example/img.png",
      "cls.source.1": "div > button[title]",
      "browser.web_vital.lcp.element": "p[title]",
      "lcp.size": 120,
    })
  })
})
