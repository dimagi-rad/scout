import { createBrowserRouter, Navigate } from "react-router-dom"
import { AppLayout } from "@/components/AppLayout/AppLayout"
import { ChatPanel } from "@/components/ChatPanel/ChatPanel"
import { ProjectsPage, ProjectForm } from "@/pages/ProjectsPage"
import { DataDictionaryPage } from "@/pages/DataDictionaryPage"
import { KnowledgePage } from "@/pages/KnowledgePage"
import { RecipesPage } from "@/pages/RecipesPage"

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <ChatPanel /> },
      { path: "chat", element: <ChatPanel /> },
      { path: "projects", element: <ProjectsPage /> },
      { path: "projects/new", element: <ProjectForm /> },
      { path: "projects/:id/edit", element: <ProjectForm /> },
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
