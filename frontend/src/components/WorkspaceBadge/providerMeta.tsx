// frontend/src/components/WorkspaceBadge/providerMeta.tsx
import { Database, Smartphone, Briefcase, MessageSquare } from "lucide-react"
import type { ComponentType, SVGProps } from "react"

type IconComponent = ComponentType<SVGProps<SVGSVGElement>>

export interface ProviderMeta {
  label: string
  Icon: IconComponent
}

const META: Record<string, ProviderMeta> = {
  commcare: { label: "CommCare", Icon: Smartphone },
  commcare_connect: { label: "CommCare Connect", Icon: Briefcase },
  ocs: { label: "Open Chat Studio", Icon: MessageSquare },
}

const FALLBACK: ProviderMeta = { label: "Workspace", Icon: Database }

export function getProviderMeta(provider: string | undefined): ProviderMeta {
  if (!provider) return FALLBACK
  return META[provider] ?? FALLBACK
}
