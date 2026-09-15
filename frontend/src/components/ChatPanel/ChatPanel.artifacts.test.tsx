import type { UIMessage } from "ai"
import { StrictMode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import type { TenantMembership } from "@/store/domainSlice"
import { useAppStore } from "@/store/store"
import { ChatPanel } from "./ChatPanel"
import type { ThreadArtifactSummary } from "./ChatThreadSidePanel"

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
    recentlyCompletedThreadIds: [],
    recentTerminationsByToolCallId: {},
    notifyJobLikelyStarted: vi.fn(),
  }),
}))

const WS_A = "11111111-1111-1111-1111-111111111111"
const WS_B = "22222222-2222-2222-2222-222222222222"
const THREAD_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const THREAD_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
const OLD_TITLE = "Previous thread's artifact"
const NEW_TITLE = "Current thread's artifact"
const OLD_ERROR = "Previous thread's artifact request failed"

type DeferredArtifactRequest = {
  workspaceId: string
  threadId: string
  resolve: (response: Response) => void
}

function workspace(id: string, name: string): TenantMembership {
  return {
    id, name, display_name: name, is_auto_created: false, role: "manage", tenants: [],
    member_count: 1, schema_status: "available", last_synced_at: null, created_at: "2026-01-01",
  }
}

function artifact(title: string): ThreadArtifactSummary {
  return {
    id: title === OLD_TITLE ? "old-artifact" : "current-artifact",
    title,
    description: "",
    artifact_type: "story",
    version: 1,
    source: "created",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    linked_at: "2026-01-01T00:00:00Z",
    last_seen_at: "2026-01-01T00:00:00Z",
  }
}

function historyText(workspaceId: string, threadId: string) {
  return `Saved conversation ${workspaceId}/${threadId}`
}

function mockThreadApi() {
  const artifactRequests: DeferredArtifactRequest[] = []
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const match = url.match(/^\/api\/workspaces\/([^/]+)\/threads\/([^/]+)\/(messages|artifacts)\/$/)
    if (!match) throw new Error(`Unexpected request: ${url}`)
    const [, workspaceId, threadId, resource] = match
    if (resource === "messages") {
      const messages: UIMessage[] = [{
        id: `history-${workspaceId}-${threadId}`,
        role: "assistant",
        parts: [{ type: "text", text: historyText(workspaceId, threadId) }],
      }]
      return Response.json(messages)
    }
    return new Promise<Response>((resolve) => {
      artifactRequests.push({ workspaceId, threadId, resolve })
    })
  }))
  return { artifactRequests }
}

function renderChat(strict = false) {
  const chat = <MemoryRouter><ChatPanel /></MemoryRouter>
  return render(strict ? <StrictMode>{chat}</StrictMode> : chat)
}

function visibleArtifacts() {
  // The collapsed aside stays mounted: text queries alone can mistake its
  // hidden "No artifacts" placeholder for the state of the open pane.
  const pane = screen.getByRole("complementary")
  expect(pane).toHaveAttribute("aria-hidden", "false")
  expect(within(pane).getByRole("heading", { name: "Artifacts", level: 2 })).toBeVisible()
  return within(pane)
}

async function openArtifacts(api: ReturnType<typeof mockThreadApi>) {
  expect(screen.queryByRole("complementary")).not.toBeInTheDocument()
  const previousCount = api.artifactRequests.length
  fireEvent.click(await screen.findByRole("button", { name: "Artifacts" }))
  visibleArtifacts()
  await waitFor(() => expect(api.artifactRequests.length).toBeGreaterThan(previousCount))
  return api.artifactRequests.at(-1)!
}

async function selectContext(workspaceId: string, threadId: string) {
  await act(async () => {
    useAppStore.setState({ activeDomainId: workspaceId, threadId })
  })
  await screen.findByText(historyText(workspaceId, threadId))
}

async function finish(request: DeferredArtifactRequest, response: Response) {
  await act(async () => { request.resolve(response) })
}

function success(title: string) {
  return Response.json({ results: [artifact(title)] })
}

const lateResponses = [
  { name: "a stale nonempty list", response: () => success(OLD_TITLE) },
  { name: "a stale empty list", response: () => Response.json({ results: [] }) },
  { name: "a stale error", response: () => Response.json({ error: OLD_ERROR }, { status: 500 }) },
]

function expectCurrentArtifacts() {
  const pane = visibleArtifacts()
  expect(pane.getByRole("button", { name: NEW_TITLE })).toBeVisible()
  expect(pane.queryByText(OLD_TITLE)).not.toBeInTheDocument()
  expect(pane.queryByText(OLD_ERROR)).not.toBeInTheDocument()
  expect(pane.queryByText("No artifacts")).not.toBeInTheDocument()
  expect(pane.queryByText("Artifacts unavailable")).not.toBeInTheDocument()
  expect(pane.getByRole("button", { name: "Refresh artifacts" })).toBeEnabled()
}

