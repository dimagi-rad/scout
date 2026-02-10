import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { Plus } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { ProjectCard } from "./ProjectCard"

export function ProjectsPage() {
  const projects = useAppStore((s) => s.projects)
  const projectsStatus = useAppStore((s) => s.projectsStatus)
  const fetchProjects = useAppStore((s) => s.projectActions.fetchProjects)
  const deleteProject = useAppStore((s) => s.projectActions.deleteProject)

  const [deleteId, setDeleteId] = useState<string | null>(null)

  useEffect(() => {
    if (projectsStatus === "idle") {
      fetchProjects()
    }
  }, [fetchProjects, projectsStatus])

  const handleDelete = async () => {
    if (deleteId) {
      await deleteProject(deleteId)
      setDeleteId(null)
    }
  }

  return (
    <div className="container mx-auto py-8">
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Projects</h1>
          <p className="text-muted-foreground">
            Manage your data projects and team access
          </p>
        </div>
        <Button asChild>
          <Link to="/projects/new">
            <Plus className="mr-2 h-4 w-4" />
            New Project
          </Link>
        </Button>
      </div>

      {projectsStatus === "loading" && (
        <div className="text-muted-foreground">Loading projects...</div>
      )}

      {projectsStatus === "loaded" && projects.length === 0 && (
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-muted-foreground">No projects yet</p>
          <Button asChild className="mt-4">
            <Link to="/projects/new">Create your first project</Link>
          </Button>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {projects.map((project) => (
          <ProjectCard
            key={project.id}
            project={project}
            onDelete={setDeleteId}
          />
        ))}
      </div>

      <AlertDialog open={!!deleteId} onOpenChange={() => setDeleteId(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Project</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete this project? This action cannot
              be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleDelete}>Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
