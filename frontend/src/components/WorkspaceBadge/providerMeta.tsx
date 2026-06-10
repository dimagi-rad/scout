// frontend/src/components/WorkspaceBadge/providerMeta.tsx
import { Database } from "lucide-react"
import type { ComponentType, SVGProps } from "react"
import {
  CommCareIcon,
  CommCareConnectIcon,
  OpenChatStudioIcon,
} from "@/assets/providers/brandIcons"

type IconComponent = ComponentType<SVGProps<SVGSVGElement>>

export interface ProviderMeta {
  label: string
  Icon: IconComponent
}

const META: Record<string, ProviderMeta> = {
  commcare: { label: "CommCare", Icon: CommCareIcon },
  commcare_connect: { label: "CommCare Connect", Icon: CommCareConnectIcon },
  ocs: { label: "Open Chat Studio", Icon: OpenChatStudioIcon },
}

const FALLBACK: ProviderMeta = { label: "Workspace", Icon: Database }

export function getProviderMeta(provider: string | undefined): ProviderMeta {
  if (!provider) return FALLBACK
  return META[provider] ?? FALLBACK
}
