/**
 * Thin fetch wrapper that handles CSRF tokens and session cookies.
 */

export function getCsrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken_scout=([^;]+)/)
  return match ? match[1] : ""
}

async function request<T>(
  url: string,
  options: RequestInit & { rawBody?: boolean } = {},
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase()
  const { rawBody, ...fetchOptions } = options

  const headers: Record<string, string> = {
    // Skip Content-Type for FormData — the browser sets the multipart boundary
    ...(rawBody ? {} : { "Content-Type": "application/json" }),
    ...(fetchOptions.headers as Record<string, string> | undefined),
  }

  // Attach CSRF token for mutations
  if (method !== "GET" && method !== "HEAD") {
    headers["X-CSRFToken"] = getCsrfToken()
  }

  const res = await fetch(url, {
    ...fetchOptions,
    headers,
    credentials: "include",
  })

  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: res.statusText }))
    throw new ApiError(res.status, body.detail ?? body.error ?? res.statusText)
  }

  // Handle 204 No Content (common for DELETE responses)
  if (res.status === 204) {
    return undefined as T
  }

  return res.json() as Promise<T>
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

export const api = {
  get: <T>(url: string) => request<T>(url),
  post: <T>(url: string, body?: unknown) =>
    request<T>(url, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  put: <T>(url: string, body?: unknown) =>
    request<T>(url, { method: "PUT", body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(url: string, body?: unknown) =>
    request<T>(url, { method: "PATCH", body: body ? JSON.stringify(body) : undefined }),
  delete: <T>(url: string) => request<T>(url, { method: "DELETE" }),
  upload: <T>(url: string, formData: FormData) =>
    request<T>(url, { method: "POST", body: formData, rawBody: true }),
  getBlob: async (url: string): Promise<Blob> => {
    const res = await fetch(url, { credentials: "include" })
    if (!res.ok) {
      throw new ApiError(res.status, res.statusText)
    }
    return res.blob()
  },
}
