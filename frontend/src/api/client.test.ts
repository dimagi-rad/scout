import { afterEach, describe, expect, it, vi } from "vitest"

import { api, ApiError } from "./client"

afterEach(() => vi.unstubAllGlobals())

function failWith(body: unknown, status = 400, statusText = "Bad Request") {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "Content-Type": "application/json" },
  })))
}

describe("API failure messages", () => {
  it("reads semantic error envelopes without displaying diagnostic detail", async () => {
    const body = {
      success: false,
      error: {
        code: "VALIDATION_ERROR",
        message: "Unknown semantic member 'visits.approved_count'.",
        detail: "private SQL diagnostic",
      },
    }
    failWith(body)
    await expect(api.post("/api/workspaces/workspace-1/semantic-query/", {})).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      message: "Unknown semantic member 'visits.approved_count'.",
      body,
    })
  })

  it.each([
    [{ detail: "Your session has expired." }, "Your session has expired."],
    [{ error: "Dataset not found." }, "Dataset not found."],
    [{ detail: "Preferred explanation.", error: "Secondary explanation." }, "Preferred explanation."],
    [{ detail: "", error: { message: "Use a valid date range." } }, "Use a valid date range."],
    [{ message: "Request could not be completed." }, "Request could not be completed."],
    [{ detail: ["Start date is required.", "End date is required."] }, "Start date is required. End date is required."],
    [{ detail: [{ msg: "Enter a valid date.", input: "private submitted value", ctx: { error: "private context" } }] }, "Enter a valid date."],
  ])("normalizes a known message shape: %j", async (body, message) => {
    failWith(body)
    await expect(api.get("/api/example/")).rejects.toMatchObject({ message, body })
  })

  it.each([null, [], {}, 42, false, { error: { code: "BAD_INPUT", input: "private input" } }])(
    "keeps the HTTP failure when the JSON body has no known message: %j",
    async (body) => {
      failWith(body, 502, "")
      const error = await api.get("/api/example/").catch((failure: unknown) => failure)
      expect(error).toBeInstanceOf(ApiError)
      expect(error).toMatchObject({ status: 502, message: "Request failed (HTTP 502).", body })
    },
  )

  it("does not put an HTML gateway response into the user-facing message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>private proxy diagnostic</html>", {
      status: 502,
      statusText: "Bad Gateway",
      headers: { "Content-Type": "text/html" },
    })))
    await expect(api.get("/api/example/")).rejects.toMatchObject({
      status: 502,
      message: "Bad Gateway",
    })
  })

  it("uses the same error explanation for a failed download and preserves access metadata", async () => {
    const body = { error: { message: "Access to this workspace was removed." }, reason: "tenant_access_lost" }
    failWith(body, 403, "Forbidden")
    await expect(api.getBlob("/api/workspaces/workspace-1/knowledge/export/")).rejects.toMatchObject({
      status: 403,
      message: "Access to this workspace was removed.",
      body,
    })
  })

  it("leaves successful JSON, empty responses and downloads unchanged", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ rows: [[7]] })))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response("test export", { headers: { "Content-Type": "text/plain" } }))
    vi.stubGlobal("fetch", fetchMock)
    await expect(api.get("/api/example/")).resolves.toEqual({ rows: [[7]] })
    await expect(api.delete("/api/example/")).resolves.toBeUndefined()
    const blob = await api.getBlob("/api/example/export/")
    expect(await blob.text()).toBe("test export")
    expect(fetchMock).toHaveBeenCalledWith("/api/example/export/", { credentials: "include" })
  })
})
