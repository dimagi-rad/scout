/**
 * Thin fetch wrapper that handles CSRF tokens and session cookies.
 */

import { BASE_PATH } from "@/config"

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

  const prefixedUrl = url.startsWith("/") ? `${BASE_PATH}${url}` : url
  const res = await fetch(prefixedUrl, {
    ...fetchOptions,
    headers,
    credentials: "include",
  })

  if (!res.ok) {
    throw await responseError(res)
  }

  if (res.status === 204) {
    return undefined as T
  }

  return res.json() as Promise<T>
}

export function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function messageText(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined
}

function messageLeaf(value: unknown): string | undefined {
  const record = asRecord(value)
  // Read only explicit message fields. In particular, a semantic error's
  // `detail`, validation `input`/`ctx`, and arbitrary metadata are not UI copy.
  return messageText(value) ?? messageText(record?.message) ?? messageText(record?.msg)
}

function errorMessage(value: unknown): string | undefined {
  if (!Array.isArray(value)) return messageLeaf(value)
  const messages = value.map(messageLeaf).filter((message) => message !== undefined)
  return messages.length ? [...new Set(messages)].join(" ") : undefined
}

async function responseError(res: Response): Promise<ApiError> {
  const body: unknown = await res.json().catch(() => undefined)
  const record = asRecord(body)
  const message = errorMessage(record?.detail)
    ?? errorMessage(record?.error)
    ?? errorMessage(body)
    ?? messageText(res.statusText)
    ?? `Request failed (HTTP ${res.status}).`
  return new ApiError(res.status, message, body)
}

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.body = body
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
    const prefixedUrl = url.startsWith("/") ? `${BASE_PATH}${url}` : url
    const res = await fetch(prefixedUrl, { credentials: "include" })
    if (!res.ok) {
      throw await responseError(res)
    }
    return res.blob()
  },
}
