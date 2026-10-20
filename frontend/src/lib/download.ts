export function downloadBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  try {
    a.click()
  } finally {
    document.body.removeChild(a)
    // Revoking synchronously can cancel the download in older Safari/Firefox.
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}
