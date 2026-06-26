/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_TEMPORAL_UI?: string;
  readonly VITE_TEMPORAL_NAMESPACE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
