import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, expect, it, vi } from "vitest"

import { ApiError } from "@/api/client"
import { workspaceApi, type WorkspaceMember } from "@/api/workspaces"
import { MembersTab } from "./WorkspaceDetailPage"

vi.mock("@/api/workspaces", () => ({
  workspaceApi: { getMembers: vi.fn(), revokeInvite: vi.fn(), addMember: vi.fn() },
}))

const manager: WorkspaceMember = {
  id: "m-1",
  user_id: "u-1",
  email: "boss@example.com",
  name: "Boss",
  role: "manage",
  created_at: "2026-09-01T10:00:00Z",
}

const joined: WorkspaceMember = {
  ...manager,
  id: "m-2",
  user_id: "u-2",
  email: "new@example.com",
  name: "New",
  role: "read",
}

const pendingInvite = {
  id: "i-1",
  email: "new@example.com",
  role: "read",
  status: "pending",
  created_at: "2026-09-01T10:00:00Z",
} as const

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(workspaceApi.getMembers)
    .mockResolvedValueOnce({ members: [manager], invites: [pendingInvite] })
    .mockResolvedValueOnce({ members: [manager, joined], invites: [] })
})

it("reloads the members when a revoke finds the invite already accepted", async () => {
  vi.mocked(workspaceApi.revokeInvite).mockRejectedValue(
    new ApiError(409, "Invite is no longer live."),
  )
  render(<MembersTab workspaceId="ws-1" isManager />)

  await userEvent.click(await screen.findByTestId("invite-revoke-new@example.com"))
  await userEvent.click(screen.getByTestId("confirm-revoke-invite-new@example.com"))

  expect(await screen.findByTestId("member-row-m-2")).toBeInTheDocument()
  expect(screen.queryByTestId("invite-row-new@example.com")).not.toBeInTheDocument()
  expect(screen.getByTestId("members-mutation-error")).toHaveTextContent(
    "Invite is no longer live.",
  )
})

it("keeps the conflict message and the list when the refresh fails", async () => {
  vi.mocked(workspaceApi.getMembers).mockReset()
  vi.mocked(workspaceApi.getMembers)
    .mockResolvedValueOnce({ members: [manager], invites: [pendingInvite] })
    .mockRejectedValueOnce(new ApiError(500, "Server error"))
  vi.mocked(workspaceApi.revokeInvite).mockRejectedValue(
    new ApiError(409, "Invite is no longer live."),
  )
  render(<MembersTab workspaceId="ws-1" isManager />)

  await userEvent.click(await screen.findByTestId("invite-revoke-new@example.com"))
  await userEvent.click(screen.getByTestId("confirm-revoke-invite-new@example.com"))

  expect(await screen.findByTestId("members-mutation-error")).toHaveTextContent(
    "Invite is no longer live.",
  )
  await waitFor(() => expect(workspaceApi.getMembers).toHaveBeenCalledTimes(2))
  expect(screen.getByTestId("invite-row-new@example.com")).toBeInTheDocument()
  expect(screen.queryByText("Server error")).not.toBeInTheDocument()
})

it("reloads the members when a re-invite finds the invite already accepted", async () => {
  vi.mocked(workspaceApi.addMember).mockRejectedValue(
    new ApiError(409, "Invite is no longer live."),
  )
  render(<MembersTab workspaceId="ws-1" isManager />)

  await userEvent.click(await screen.findByTestId("add-member-button"))
  await userEvent.type(screen.getByTestId("add-member-email"), "new@example.com")
  await userEvent.click(screen.getByTestId("add-member-submit"))

  expect(await screen.findByTestId("member-row-m-2")).toBeInTheDocument()
  expect(screen.queryByTestId("invite-row-new@example.com")).not.toBeInTheDocument()
  expect(screen.getByTestId("add-member-error")).toHaveTextContent("Invite is no longer live.")
})
