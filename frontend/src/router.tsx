import { createBrowserRouter, Navigate } from "react-router-dom"
import { BASE_PATH } from "@/config"
import { AppLayout } from "@/components/AppLayout/AppLayout"
import { ChatRoute } from "@/components/ChatPanel/ChatRoute"
import { ChatRedirect } from "@/components/ChatPanel/ChatRedirect"
import { ArtifactsPage } from "@/pages/ArtifactsPage"
import { CrossOppPage } from "@/pages/CrossOppPage"
import { DataDictionaryPage } from "@/pages/DataDictionaryPage"
import { KnowledgePage } from "@/pages/KnowledgePage"
import { RecipesPage } from "@/pages/RecipesPage"
import { ConnectionsPage } from "@/pages/ConnectionsPage"
import { WorkspacesPage } from "@/pages/WorkspacesPage"
import { WorkspaceDetailPage } from "@/pages/WorkspaceDetailPage"

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <ChatRedirect /> },
      { path: "chat", element: <ChatRedirect /> },
      { path: "workspaces/:workspaceId/chat", element: <ChatRoute /> },
      { path: "workspaces/:workspaceId/chat/:threadId", element: <ChatRoute /> },
      // Pretty chat URL: cosmetic slug + UUID. The uuid keeps the param name
      // `:workspaceId` so `useParams().workspaceId` consumers still get the bare
      // uuid; `:slug` is decorative and ignored for lookup. The literal "chat"
      // segment ranks these above the `:slug/:workspaceId` settings route, and
      // the bare routes above stay for back-compat with old `/workspaces/<uuid>`.
      { path: "workspaces/:slug/:workspaceId/chat", element: <ChatRoute /> },
      { path: "workspaces/:slug/:workspaceId/chat/:threadId", element: <ChatRoute /> },
      { path: "artifacts", element: <ArtifactsPage /> },
      { path: "knowledge", element: <KnowledgePage /> },
      { path: "knowledge/new", element: <KnowledgePage /> },
      { path: "knowledge/:id", element: <KnowledgePage /> },
      { path: "recipes", element: <RecipesPage /> },
      { path: "recipes/:id", element: <RecipesPage /> },
      { path: "recipes/:id/runs/:runId", element: <RecipesPage /> },
      { path: "data-dictionary", element: <DataDictionaryPage /> },
      { path: "crossopp", element: <CrossOppPage /> },
      { path: "settings/connections", element: <ConnectionsPage /> },
      { path: "workspaces", element: <WorkspacesPage /> },
      { path: "workspaces/:workspaceId", element: <WorkspaceDetailPage /> },
      // Pretty URL: cosmetic slug + UUID. Resolution is always by :workspaceId;
      // the :slug segment is ignored for lookup. Bare route above stays for
      // back-compat with old `/workspaces/<uuid>` links.
      { path: "workspaces/:slug/:workspaceId", element: <WorkspaceDetailPage /> },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
], { basename: BASE_PATH || undefined })
