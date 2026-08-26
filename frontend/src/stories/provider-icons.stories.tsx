import type { Meta, StoryObj } from "@storybook/react-vite"

import {
  CommCareConnectIcon,
  CommCareIcon,
  OpenChatStudioIcon,
} from "@/assets/providers/brandIcons"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"

const providers = [
  { value: "commcare", Icon: CommCareIcon },
  { value: "commcare_connect", Icon: CommCareConnectIcon },
  { value: "ocs", Icon: OpenChatStudioIcon },
  { value: "unknown", Icon: getProviderMeta("unknown").Icon },
]

const meta = {
  title: "App Primitives/Provider Icons",
  tags: ["autodocs"],
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

export const Icons: Story = {
  render: () => (
    <div className="grid w-[520px] gap-3">
      {providers.map(({ value, Icon }) => {
        const meta = getProviderMeta(value)
        return (
          <div key={value} className="flex items-center gap-3 rounded-lg border p-3">
            <Icon className="h-8 w-8 shrink-0" aria-hidden />
            <div>
              <div className="text-sm font-medium">{meta.label}</div>
              <div className="font-mono text-xs text-muted-foreground">{value}</div>
            </div>
          </div>
        )
      })}
    </div>
  ),
}
