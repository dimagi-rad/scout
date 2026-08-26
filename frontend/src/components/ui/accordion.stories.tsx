import type { Meta, StoryObj } from "@storybook/react-vite"

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"

const meta = {
  title: "Design Primitives/Accordion",
  component: Accordion,
  tags: ["autodocs"],
  args: {
    type: "single",
  },
} satisfies Meta<typeof Accordion>

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => (
    <Accordion type="single" collapsible defaultValue="schema" className="w-[520px]">
      <AccordionItem value="schema">
        <AccordionTrigger>Schema availability</AccordionTrigger>
        <AccordionContent>
          Queryable schemas are created after the workspace has at least one successful data
          materialization.
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value="members">
        <AccordionTrigger>Workspace members</AccordionTrigger>
        <AccordionContent>
          Members can be managers, read-write collaborators, or read-only viewers.
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value="sync">
        <AccordionTrigger>Data freshness</AccordionTrigger>
        <AccordionContent>
          The sync timestamp reflects the most recent completed materialization job.
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  ),
}

export const MultipleOpen: Story = {
  render: () => (
    <Accordion type="multiple" defaultValue={["cases", "forms"]} className="w-[520px]">
      <AccordionItem value="cases">
        <AccordionTrigger>cases</AccordionTrigger>
        <AccordionContent>Case metadata, ownership, status, and indexed properties.</AccordionContent>
      </AccordionItem>
      <AccordionItem value="forms">
        <AccordionTrigger>forms</AccordionTrigger>
        <AccordionContent>Submission metadata, form XML fields, and received timestamps.</AccordionContent>
      </AccordionItem>
      <AccordionItem value="users">
        <AccordionTrigger>users</AccordionTrigger>
        <AccordionContent>Mobile worker identity and group membership.</AccordionContent>
      </AccordionItem>
    </Accordion>
  ),
}
