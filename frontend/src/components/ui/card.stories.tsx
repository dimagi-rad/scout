import type { Meta, StoryObj } from "@storybook/react-vite"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"

const meta = {
  title: "Design Primitives/Card",
  component: Card,
  tags: ["autodocs"],
} satisfies Meta<typeof Card>

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => (
    <Card className="w-[360px]">
      <CardHeader>
        <CardTitle>Workspace health</CardTitle>
        <CardDescription>Current state of the connected data source.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center justify-between rounded-md border p-3">
          <span className="text-sm text-muted-foreground">Schema status</span>
          <Badge variant="secondary">Available</Badge>
        </div>
        <Button className="w-full">Open workspace</Button>
      </CardContent>
    </Card>
  ),
}

export const Dense: Story = {
  render: () => (
    <Card className="w-[320px] shadow-sm">
      <CardHeader className="p-4">
        <CardTitle className="text-base">Recent sync</CardTitle>
        <CardDescription>12,482 rows loaded in 42 seconds.</CardDescription>
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0">
        <div className="h-1.5 overflow-hidden rounded-full bg-muted">
          <div className="h-full w-4/5 rounded-full bg-primary" />
        </div>
      </CardContent>
    </Card>
  ),
}
