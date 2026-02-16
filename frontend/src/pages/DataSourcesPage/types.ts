/**
 * TypeScript types for database connections API.
 */

export interface DatabaseConnection {
  id: string
  name: string
  description: string
  db_host: string
  db_port: number
  db_name: string
  is_active: boolean
  project_count: number
  created_at: string
  updated_at: string
}

export interface DatabaseConnectionFormData {
  name: string
  description: string
  db_host: string
  db_port: number
  db_name: string
  db_user?: string
  db_password?: string
  is_active: boolean
}

export interface ConnectionTestResult {
  success: boolean
  schemas?: string[]
  error?: string
}
