import { expect, test, type Page } from "@playwright/test"

const OWNER = "11111111-1111-1111-1111-111111111111"
const OTHER = "22222222-2222-2222-2222-222222222222"
const ARTIFACT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const LINK = `/workspaces/owner/${OWNER}/artifacts/${ARTIFACT}`

async function mockBackend(page: Page, artifactStatus = 200, dataReady = Promise.resolve()) {
  const artifactRequests: string[] = []
  const unexpected: string[] = []
  await page.route("**/health/", (route) => route.fulfill({ json: { status: "ok" } }))
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.includes(`/artifacts/${ARTIFACT}/`)) artifactRequests.push(path)
    if (path === "/api/auth/csrf/") return route.fulfill({ json: {} })
    if (path === "/api/auth/me/") {
      return route.fulfill({ json: { id: "fixture-user", name: "Link tester", email: "links@example.com", onboarding_complete: true } })
    }
    if (path === "/api/workspaces/") {
      return route.fulfill({ json: [[OTHER, "Other"], [OWNER, "Owner"]].map(([id, name]) => ({
        id, name, display_name: name, role: "read", has_access: true,
        tenants: [], member_count: 1, schema_status: "available", is_auto_created: false,
        last_synced_at: null, created_at: "2026-01-01T00:00:00Z",
      })) })
    }
    if (path.endsWith("/threads/")) return route.fulfill({ json: [] })
    if (path.endsWith("/jobs/active/")) return route.fulfill({ json: { jobs: [], recent_terminations: [] } })
    if (path === `/api/workspaces/${OWNER}/artifacts/${ARTIFACT}/data/`) {
      await dataReady
      return route.fulfill({ status: artifactStatus, json: artifactStatus === 200 ? {
        id: ARTIFACT, title: "Owning workspace artifact", type: "html", code: "",
        data: {}, semantic_queries: [], version: 1,
      } : { error: "Artifact access denied" } })
    }
    if (path === `/api/workspaces/${OWNER}/artifacts/${ARTIFACT}/sandbox/`) {
      return route.fulfill({ contentType: "text/html", body: "<!doctype html><html><body><h2>Synthetic artifact fixture</h2><p>This test uses no live provider data.</p></body></html>" })
    }
    if (path === `/api/workspaces/${OTHER}/artifacts/`) return route.fulfill({ json: { results: [] } })
    unexpected.push(path)
    return route.fulfill({ status: 404, json: { error: "Unexpected fixture request" } })
  })
  return { artifactRequests, unexpected }
}

test("opens and reloads an owner link while another workspace defaults, then switches safely", async ({ page }, testInfo) => {
  let allowData!: () => void
  const dataReady = new Promise<void>((resolve) => { allowData = resolve })
  const requests = await mockBackend(page, 200, dataReady)
  await page.goto(LINK)
  await expect(page.getByTestId("domain-selector")).toContainText("Other")
  allowData()
  await expect(page.getByTestId("artifact-detail-title")).toHaveText("Owning workspace artifact")
  await expect(page.getByTestId("domain-selector")).toContainText("Owner")

  const copiedLink = page.url()
  await page.reload()
  await expect(page.getByTestId("artifact-detail-title")).toHaveText("Owning workspace artifact")
  await expect(page.getByTestId("domain-selector")).toContainText("Owner")
  await expect(page).toHaveURL(copiedLink)
  await expect(page.frameLocator(`[data-testid="artifact-frame-${ARTIFACT}"]`).getByRole("heading")).toHaveText("Synthetic artifact fixture")
  await page.screenshot({ path: testInfo.outputPath("artifact-owner-link.png"), fullPage: true })

  await page.getByTestId("domain-selector").click()
  await page.getByTestId("workspace-search").fill("Other")
  await page.getByTestId(`domain-item-${OTHER}`).click()
  await expect(page).toHaveURL(/\/artifacts$/)
  await expect(page.getByTestId("domain-selector")).toContainText("Other")
  await expect(page.getByText("No artifacts yet.", { exact: false })).toBeVisible()
  expect(requests.artifactRequests.every((path) => path.startsWith(`/api/workspaces/${OWNER}/`))).toBe(true)
  expect(requests.unexpected).toEqual([])
})

for (const status of [403, 404]) {
  test(`preserves scoped ${status} without querying a different workspace`, async ({ page }) => {
    const requests = await mockBackend(page, status)
    await page.goto(LINK)
    await expect(page.getByText("Artifact access denied")).toBeVisible()
    await expect(page.getByTestId("domain-selector")).toContainText("Other")
    await expect(page.getByTestId(`artifact-frame-${ARTIFACT}`)).toHaveCount(0)
    await expect(page).toHaveURL(new RegExp(`${ARTIFACT}$`))
    expect(requests.artifactRequests.every((path) => path.startsWith(`/api/workspaces/${OWNER}/`))).toBe(true)
    expect(requests.unexpected).toEqual([])
  })
}
