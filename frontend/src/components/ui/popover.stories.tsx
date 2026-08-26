import { CalendarDays } from "lucide-react"
import type { Meta, StoryObj } from "@storybook/react-vite"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"

const meta = {
  title: "Design Primitives/Popover",
  component: Popover,
  tags: ["autodocs"],
} satisfies Meta<typeof Popover>

export default meta
type Story = StoryObj<typeof meta>

export const Triggered: Story = {
  render: () => (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline">
          <CalendarDays />
          Schedule sync
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80">
        <div className="grid gap-4">
          <div className="space-y-1">
            <h4 className="text-sm font-medium leading-none">Sync cadence</h4>
            <p className="text-sm text-muted-foreground">
              Choose how often Scout refreshes this workspace.
            </p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="sync-time">Daily sync time</Label>
            <Input id="sync-time" type="time" defaultValue="08:00" />
          </div>
        </div>
      </PopoverContent>
    </Popover>
  ),
}

export const Open: Story = {
  render: () => (
    <Popover open>
      <PopoverTrigger asChild>
        <Button variant="outline">Open popover</Button>
      </PopoverTrigger>
      <PopoverContent className="w-72">
        <div className="space-y-2">
          <h4 className="text-sm font-medium">Materialization</h4>
          <p className="text-sm text-muted-foreground">
            42,000 rows loaded from CommCare in the last sync.
          </p>
        </div>
      </PopoverContent>
    </Popover>
  ),
}
