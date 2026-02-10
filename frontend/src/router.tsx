import { createBrowserRouter, Navigate } from "react-router-dom"
import { AppLayout } from "@/components/AppLayout/AppLayout"
import { ChatPanel } from "@/components/ChatPanel/ChatPanel"
import { ProjectsPage } from "@/pages/ProjectsPage"

// Placeholder pages (to be created later)
const KnowledgePage = () => <div className="p-8">Knowledge Page (coming soon)</div>
const RecipesPage = () => <div className="p-8">Recipes Page (coming soon)</div>
const DataDictionaryPage = () => <div className="p-8">Data Dictionary Page (coming soon)</div>

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <ChatPanel /> },
      { path: "chat", element: <ChatPanel /> },
      { path: "projects", element: <ProjectsPage /> },
      { path: "projects/new", element: <ProjectsPage /> },
      { path: "projects/:id/edit", element: <ProjectsPage /> },
      { path: "knowledge", element: <KnowledgePage /> },
      { path: "knowledge/new", element: <KnowledgePage /> },
      { path: "knowledge/:id", element: <KnowledgePage /> },
      { path: "recipes", element: <RecipesPage /> },
      { path: "recipes/:id", element: <RecipesPage /> },
      { path: "data-dictionary", element: <DataDictionaryPage /> },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
])
