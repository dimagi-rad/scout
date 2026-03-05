import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import App from "./App"

// Auto-close popup after OAuth redirect (popup_close=1 added by ConnectionsPage)
if (
  new URLSearchParams(window.location.search).get("popup_close") === "1" &&
  window.opener
) {
  window.close()
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