beforeEach(() => {
  localStorage.clear()
  useAppStore.setState({
    domains: [workspace(WS_A, "Workspace A"), workspace(WS_B, "Workspace B")],
    domainsStatus: "loaded", activeDomainId: WS_A, threadId: THREAD_A,
    threads: [], threadsStatus: "loaded", threadsAccessLostMessage: null,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("ChatPanel artifact response isolation", () => {
  describe.each([
    { label: "thread", nextWorkspaceId: WS_A },
    { label: "workspace and thread", nextWorkspaceId: WS_B },
  ])("after switching $label", ({ nextWorkspaceId }) => {
    it.each(lateResponses)("ignores $name after the new context succeeds", async ({ response }) => {
      const api = mockThreadApi()
      renderChat()
      await screen.findByText(historyText(WS_A, THREAD_A))
      const previousRequest = await openArtifacts(api)
      expect(previousRequest).toMatchObject({ workspaceId: WS_A, threadId: THREAD_A })

      await selectContext(nextWorkspaceId, THREAD_B)
      const currentRequest = await openArtifacts(api)
      expect(currentRequest).toMatchObject({ workspaceId: nextWorkspaceId, threadId: THREAD_B })
      await finish(currentRequest, success(NEW_TITLE))
      expectCurrentArtifacts()

      await finish(previousRequest, response())
      expectCurrentArtifacts()
    })
  })

  it.each([lateResponses[0], lateResponses[2]])("keeps the new request loading when $name completes first", async ({ response }) => {
    const api = mockThreadApi()
    renderChat()
    await screen.findByText(historyText(WS_A, THREAD_A))
    const previousRequest = await openArtifacts(api)
    await selectContext(WS_B, THREAD_B)
    const currentRequest = await openArtifacts(api)

    await finish(previousRequest, response())
    const pane = visibleArtifacts()
    expect(pane.getByText("Loading artifacts")).toBeVisible()
    expect(pane.getByRole("button", { name: "Refresh artifacts" })).toBeDisabled()
    expect(pane.queryByText(OLD_TITLE)).not.toBeInTheDocument()
    expect(pane.queryByText(OLD_ERROR)).not.toBeInTheDocument()
    await finish(currentRequest, success(NEW_TITLE))
    expectCurrentArtifacts()
  })

  it("clears the old list when only the workspace changes", async () => {
    const api = mockThreadApi()
    renderChat()
    await screen.findByText(historyText(WS_A, THREAD_A))
    const previousRequest = await openArtifacts(api)
    await finish(previousRequest, success(OLD_TITLE))
    expect(visibleArtifacts().getByText(OLD_TITLE)).toBeVisible()

    await selectContext(WS_B, THREAD_A)
    const currentRequest = await openArtifacts(api)
    expect(currentRequest).toMatchObject({ workspaceId: WS_B, threadId: THREAD_A })
    expect(visibleArtifacts().queryByText(OLD_TITLE)).not.toBeInTheDocument()
    expect(visibleArtifacts().getByText("Loading artifacts")).toBeVisible()
    await finish(currentRequest, success(NEW_TITLE))
    expectCurrentArtifacts()
  })

  it.each(lateResponses)("lets a newer same-context refresh win over $name", async ({ response }) => {
    const api = mockThreadApi()
    renderChat()
    await screen.findByText(historyText(WS_A, THREAD_A))
    const initialRequest = await openArtifacts(api)
    await finish(initialRequest, success(OLD_TITLE))

    const previousCount = api.artifactRequests.length
    fireEvent.click(visibleArtifacts().getByRole("button", { name: "Refresh artifacts" }))
    await waitFor(() => expect(api.artifactRequests).toHaveLength(previousCount + 1))
    const previousRefresh = api.artifactRequests.at(-1)!
    expect(visibleArtifacts().getByRole("button", { name: "Refresh artifacts" })).toBeDisabled()

    // Closing/reopening is a real way to request a newer snapshot while the
    // refresh is pending; never force-click the disabled refresh control.
    fireEvent.click(visibleArtifacts().getByRole("button", { name: "Close panel" }))
    const currentRefresh = await openArtifacts(api)
    expect(currentRefresh).toMatchObject({ workspaceId: WS_A, threadId: THREAD_A })
    await finish(currentRefresh, success(NEW_TITLE))
    expectCurrentArtifacts()
    await finish(previousRefresh, response())
    expectCurrentArtifacts()
  })

  it.each([lateResponses[0], lateResponses[2]])("ignores $name from the first A visit after A → B → A", async ({ response }) => {
    const api = mockThreadApi()
    renderChat()
    await screen.findByText(historyText(WS_A, THREAD_A))
    const firstARequest = await openArtifacts(api)
    await selectContext(WS_B, THREAD_B)
    const bRequest = await openArtifacts(api)
    await finish(bRequest, success("Workspace B artifact"))

    await selectContext(WS_A, THREAD_A)
    const currentARequest = await openArtifacts(api)
    expect(currentARequest).toMatchObject({ workspaceId: WS_A, threadId: THREAD_A })
    await finish(currentARequest, success(NEW_TITLE))
    await finish(firstARequest, response())
    expectCurrentArtifacts()
  })

  it.each(lateResponses)("keeps a remounted StrictMode panel independent of $name after unmount", async ({ response }) => {
    const api = mockThreadApi()
    const oldView = renderChat(true)
    await screen.findByText(historyText(WS_A, THREAD_A))
    const unmountedRequest = await openArtifacts(api)
    oldView.unmount()

    renderChat(true)
    await screen.findByText(historyText(WS_A, THREAD_A))
    const currentRequest = await openArtifacts(api)
    await finish(currentRequest, success(NEW_TITLE))
    const requestCount = api.artifactRequests.length
    await finish(unmountedRequest, response())
    expectCurrentArtifacts()
    expect(api.artifactRequests).toHaveLength(requestCount)
  })
})
