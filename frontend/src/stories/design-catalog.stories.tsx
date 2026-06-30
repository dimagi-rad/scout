import type { Meta, StoryObj } from "@storybook/react-vite"

const primitiveGroups = [
  {
    group: "UI primitives",
    items: [
      ["Accordion", "src/components/ui/accordion.tsx", "Disclosure"],
      ["AlertDialog", "src/components/ui/alert-dialog.tsx", "Modal"],
      ["Badge", "src/components/ui/badge.tsx", "Status"],
      ["Button", "src/components/ui/button.tsx", "Action"],
      ["Card", "src/components/ui/card.tsx", "Container"],
      ["Dialog", "src/components/ui/dialog.tsx", "Modal"],
      ["DropdownMenu", "src/components/ui/dropdown-menu.tsx", "Menu"],
      ["Input", "src/components/ui/input.tsx", "Form"],
      ["Label", "src/components/ui/label.tsx", "Form"],
      ["Popover", "src/components/ui/popover.tsx", "Overlay"],
      ["Select", "src/components/ui/select.tsx", "Form"],
      ["Skeleton", "src/components/ui/skeleton.tsx", "Loading"],
      ["Table", "src/components/ui/table.tsx", "Data display"],
      ["Tabs", "src/components/ui/tabs.tsx", "Navigation"],
      ["Textarea", "src/components/ui/textarea.tsx", "Form"],
    ],
  },
  {
    group: "App primitives",
    items: [
      ["Provider icons", "src/assets/providers/brandIcons.tsx", "Brand"],
      ["RoleBadge", "src/components/RoleBadge/RoleBadge.tsx", "Status"],
      ["SearchFilterBar", "src/components/SearchFilterBar/SearchFilterBar.tsx", "Filter"],
      ["NavItem", "src/components/Sidebar/NavItem.tsx", "Navigation"],
      ["WorkspaceSwitcher", "src/components/WorkspaceSwitcher/WorkspaceSwitcher.tsx", "Navigation"],
      ["ChatComposer", "src/components/ChatPanel/ChatComposer.tsx", "Composer"],
      ["ChatMessage", "src/components/ChatMessage/ChatMessage.tsx", "Message"],
      ["ChatTextPart", "src/components/ChatMessage/ChatMessage.tsx", "Message"],
      ["ChatReasoningPart", "src/components/ChatMessage/ChatMessage.tsx", "Reasoning"],
      ["ChatToolCallPart", "src/components/ChatMessage/ChatMessage.tsx", "Tool call"],
      ["ChatArtifactButton", "src/components/ChatMessage/ChatMessage.tsx", "Artifact"],
      ["ChatThinkingIndicator", "src/components/ChatPanel/ChatStatus.tsx", "Status"],
      ["ChatErrorNotice", "src/components/ChatPanel/ChatStatus.tsx", "Status"],
      ["ChatOverloadNotice", "src/components/ChatPanel/ChatStatus.tsx", "Status"],
      ["SlashCommandMenu", "src/components/ChatPanel/SlashCommandMenu.tsx", "Command"],
      ["SqlHighlighter", "src/components/ChatMessage/SqlHighlighter.tsx", "Code"],
      ["Tool outputs", "src/components/ChatMessage/ToolOutput.tsx", "Data display"],
      ["MaterializationProgressBanner", "src/components/MaterializationStatus/MaterializationProgressBanner.tsx", "Status"],
      ["MaterializationFailure", "src/components/MaterializationStatus/MaterializationFailure.tsx", "Status"],
      ["ChatEmptyState", "src/components/ChatEmptyState/ChatEmptyState.tsx", "Composer"],
    ],
  },
]

const meta = {
  title: "Design Primitives/Catalog",
  tags: ["autodocs"],
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

export const Inventory: Story = {
  render: () => (
    <main className="mx-auto max-w-5xl px-8 py-10">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold">Design primitive inventory</h1>
        <p className="max-w-3xl text-sm text-muted-foreground">
          Storybook catalog for the reusable design layer found in the frontend repository.
          Page containers and backend-driven flows are intentionally excluded from this
          primitive inventory.
        </p>
      </div>

      <div className="mt-8 grid gap-8">
        {primitiveGroups.map((section) => (
          <section key={section.group} className="space-y-3">
            <h2 className="text-lg font-semibold">{section.group}</h2>
            <div className="overflow-hidden rounded-lg border">
              <table className="w-full text-sm">
                <thead className="bg-muted/50 text-left text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 font-medium">Primitive</th>
                    <th className="px-4 py-2 font-medium">Type</th>
                    <th className="px-4 py-2 font-medium">Source</th>
                  </tr>
                </thead>
                <tbody>
                  {section.items.map(([name, path, type]) => (
                    <tr key={path} className="border-t">
                      <td className="px-4 py-2 font-medium">{name}</td>
                      <td className="px-4 py-2 text-muted-foreground">{type}</td>
                      <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                        {path}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ))}
      </div>
    </main>
  ),
}
