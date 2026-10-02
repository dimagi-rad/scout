import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { ApiError } from "@/api/client"
import { ChatErrorNotice } from "./ChatStatus"
import { ACCESS_RETRY_MESSAGE, GENERIC_CHAT_ERROR_MESSAGE, classifyChatError } from "./chatErrors"

function bodyError(body: unknown) {
  return new Error(JSON.stringify(body))
}

const RECONNECT_TEXT = "Your CommCare connection has expired. Reconnect it in Connected Accounts."

function renderNotice(error: Error) {
  const onRetry = vi.fn()
  const onStartNewThread = vi.fn()
  render(
    <MemoryRouter>
      <ChatErrorNotice error={error} onRetry={onRetry} onStartNewThread={onStartNewThread} />
    </MemoryRouter>,
  )
  return { onRetry, onStartNewThread }
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe("classifyChatError", () => {
  it.each(["verification_unavailable", "verification_in_progress"])(
    "treats %s as a retryable access check",
    (reason) => {
      const error = bodyError({ error: "x", reason, retryable: true, recovery_url: "/settings/connections" })
      expect(classifyChatError(error)).toEqual({ kind: "access-retry" })
    },
  )

  it.each(["credential_expired", "credential_missing", "upstream_access_lost", "tenant_access_lost"])(
    "treats %s as needing a reconnect, with the backend text",
    (reason) => {
      const error = bodyError({ error: RECONNECT_TEXT, reason, recovery_url: "/settings/connections" })
      expect(classifyChatError(error)).toEqual({
        kind: "access-reconnect",
        message: RECONNECT_TEXT,
        recoveryPath: "/settings/connections",
      })
    },
  )

  it.each(["no_sources", "message_too_long"])(
    "treats %s as final, with the backend text",
    (reason) => {
      expect(classifyChatError(bodyError({ error: "Backend remedy.", reason }))).toEqual({
        kind: "final",
        message: "Backend remedy.",
      })
    },
  )

  it("falls back to Connected Accounts for a missing or off-site recovery_url", () => {
    for (const recovery_url of [undefined, "https://evil.example/", "//evil.example/", "/\\evil.example/", 42]) {
      const error = bodyError({ error: RECONNECT_TEXT, reason: "credential_expired", recovery_url })
      expect(classifyChatError(error)).toMatchObject({ recoveryPath: "/settings/connections" })
    }
  })

  it.each([
    ["an unknown reason", bodyError({ error: "internal detail", reason: "something_new" })],
    ["no reason", bodyError({ error: "Agent initialization failed. Ref: abc" })],
    ["a reconnect reason without text", bodyError({ reason: "credential_expired" })],
    ["a non-JSON body", new Error("<html>502 Bad Gateway</html>")],
    ["a JSON scalar", new Error("null")],
  ])("treats %s as generic", (_label, error) => {
    expect(classifyChatError(error)).toEqual({ kind: "generic" })
  })

  it("keeps the stale-thread case", () => {
    expect(classifyChatError(new ApiError(404, "gone"))).toEqual({ kind: "stale" })
    expect(classifyChatError(bodyError({ error: "Thread not found" }))).toEqual({ kind: "stale" })
  })
})

describe("ChatErrorNotice", () => {
  it("offers Retry for an unconfirmed access check, without the backend text", () => {
    const { onRetry } = renderNotice(
      bodyError({ error: "BACKEND TEXT", reason: "verification_unavailable", retryable: true }),
    )
    expect(screen.getByTestId("chat-error-message")).toHaveTextContent(ACCESS_RETRY_MESSAGE)
    expect(screen.queryByText("BACKEND TEXT")).toBeNull()
    expect(screen.queryByTestId("chat-error-recovery-link")).toBeNull()
    fireEvent.click(screen.getByTestId("chat-error-retry"))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it("shows the backend text and a Connected Accounts link, not Retry, for a reconnect", () => {
    renderNotice(
      bodyError({
        error: RECONNECT_TEXT,
        reason: "credential_expired",
        retryable: false,
        recovery_url: "/settings/connections",
      }),
    )
    expect(screen.getByTestId("chat-error-message")).toHaveTextContent(RECONNECT_TEXT)
    const link = screen.getByTestId("chat-error-recovery-link")
    expect(link).toHaveTextContent("Connected Accounts")
    expect(link).toHaveAttribute("href", "/settings/connections")
    expect(screen.queryByTestId("chat-error-retry")).toBeNull()
  })

  it("never renders an unrecognised body and keeps Retry", () => {
    const { onRetry } = renderNotice(
      bodyError({ error: "Traceback: secret detail", reason: "something_new" }),
    )
    expect(screen.getByTestId("chat-error-message")).toHaveTextContent(GENERIC_CHAT_ERROR_MESSAGE)
    expect(screen.queryByText(/secret detail/)).toBeNull()
    fireEvent.click(screen.getByTestId("chat-error-retry"))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it("shows the backend text without Retry when resending cannot succeed", () => {
    renderNotice(bodyError({ error: "This workspace has no data sources.", reason: "no_sources" }))
    expect(screen.getByTestId("chat-error-message")).toHaveTextContent(
      "This workspace has no data sources.",
    )
    expect(screen.queryByTestId("chat-error-retry")).toBeNull()
  })

  it("offers a new chat next to Retry for a generic failure", () => {
    const { onStartNewThread } = renderNotice(new Error("<html>502</html>"))
    fireEvent.click(screen.getByTestId("chat-error-new-thread"))
    expect(onStartNewThread).toHaveBeenCalledOnce()
  })

  it("offers a new chat for a stale thread", () => {
    const { onStartNewThread } = renderNotice(new ApiError(404, "Thread not found"))
    expect(screen.queryByTestId("chat-error-retry")).toBeNull()
    fireEvent.click(screen.getByTestId("chat-error-new-thread"))
    expect(onStartNewThread).toHaveBeenCalledOnce()
  })
})
