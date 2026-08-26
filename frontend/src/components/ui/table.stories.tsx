import type { Meta, StoryObj } from "@storybook/react-vite"

import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"

const meta = {
  title: "Design Primitives/Table",
  component: Table,
  tags: ["autodocs"],
} satisfies Meta<typeof Table>

export default meta
type Story = StoryObj<typeof meta>

export const DatasetPreview: Story = {
  render: () => (
    <div className="w-[680px]">
      <Table>
        <TableCaption>Materialized tables available to Scout.</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>Table</TableHead>
            <TableHead>Source</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Rows</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell className="font-mono">cases</TableCell>
            <TableCell>CommCare</TableCell>
            <TableCell>
              <Badge variant="secondary">Ready</Badge>
            </TableCell>
            <TableCell className="text-right">12,482</TableCell>
          </TableRow>
          <TableRow>
            <TableCell className="font-mono">forms</TableCell>
            <TableCell>CommCare</TableCell>
            <TableCell>
              <Badge variant="outline">Syncing</Badge>
            </TableCell>
            <TableCell className="text-right">87,031</TableCell>
          </TableRow>
          <TableRow>
            <TableCell className="font-mono">workers</TableCell>
            <TableCell>Connect</TableCell>
            <TableCell>
              <Badge variant="secondary">Ready</Badge>
            </TableCell>
            <TableCell className="text-right">4,218</TableCell>
          </TableRow>
        </TableBody>
        <TableFooter>
          <TableRow>
            <TableCell colSpan={3}>Total rows</TableCell>
            <TableCell className="text-right">103,731</TableCell>
          </TableRow>
        </TableFooter>
      </Table>
    </div>
  ),
}
