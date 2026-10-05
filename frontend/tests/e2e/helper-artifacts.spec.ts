import { expect, test } from "@playwright/test"

const WORKSPACE = "11111111-1111-1111-1111-111111111111"
const THREAD = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
const ARTIFACT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

test("keeps helper artifacts visible in the main reply after reload", async ({ page }, testInfo) => {
  await page.route("**/health/", (route) => route.fulfill({ json: { status: "ok" } }))
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === "/api/auth/me/") return route.fulfill({ json: {
      id: "fixture-user", name: "Chart tester", email: "charts@example.com", onboarding_complete: true,
    } })
    if (path === "/api/workspaces/") return route.fulfill({ json: [{
      id: WORKSPACE, name: "Reports", display_name: "Reports", role: "read", has_access: true,
      tenants: [], member_count: 1, schema_status: "available", is_auto_created: false,
      last_synced_at: null, created_at: "2026-01-01T00:00:00Z",
    }] })
    if (path.endsWith("/freshness/")) return route.fulfill({ json: { sources: [] } })
    if (path.endsWith("/threads/")) return route.fulfill({ json: [{ id: THREAD, title: "Visits by week" }] })
    if (path.endsWith("/jobs/active/")) return route.fulfill({ json: { jobs: [], recent_terminations: [], workspace_loads: [] } })
    if (path.endsWith("/messages/")) return route.fulfill({ json: [{
      id: "reply", role: "assistant", parts: [
        { type: "tool-artifact_manager", toolCallId: "helper", state: "output-available", input: { task: "Build chart" },
          output: { status: "done", artifact_id: ARTIFACT, artifact_version: 1 } },
        { type: "data-subagent-text", data: { parentToolCallId: "helper", text: "Inspected the weekly visits dataset." } },
        { type: "data-subagent-tool-output", data: { parentToolCallId: "helper", toolCallId: "write", toolName: "artifact_write",
          output: { status: "created", artifact: { id: ARTIFACT, version: 1 } } } },
        { type: "text", text: `Your weekly visits chart is ready. [View chart](/workspaces/${WORKSPACE}/artifacts/${ARTIFACT})` },
      ],
    }] })
    if (path.endsWith(`/artifacts/${ARTIFACT}/data/`)) return route.fulfill({ json: {
      id: ARTIFACT, title: "Weekly visits", type: "html", code: "", data: {}, semantic_queries: [], version: 1,
    } })
    if (path.endsWith(`/artifacts/${ARTIFACT}/sandbox/`)) return route.fulfill({ contentType: "text/html", body: "<h2>Weekly visits chart</h2>" })
    if (path.endsWith("/artifacts/")) return route.fulfill({ json: { results: [] } })
    return route.fulfill({ json: {} })
  })
  await page.goto(`/workspaces/${WORKSPACE}/chat/${THREAD}`)
  const helper = page.getByTestId("tool-call-artifact_manager")
  const artifact = page.getByTestId(`chat-artifact-${ARTIFACT}`)
  await expect(helper).toHaveAttribute("aria-expanded", "false")
  await expect(artifact).toBeVisible()
  await expect(artifact).toHaveCount(1)
  await expect(page.getByText("Inspected the weekly visits dataset.")).toHaveCount(0)
  await helper.click()
  await expect(page.getByText("Inspected the weekly visits dataset.")).toBeVisible()
  await helper.click()
  await page.reload()
  await expect(helper).toHaveAttribute("aria-expanded", "false")
  await expect(artifact).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath("helper-artifact-reply.png"), fullPage: true })
  await artifact.click()
  await expect(page.getByText("Weekly visits", { exact: true })).toBeVisible()
})
