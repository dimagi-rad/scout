import { CheckCircle2, Clock, Database, XCircle } from "lucide-react"
import type { Meta, StoryObj } from "@storybook/react-vite"

import { Badge } from "@/components/ui/badge"

const meta = {
  title: "Design Primitives/Badge",
  component: Badge,
  tags: ["autodocs"],
  argTypes: {
    variant: {
      control: "select",
      options: ["default", "secondary", "destructive", "outline", "ghost", "link"],
    },
  },
  args: {
    children: "Ready",
    variant: "default",
  },
} satisfies Meta<typeof Badge>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

export const Variants: Story = {
  render: () => (
    <div className="flex flex-wrap items-center gap-2">
      <Badge>Default</Badge>
      <Badge variant="secondary">Secondary</Badge>
      <Badge variant="outline">Outline</Badge>
      <Badge variant="ghost">Ghost</Badge>
      <Badge variant="destructive">Destructive</Badge>
      <Badge variant="link">Link</Badge>
    </div>
  ),
}

export const WithIcons: Story = {
  render: () => (
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant="secondary">
        <CheckCircle2 />
        Synced
      </Badge>
      <Badge variant="outline">
        <Database />
        analytics
      </Badge>
      <Badge variant="ghost">
        <Clock />
        245ms
      </Badge>
      <Badge variant="destructive">
        <XCircle />
        Failed
      </Badge>
    </div>
  ),
}
