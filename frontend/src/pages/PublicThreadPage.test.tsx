import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { PublicThreadPage } from "@/pages/PublicThreadPage"

// Issue #240, finding 00#8: the shared/public thread page must SANDBOX-RENDER
// artifacts (self-contained html/svg in an iframe) instead of always dumping
// the source as <pre>. React/Plotly artifacts (which need the CDN renderer +
// authed live data) keep showing source for anonymous viewers.

const TOKEN = "share-tok-123"
const WS = "ws-1"

// A message whose assistant turn created the artifact, so the page renders a
// clickable artifact button that opens the preview.
function messageFor(artifactId: string) {
  return {
    id: "m1",
    role: "assistant",
    parts: [
      {
        type: "tool-create_artifact",
        toolName: "create_artifact",
        state: "output-available",
        output: { artifact_id: artifactId },
      },
    ],
  }
}

function thread(artifact: Record<string, unknown>) {
  return {
    thread: { id: "t1", title: "Shared", created_at: "2026-01-01T00:00:00Z" },
    messages: [messageFor(artifact.id as string)],
    artifacts: [artifact],
  }
}

function mockFetch(payload: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => payload,
    })),
  )
}

describe("PublicThreadPage artifact rendering (issue #240)", () => {
  beforeEach(() => {
    window.history.pushState({}, "", `/shared/threads/${TOKEN}`)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("renders a self-contained html artifact in a sandboxed iframe", async () => {
    const art = {
      id: "a1",
      title: "HTML Report",
      artifact_type: "html",
      code: "<h1>Hello</h1>",
      data: {},
      version: 1,
      workspace_id: WS,
    }
    mockFetch(thread(art))

    render(<PublicThreadPage />)

    const button = await screen.findByText("HTML Report")
    await userEvent.click(button)

    const frame = await screen.findByTestId(`public-artifact-frame-${art.id}`)
    expect(frame.tagName).toBe("IFRAME")
    // SECURITY: untrusted markup must be sandboxed with NO allow-same-origin
    // and (for static html/svg) NO allow-scripts.
    expect(frame.getAttribute("sandbox")).toBe("")
    expect(frame.getAttribute("srcdoc")).toContain("<h1>Hello</h1>")
  })

  it("falls back to a <pre> code view for react artifacts", async () => {
    const art = {
      id: "a2",
      title: "React Chart",
      artifact_type: "react",
      code: "export default function C(){return <div/>}",
      data: {},
      version: 1,
      workspace_id: WS,
    }
    mockFetch(thread(art))

    render(<PublicThreadPage />)

    const button = await screen.findByText("React Chart")
    await userEvent.click(button)

    const code = await screen.findByTestId(`public-artifact-code-${art.id}`)
    expect(code.tagName).toBe("PRE")
    expect(code.textContent).toContain("export default function C")
    expect(
      screen.queryByTestId(`public-artifact-frame-${art.id}`),
    ).not.toBeInTheDocument()
  })
})
