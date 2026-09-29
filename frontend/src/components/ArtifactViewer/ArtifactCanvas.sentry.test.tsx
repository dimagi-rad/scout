import { act, render, screen } from "@testing-library/react"
import * as Sentry from "@sentry/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { ArtifactDetail } from "@/components/ArtifactGraph"
import { resetReportedRenderErrorsForTests } from "@/lib/reportRenderError"
import { ArtifactCanvas } from "./ArtifactCanvas"

const scope = vi.hoisted(() => ({ setTag: vi.fn(), addEventProcessor: vi.fn() }))

vi.mock("@sentry/react", () => ({
  captureException: vi.fn(),
  withScope: vi.fn((callback: (s: typeof scope) => void) => callback(scope)),
}))

const artifact: ArtifactDetail = {
  id: "artifact-one",
  title: "Sandboxed chart",
  type: "react",
  code: "",
  version: 3,
  semantic_queries: [],
  data: { rows: [{ patient: "Customer Name", visits: 7 }] },
}

const sandboxError = {
  type: "artifact-error",
  error: {
    title: "React Render Error",
    message: `Cannot read properties of undefined (reading 'visits') near "Customer Name"`,
    details: [
      "TypeError: Cannot read properties of undefined (reading 'visits')",
      "    at App (eval at render (sandbox:1:1), <anonymous>:4:12)",
      "    at renderWithHooks (react-dom.js:10:5)",
    ].join("\n"),
  },
  data: artifact.data,
}

function renderCanvas() {
  const view = render(
    <ArtifactCanvas artifactId={artifact.id} workspaceId="workspace" artifact={artifact} isLoading={false} error={null} />,
  )
  const frame = screen.getByTitle(artifact.title) as HTMLIFrameElement
  const post = (data: unknown, source: Window | null = frame.contentWindow) =>
    act(() => {
      window.dispatchEvent(new MessageEvent("message", { data, source }))
    })
  return { ...view, post }
}

describe("ArtifactCanvas sandbox error reporting", () => {
  beforeEach(() => resetReportedRenderErrorsForTests())
  afterEach(() => vi.clearAllMocks())

  it("reports an iframe render error once, with only safe context", () => {
    const { post, rerender } = renderCanvas()

    post(sandboxError)

    expect(Sentry.captureException).toHaveBeenCalledTimes(1)
    const reported = vi.mocked(Sentry.captureException).mock.calls[0][0] as Error
    expect(reported.name).toBe("TypeError")
    expect(reported.message).toBe("Cannot read properties of undefined (reading '…') near \"…\"")
    expect(reported.stack).toBe([
      "TypeError: Cannot read properties of undefined (reading '…') near \"…\"",
      "    at App (eval at render (sandbox:1:1), <anonymous>:4:12)",
      "    at renderWithHooks (react-dom.js:10:5)",
    ].join("\n"))
    expect(JSON.stringify([reported.message, reported.stack])).not.toContain("Customer Name")
    expect(scope.setTag).toHaveBeenCalledWith("artifact_id", "artifact-one")
    expect(scope.setTag).toHaveBeenCalledWith("artifact_version", "3")
    expect(scope.setTag).toHaveBeenCalledWith("render_error_stage", "React Render Error")
    expect(scope.setTag).toHaveBeenCalledWith("render_error_source", "sandbox")

    post(sandboxError)
    rerender(
      <ArtifactCanvas artifactId={artifact.id} workspaceId="workspace" artifact={{ ...artifact }} isLoading={false} error={null} />,
    )
    post(sandboxError)
    expect(Sentry.captureException).toHaveBeenCalledTimes(1)
  })

  it("ignores artifact-error messages from any window but its own iframe", () => {
    const { post } = renderCanvas()

    post(sandboxError, window)

    expect(Sentry.captureException).not.toHaveBeenCalled()
  })
})
