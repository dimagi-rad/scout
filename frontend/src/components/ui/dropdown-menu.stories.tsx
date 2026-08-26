import {
  Database,
  Download,
  MoreHorizontal,
  Pencil,
  Settings,
  Trash2,
} from "lucide-react"
import type { Meta, StoryObj } from "@storybook/react-vite"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

const meta = {
  title: "Design Primitives/Dropdown Menu",
  component: DropdownMenu,
  tags: ["autodocs"],
} satisfies Meta<typeof DropdownMenu>

export default meta
type Story = StoryObj<typeof meta>

function MenuContent() {
  return (
    <DropdownMenuContent align="end" className="w-56">
      <DropdownMenuLabel>Workspace</DropdownMenuLabel>
      <DropdownMenuGroup>
        <DropdownMenuItem>
          <Pencil />
          Rename
          <DropdownMenuShortcut>R</DropdownMenuShortcut>
        </DropdownMenuItem>
        <DropdownMenuItem>
          <Download />
          Export schema
        </DropdownMenuItem>
        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            <Database />
            Data source
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent className="w-44">
            <DropdownMenuItem>CommCare</DropdownMenuItem>
            <DropdownMenuItem>CommCare Connect</DropdownMenuItem>
            <DropdownMenuItem>Open Chat Studio</DropdownMenuItem>
          </DropdownMenuSubContent>
        </DropdownMenuSub>
      </DropdownMenuGroup>
      <DropdownMenuSeparator />
      <DropdownMenuCheckboxItem checked>Show only with data</DropdownMenuCheckboxItem>
      <DropdownMenuRadioGroup value="recent">
        <DropdownMenuRadioItem value="recent">Recent first</DropdownMenuRadioItem>
        <DropdownMenuRadioItem value="alpha">Alphabetical</DropdownMenuRadioItem>
      </DropdownMenuRadioGroup>
      <DropdownMenuSeparator />
      <DropdownMenuItem>
        <Settings />
        Settings
      </DropdownMenuItem>
      <DropdownMenuItem variant="destructive">
        <Trash2 />
        Delete
      </DropdownMenuItem>
    </DropdownMenuContent>
  )
}

export const Triggered: Story = {
  render: () => (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="icon" aria-label="Open menu">
          <MoreHorizontal />
        </Button>
      </DropdownMenuTrigger>
      <MenuContent />
    </DropdownMenu>
  ),
}

export const Open: Story = {
  render: () => (
    <DropdownMenu open>
      <DropdownMenuTrigger asChild>
        <Button variant="outline">
          Actions
          <MoreHorizontal />
        </Button>
      </DropdownMenuTrigger>
      <MenuContent />
    </DropdownMenu>
  ),
}
