import { describe, expect, it } from "vitest"

import { resolveSlashCommand } from "./slashCommands"

describe("refresh-data command", () => {
  it("respects in-flight work and acknowledges asynchronous dispatch", () => {
    const prompt = resolveSlashCommand("/refresh-data")
    expect(prompt).toContain("if a refresh is already in progress, do not start another one")
    expect(prompt).toContain("call run_materialization once and report its actual result")
    expect(prompt).toContain("acknowledge that the background job has started and end this turn")
    expect(prompt).toContain("do not claim the sync is complete or query the new data yet")
    expect(prompt).not.toContain("then confirm when the sync is complete")
  })

  it("does not promise a resume for another thread or a failed job", () => {
    const prompt = resolveSlashCommand("/refresh-data")
    expect(prompt).toContain("Only promise an automatic follow-up when the tool confirms a job for this conversation")
    expect(prompt).toContain("If the tool reports failure, explain the required next step")
  })

  it("routes read-only members to a write-capable member instead of a refresh", () => {
    const prompt = resolveSlashCommand("/refresh-data")
    expect(prompt).toContain("If the run_materialization tool is not available to you, do not attempt a refresh")
    expect(prompt).toContain("a workspace member with write access needs to run it")
  })

  it("preserves additional user context", () => {
    expect(resolveSlashCommand("/refresh-data  OCS messages only  ")).toBe(
      `${resolveSlashCommand("/refresh-data")}\n\nOCS messages only`,
    )
  })

  it("leaves ordinary messages and unknown commands unchanged", () => {
    expect(resolveSlashCommand("refresh my data")).toBe("refresh my data")
    expect(resolveSlashCommand("/unknown data")).toBe("/unknown data")
  })
})

describe("save-recipe command", () => {
  it("tells read-only members that saving a recipe needs write access", () => {
    const prompt = resolveSlashCommand("/save-recipe")
    expect(prompt).toContain("If the save_as_recipe tool is not available to you, do not attempt to save the recipe")
    expect(prompt).toContain("requires write access to this workspace")
  })
})
