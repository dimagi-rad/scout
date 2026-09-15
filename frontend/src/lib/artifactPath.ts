import { matchPath } from "react-router-dom"

import { workspacePath } from "./workspacePath"

export function artifactPath(workspace: Parameters<typeof workspacePath>[0], artifactId: string): string {
  return `${workspacePath(workspace)}/artifacts/${artifactId}`
}

export function isWorkspaceArtifactPath(pathname: string): boolean {
  return [
    "/workspaces/:workspaceId/artifacts/:artifactId",
    "/workspaces/:slug/:workspaceId/artifacts/:artifactId",
  ].some((pattern) => matchPath(pattern, pathname) !== null)
}
