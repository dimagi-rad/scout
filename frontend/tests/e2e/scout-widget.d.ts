interface ScoutWidgetInstance {
  id: number;
  ready: boolean;
  setTenant(tenantId: string): void;
  setMode(mode: string): void;
  destroy(): void;
}

interface ScoutWidgetOptions {
  container?: string | HTMLElement;
  mode?: string;
  tenant?: string;
  theme?: string;
  onReady?: () => void;
  onEvent?: (data: { type: string; payload?: unknown }) => void;
}

interface ScoutWidgetStatic {
  init(opts: ScoutWidgetOptions): ScoutWidgetInstance;
  destroy(): void;
  _q?: [string, unknown][];
}

interface Window {
  ScoutWidget: ScoutWidgetStatic;
}
