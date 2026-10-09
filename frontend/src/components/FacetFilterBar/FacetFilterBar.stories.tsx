import { useCallback, useEffect, useState, type ReactNode } from "react"
import { MemoryRouter } from "react-router-dom"
import type { Meta, StoryObj } from "@storybook/react-vite"

import type { UserTenant } from "@/api/auth"
import { setCachedUserTenants } from "@/api/userTenantsCache"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal/CreateWorkspaceModal"
import { TenantsTab } from "@/pages/WorkspaceDetailPage/TenantsTab"
import { useFacetedList } from "@/lib/filters/useFacetedList"
import {
  TENANT_FACETS,
  normalizeTenantSearch,
  tenantMatchesSearch,
} from "@/lib/filters/tenantFacets"
import { useAppStore } from "@/store/store"
import { FacetFilterBar } from "./FacetFilterBar"

const ORGS = [
  ["dimagi", "Dimagi"],
  ["acme-health", "Acme Health"],
  ["kenya-moh", "Kenya Ministry of Health"],
  ["ghana-chw", "Ghana CHW Network"],
  ["uganda-ngo", "Uganda Child Care"],
  ["malawi-nutrition", "Malawi Nutrition Trust"],
  ["nigeria-vax", "Nigeria Vaccination Alliance"],
  ["india-asha", "India ASHA Collective"],
  ["peru-salud", "Peru Salud"],
  ["haiti-care", "Haiti Care"],
  ["zambia-wash", "Zambia WASH"],
  ["mozambique-mch", "Mozambique MCH"],
  ["senegal-sante", "Sénégal Santé"],
  ["togo-pilot", "Togo Pilot"],
] as const
const PROGRAMS = ["Nutrition", "Immunization", "Maternal Health", "WASH", "Early Learning"]

function connectTenant(n: number): UserTenant {
  const [org, orgName] = ORGS[n % ORGS.length]
  const program = PROGRAMS[n % PROGRAMS.length]
  return {
    id: `m-connect-${n}`,
    provider: "commcare_connect",
    tenant_id: String(800 + n),
    tenant_uuid: `connect-${n}`,
    tenant_name: `${program} ${orgName.split(" ")[0]} ${n}`,
    last_selected_at: null,
    attributes:
      n % 11 === 0
        ? {}
        : {
            is_active: n % 3 !== 0,
            ...(n % 7 === 0 ? {} : { is_test: n % 5 === 0 }),
            end_date: n % 4 === 0 ? "2025-12-31" : null,
            organization: org,
            organization_name: orgName,
            program: `prog-${n % PROGRAMS.length}`,
            program_name: program,
            visit_count: n * 13,
          },
  }
}

const DEMO_TENANTS: UserTenant[] = [
  ...Array.from({ length: 6 }, (_, n) => ({
    id: `m-cc-${n}`,
    provider: "commcare",
    tenant_id: `domain-${n}`,
    tenant_uuid: `cc-${n}`,
    tenant_name: `CommCare Project ${n}`,
    last_selected_at: null,
    attributes: {},
  })),
  ...Array.from({ length: 3 }, (_, n) => ({
    id: `m-ocs-${n}`,
    provider: "ocs",
    tenant_id: `bot-${n}`,
    tenant_uuid: `ocs-${n}`,
    tenant_name: `Chatbot ${n}`,
    last_selected_at: null,
  })),
  ...Array.from({ length: 90 }, (_, n) => connectTenant(n + 1)),
]

const STORY_USER = {
  id: "storybook-user",
  email: "demo@example.org",
  name: "Demo",
  is_staff: false,
  onboarding_complete: true,
}

// Seeds after mount and restores on unmount; the pickers load once the user id appears.
function SeededStore({ children }: { children: ReactNode }) {
  useEffect(() => {
    const previous = useAppStore.getState()
    setCachedUserTenants(STORY_USER.id, DEMO_TENANTS)
    useAppStore.setState({ user: STORY_USER, authStatus: "authenticated" })
    useAppStore.setState({ domains: [], domainsStatus: "loaded" })
    return () => useAppStore.setState(previous)
  }, [])
  return children
}

const meta = {
  title: "App Primitives/FacetFilterBar",
  tags: ["autodocs"],
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

function StandaloneDemo() {
  const [search, setSearch] = useState("")
  const normalized = normalizeTenantSearch(search)
  const predicate = useCallback(
    (t: UserTenant) => tenantMatchesSearch(t, normalized),
    [normalized],
  )
  const list = useFacetedList({
    items: DEMO_TENANTS,
    facets: TENANT_FACETS,
    storageKey: null,
    predicate,
  })
  return (
    <div className="w-[30rem] space-y-3">
      <FacetFilterBar
        testIdPrefix="story-filter"
        search={search}
        onSearchChange={setSearch}
        searchPlaceholder="Search by name or opportunity ID…"
        facets={list.facets}
        options={list.options}
        selection={list.selection}
        onFacetChange={list.setFacet}
        onClear={() => {
          setSearch("")
          list.clearFacets()
        }}
        shownCount={list.filtered.length}
        totalCount={DEMO_TENANTS.length}
      />
      <ul className="max-h-64 overflow-y-auto rounded-md border p-1 text-sm">
        {list.filtered.map((t) => (
          <li key={t.tenant_uuid} className="px-2 py-1">
            {t.tenant_name}
          </li>
        ))}
      </ul>
    </div>
  )
}

export const Standalone: Story = {
  render: () => <StandaloneDemo />,
}

export const InCreateWorkspaceModal: Story = {
  render: () => (
    <SeededStore>
      <MemoryRouter>
        <CreateWorkspaceModal onClose={() => {}} />
      </MemoryRouter>
    </SeededStore>
  ),
}

export const InAddSourcePanel: Story = {
  parameters: { layout: "padded" },
  render: () => (
    <SeededStore>
      <div className="max-w-3xl">
        <TenantsTab workspaceId="storybook-workspace" isManager onWorkspaceDeleted={() => {}} />
      </div>
    </SeededStore>
  ),
}
