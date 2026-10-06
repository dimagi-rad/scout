import { ApiError } from "@/api/client"

export function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError && error.message ? error.message : fallback
}
