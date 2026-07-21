// @ts-check

/** @param {number} bytes */
export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

/**
 * Owns browser-only file inspection and object-URL lifecycle. Server-side
 * validation remains authoritative; this controller only provides fast local
 * feedback before a submission begins.
 *
 * @param {{ elements: Record<string, any>, state: Record<string, any>, updateRunButton: () => void }} context
 */
export function createUploadController({ elements, state, updateRunButton }) {
  let previewUrl = null;

  /** @param {string} [message] */
  function showUploadError(message = "") {
    elements.uploadError.textContent = message;
    elements.uploadError.classList.toggle("hidden", !message);
    elements.dropZone.classList.toggle("invalid", Boolean(message));
  }

  function revokePreview() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
    elements.filePreview.replaceChildren();
  }

  /** @param {File} file */
  async function renderPreview(file) {
    revokePreview();
    elements.fileMeta.textContent = "Inspecting document locally…";
    if (file.type.startsWith("image/")) {
      previewUrl = URL.createObjectURL(file);
      const image = document.createElement("img");
      image.alt = "";
      image.src = previewUrl;
      image.addEventListener("load", () => {
        elements.fileMeta.textContent = `${image.naturalWidth} × ${image.naturalHeight} pixels`;
      }, { once: true });
      elements.filePreview.append(image);
      return;
    }
    if (file.name.toLowerCase().endsWith(".pdf")) {
      try {
        const pdfjs = await import("/static/vendor/pdf.min.mjs");
        pdfjs.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.min.mjs";
        const documentTask = pdfjs.getDocument({ data: await file.arrayBuffer() });
        const pdf = await documentTask.promise;
        const page = await pdf.getPage(1);
        const baseViewport = page.getViewport({ scale: 1 });
        const viewport = page.getViewport({ scale: 130 / baseViewport.width });
        const canvas = document.createElement("canvas");
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
        elements.filePreview.append(canvas);
        elements.fileMeta.textContent = `${pdf.numPages} ${pdf.numPages === 1 ? "page" : "pages"}`;
        await pdf.destroy();
        return;
      } catch {
        // The server validates the PDF before it can enter the OCR queue.
      }
    }
    const marker = document.createElement("span");
    marker.textContent = file.name.split(".").pop()?.toUpperCase() || "DOC";
    elements.filePreview.append(marker);
    elements.fileMeta.textContent = "Ready for secure server validation";
  }

  /** @param {File | undefined} file */
  async function setFile(file) {
    if (!file) return;
    showUploadError();
    const extension = `.${file.name.split(".").pop()?.toLowerCase() || ""}`;
    if (state.acceptedExtensions.length && !state.acceptedExtensions.includes(extension)) {
      showUploadError("Choose a supported PDF or image file.");
      return;
    }
    if (file.size > state.maxUploadBytes) {
      showUploadError(`This file exceeds the ${formatBytes(state.maxUploadBytes)} upload limit.`);
      return;
    }
    state.file = file;
    elements.fileName.textContent = file.name;
    elements.fileSize.textContent = formatBytes(file.size);
    elements.fileCard.classList.remove("hidden");
    updateRunButton();
    await renderPreview(file);
  }

  function clearFile() {
    state.file = null;
    elements.fileInput.value = "";
    elements.fileCard.classList.add("hidden");
    showUploadError();
    revokePreview();
    updateRunButton();
  }

  return { clearFile, dispose: revokePreview, formatBytes, setFile, showUploadError };
}
