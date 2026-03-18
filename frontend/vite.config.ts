import { defineConfig, loadEnv } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import path from "path"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, ".."), "")

  return {
    base: env.VITE_BASE_PATH || "/",
    plugins: [react(), tailwindcss()],
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
