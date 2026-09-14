import type { Meta, StoryObj } from "@storybook/react-vite"

import { ArtifactDataRecovery } from "./ArtifactDataRecovery"
import type { ArtifactDataRecoveryState } from "./types"

const meta = {
  title: "Artifact System/Data Recovery",
  component: ArtifactDataRecovery,
  tags: ["autodocs"],
  decorators: [
    (Story) => (
      <div className="flex min-h-[520px] min-w-[720px] bg-background">
        <Story />
      </div>
    ),
  ],
  args: {
    error: null,
    isChecking: false,
    isStarting: false,
    onRecover: () => undefined,
    onRetryCheck: () => undefined,
  },
} satisfies Meta<typeof ArtifactDataRecovery>

export default meta
type Story = StoryObj<typeof meta>

const offline: ArtifactDataRecoveryState = {
  status: "needs_materialization",
  recovery_action: "materialization",
  physical_status: "expired",
  semantic_status: "blocked",
  message: "The data behind this artifact is no longer available. Restore the workspace data to use it again.",
  can_retry: true,
  recovery: null,
}

export const DataOffline: Story = {
  args: { state: offline },
}

export const RebuildingSemanticModel: Story = {
  args: {
    state: {
      status: "needs_semantic_rebuild",
      recovery_action: "semantic_rebuild",
      physical_status: "active",
      semantic_status: "missing",
      message: "The workspace data is available, but its semantic model needs to be rebuilt before this artifact can query it.",
      can_retry: true,
      recovery: null,
    },
  },
}

export const RestoringWithProgress: Story = {
  args: {
    state: {
      ...offline,
      status: "recovering",
      message: "Restoring the workspace data for this artifact.",
      can_retry: false,
      recovery: {
        id: "33333333-3333-3333-3333-333333333333",
        type: "materialization",
        state: "running",
        progress: {
          percent: 64,
          rows_loaded: 32_000,
          rows_total: 50_000,
          unit: "rows",
          message: "Loading visits",
          source: "Visits",
          step: 2,
          total_steps: 3,
        },
        created_at: "2026-09-14T12:00:00Z",
      },
    },
  },
}

export const RecoveryFailed: Story = {
  args: {
    state: {
      status: "failed",
      recovery_action: "semantic_rebuild",
      physical_status: "active",
      semantic_status: "unavailable",
      message: "Scout could not restore this artifact's data.",
      detail: "The generated semantic schema could not be activated.",
      can_retry: true,
      recovery: {
        id: "44444444-4444-4444-4444-444444444444",
        type: "semantic_rebuild",
        state: "failed",
        progress: null,
        created_at: "2026-09-14T12:00:00Z",
      },
    },
  },
}
