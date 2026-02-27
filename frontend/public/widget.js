(function () {
  "use strict";

  var SCOUT_ORIGIN = (function () {
    var scripts = document.getElementsByTagName("script");
    for (var i = 0; i < scripts.length; i++) {
      var src = scripts[i].src || "";
      if (src.indexOf("widget.js") !== -1) {
        var url = new URL(src);
        return url.origin;
      }
    }
    return window.location.origin;
  })();

  var instances = {};
  var instanceId = 0;

  function ScoutWidgetInstance(opts) {
    this.id = ++instanceId;
    this.opts = opts;
    this.iframe = null;
    this.container = null;
    this.ready = false;
    this._boundMessageHandler = this._onMessage.bind(this);
    this._init();
  }

  ScoutWidgetInstance.prototype._init = function () {
    // Resolve container
    if (typeof this.opts.container === "string") {
      this.container = document.querySelector(this.opts.container);
    } else {
      this.container = this.opts.container;
    }
    if (!this.container) {
      console.error("[ScoutWidget] Container not found:", this.opts.container);
      return;
    }

    // Show loading state
    this.container.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;' +
      'height:100%;width:100%;font-family:system-ui,sans-serif;color:#666;">' +
      '<div style="text-align:center;">' +
      '<div style="width:24px;height:24px;border:3px solid #e5e7eb;' +
      'border-top-color:#6366f1;border-radius:50%;animation:scout-spin 0.8s linear infinite;' +
      'margin:0 auto 8px;"></div>Loading Scout...</div></div>';

    // Add spinner animation
    if (!document.getElementById("scout-widget-styles")) {
      var style = document.createElement("style");
      style.id = "scout-widget-styles";
      style.textContent =
        "@keyframes scout-spin{to{transform:rotate(360deg)}}";
      document.head.appendChild(style);
    }

    // Build iframe URL
    var params = [];
    if (this.opts.mode) params.push("mode=" + encodeURIComponent(this.opts.mode));
    if (this.opts.tenant) params.push("tenant=" + encodeURIComponent(this.opts.tenant));
    if (this.opts.theme) params.push("theme=" + encodeURIComponent(this.opts.theme));
    var src = SCOUT_ORIGIN + "/embed/" + (params.length ? "?" + params.join("&") : "");

    // Create iframe
    this.iframe = document.createElement("iframe");
    this.iframe.src = src;
    this.iframe.style.cssText =
      "width:100%;height:100%;border:none;display:block;";
    this.iframe.setAttribute("allow", "clipboard-write");
    this.iframe.setAttribute("title", "Scout");

    // Listen for messages
    window.addEventListener("message", this._boundMessageHandler);

    // Replace loading state with iframe
    this.iframe.onload = function () {
      // iframe loaded, but we wait for scout:ready postMessage
    };

    this.iframe.onerror = function () {
      this.container.innerHTML =
        '<div style="display:flex;align-items:center;justify-content:center;' +
        'height:100%;font-family:system-ui,sans-serif;color:#ef4444;">' +
        "Failed to load Scout</div>";
    }.bind(this);

    this.container.innerHTML = "";
    this.container.appendChild(this.iframe);

    instances[this.id] = this;
  };

  ScoutWidgetInstance.prototype._onMessage = function (event) {
    if (event.origin !== SCOUT_ORIGIN) return;
    var data = event.data;
    if (!data || typeof data.type !== "string" || !data.type.startsWith("scout:")) return;

    if (data.type === "scout:ready") {
      this.ready = true;
      if (typeof this.opts.onReady === "function") this.opts.onReady();
    }

    if (typeof this.opts.onEvent === "function") {
      this.opts.onEvent(data);
    }
  };

  ScoutWidgetInstance.prototype._postMessage = function (type, payload) {
    if (!this.iframe || !this.iframe.contentWindow) return;
    this.iframe.contentWindow.postMessage(
      { type: type, payload: payload },
      SCOUT_ORIGIN
    );
  };

  ScoutWidgetInstance.prototype.setTenant = function (tenantId) {
    this._postMessage("scout:set-tenant", { tenant: tenantId });
  };

  ScoutWidgetInstance.prototype.setMode = function (mode) {
    this._postMessage("scout:set-mode", { mode: mode });
  };

  ScoutWidgetInstance.prototype.destroy = function () {
    window.removeEventListener("message", this._boundMessageHandler);
    if (this.iframe && this.iframe.parentNode) {
      this.iframe.parentNode.removeChild(this.iframe);
    }
    delete instances[this.id];
  };

  // Public API
  var ScoutWidget = {
    init: function (opts) {
      return new ScoutWidgetInstance(opts || {});
    },
    destroy: function () {
      Object.keys(instances).forEach(function (id) {
        instances[id].destroy();
      });
    },
  };

  // Replay queued calls from async loading stub
  var queued = window.ScoutWidget && window.ScoutWidget._q;
  window.ScoutWidget = ScoutWidget;
  if (queued && queued.length) {
    queued.forEach(function (call) {
      var method = call[0];
      var args = call[1];
      if (typeof ScoutWidget[method] === "function") {
        ScoutWidget[method](args);
      }
    });
  }
})();
