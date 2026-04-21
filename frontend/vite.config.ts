import { defineConfig, loadEnv } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { sentryVitePlugin } from "@sentry/vite-plugin"
import path from "path"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, ".."), "")

  const sentryAuthToken = process.env.SENTRY_AUTH_TOKEN
  const sentryOrg = process.env.SENTRY_ORG
  const sentryProject = process.env.SENTRY_PROJECT
  const sentryUploadEnabled = Boolean(sentryAuthToken && sentryOrg && sentryProject)

  return {
    base: env.VITE_BASE_PATH || "/",
    build: {
      // 'hidden' emits source maps but strips the sourceMappingURL comment
      // so browsers don't fetch them. The Sentry plugin uploads then deletes
      // them, 'hidden' is defense-in-depth in case upload+delete is skipped.
      sourcemap: sentryUploadEnabled ? "hidden" : false,
    },
    plugins: [
      react(),
      tailwindcss(),
      ...(sentryUploadEnabled
        ? [
            sentryVitePlugin({
              authToken: sentryAuthToken,
              org: sentryOrg,
              project: sentryProject,
              release: { name: env.VITE_SENTRY_RELEASE || undefined },
              sourcemaps: { filesToDeleteAfterUpload: ["./dist/**/*.map"] },
            }),
          ]
        : []),
    ],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    server: {
      allowedHosts: ['.ngrok-free.app', '.ts.net'],
      watch: {
        usePolling: !!process.env.WSL_DISTRO_NAME,
      },
      proxy: {
        "/api": {
          target: `http://localhost:${env.API_PORT || 8000}`,
        },
        "/accounts": {
          target: `http://localhost:${env.API_PORT || 8000}`,
        },
        "/health": {
          target: `http://localhost:${env.API_PORT || 8000}`,
        },
      },
    }
  }
})
