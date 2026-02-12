interface ArtifactViewerProps {
  artifactId: string
}

export function ArtifactViewer({ artifactId }: ArtifactViewerProps) {
  return (
    <iframe
      src={`/api/artifacts/${artifactId}/sandbox/`}
      className="w-full h-96 rounded-lg border border-border"
      sandbox="allow-scripts"
      loading="lazy"
      title="Artifact"
    />
  )
}
