import type { Meta, StoryObj } from "@storybook/react-vite"

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

const meta = {
  title: "Design Primitives/Tabs",
  component: Tabs,
  tags: ["autodocs"],
} satisfies Meta<typeof Tabs>

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => (
    <Tabs defaultValue="overview" className="w-[560px]">
      <TabsList>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="schema">Schema</TabsTrigger>
        <TabsTrigger value="members">Members</TabsTrigger>
      </TabsList>
      <TabsContent value="overview">
        <div className="rounded-md border p-4 text-sm text-muted-foreground">
          Workspace-level metrics, current sync state, and recent activity.
        </div>
      </TabsContent>
      <TabsContent value="schema">
        <div className="rounded-md border p-4 text-sm text-muted-foreground">
          Tables, columns, relationships, and available query surfaces.
        </div>
      </TabsContent>
      <TabsContent value="members">
        <div className="rounded-md border p-4 text-sm text-muted-foreground">
          People with access to this workspace and their assigned roles.
        </div>
      </TabsContent>
    </Tabs>
  ),
}
