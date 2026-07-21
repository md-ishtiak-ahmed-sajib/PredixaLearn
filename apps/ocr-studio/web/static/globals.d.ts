/** Browser globals supplied by the local OCR studio. */
interface Error {
  status?: number;
}

interface Window {
  marked?: {
    parse(source: string, options?: Record<string, unknown>): string;
  };
  DOMPurify?: {
    sanitize(input: string, options?: Record<string, unknown>): string;
  };
  predixalearnHealth?: Record<string, any>;
}

interface Event {
  key?: string;
  dataTransfer?: DataTransfer | null;
  detail?: any;
  target: any;
}

interface EventTarget {
  closest?: (selector: string) => Element | null;
  value?: any;
  files?: FileList | null;
}

interface Element {
  disabled?: boolean;
  checked?: boolean;
  value?: any;
  max?: any;
  href?: string;
  title?: string;
  dataset: DOMStringMap;
  tabIndex: number;
  showModal?: () => void;
  close?: () => void;
  open?: boolean;
  click?: () => void;
}

declare module "*pdf.min.mjs" {
  export const GlobalWorkerOptions: { workerSrc: string };
  export function getDocument(source: unknown): { promise: Promise<any> };
}
