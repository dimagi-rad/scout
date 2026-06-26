import type { Meta, StoryObj } from "@storybook/react-vite"

import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"

const meta = {
  title: "Design Primitives/Form Controls",
  tags: ["autodocs"],
  parameters: {
    layout: "centered",
  },
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

export const TextInputs: Story = {
  render: () => (
    <div className="grid w-[360px] gap-5">
      <div className="grid gap-2">
        <Label htmlFor="workspace-name">Workspace name</Label>
        <Input id="workspace-name" placeholder="Global operations" />
      </div>
      <div className="grid gap-2">
        <Label htmlFor="api-key">API key</Label>
        <Input id="api-key" type="password" value="sk-test-••••••••" readOnly />
      </div>
      <div className="grid gap-2">
        <Label htmlFor="disabled-field">Disabled field</Label>
        <Input id="disabled-field" placeholder="Unavailable" disabled />
      </div>
    </div>
  ),
}

export const TextareaStates: Story = {
  render: () => (
    <div className="grid w-[420px] gap-5">
      <div className="grid gap-2">
        <Label htmlFor="prompt">System prompt</Label>
        <Textarea
          id="prompt"
          rows={4}
          defaultValue="Answer with concise SQL-backed evidence and call out material caveats."
        />
      </div>
      <div className="grid gap-2">
        <Label htmlFor="disabled-notes">Notes</Label>
        <Textarea id="disabled-notes" rows={3} placeholder="Disabled" disabled />
      </div>
    </div>
  ),
}

export const SelectMenu: Story = {
  render: () => (
    <div className="grid w-[320px] gap-2">
      <Label>Workspace role</Label>
      <Select defaultValue="read-write">
        <SelectTrigger className="w-full">
          <SelectValue placeholder="Select role" />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectLabel>Roles</SelectLabel>
            <SelectItem value="manage">Manager</SelectItem>
            <SelectItem value="read-write">Read-write</SelectItem>
            <SelectItem value="read">Read only</SelectItem>
            <SelectSeparator />
            <SelectItem value="disabled" disabled>
              Disabled role
            </SelectItem>
          </SelectGroup>
        </SelectContent>
      </Select>
    </div>
  ),
}

export const SelectOpen: Story = {
  render: () => (
    <Select defaultValue="commcare" open>
      <SelectTrigger className="w-[240px]">
        <SelectValue placeholder="Select provider" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="commcare">CommCare</SelectItem>
        <SelectItem value="connect">CommCare Connect</SelectItem>
        <SelectItem value="ocs">Open Chat Studio</SelectItem>
      </SelectContent>
    </Select>
  ),
}
