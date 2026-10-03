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

/**
 * The data provider an OAuth app id belongs to ("commcare_eu" -> "commcare"),
 * matching the backend's canonical_provider: Connect is checked before CommCare.
 */
export function canonicalProvider(id: string): string {
  return ["commcare_connect", "commcare", "ocs"].find((key) => id.startsWith(key)) ?? id
}

/** Tinted surface per provider: CommCare blue, Connect green, OCS purple. */
export const PROVIDER_TINT: Record<string, string> = {
  commcare: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  commcare_connect: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  ocs: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400",
}
export const FALLBACK_TINT = "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400"
