import { NavLink, useLocation } from "react-router-dom"
import { cn } from "@/lib/utils"
import type { LucideIcon } from "lucide-react"

interface NavItemProps {
  to: string
  icon: LucideIcon
  label: string
  onNavigate?: () => void
  /**
   * Optional predicate to force the active state for paths that don't match
   * `to` directly (e.g. the Chat item is active on `/workspaces/:id/chat`).
   */
  isActivePath?: (pathname: string) => boolean
}

export function NavItem({ to, icon: Icon, label, onNavigate, isActivePath }: NavItemProps) {
  const location = useLocation()
  const forceActive = isActivePath?.(location.pathname) ?? false
  return (
    <NavLink
      to={to}
      title={label}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          "scout-sidebar-nav-link flex items-center gap-3 rounded-md px-2 py-2 text-sm font-medium transition-colors lg:px-3",
          isActive || forceActive
            ? "bg-accent text-accent-foreground"
            : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
        )
      }
    >
      <Icon className="h-4 w-4 shrink-0" />
      <span className="scout-sidebar-label min-w-0 truncate">{label}</span>
    </NavLink>
  )
}
