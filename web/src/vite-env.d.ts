/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_TEMPORAL_UI?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
