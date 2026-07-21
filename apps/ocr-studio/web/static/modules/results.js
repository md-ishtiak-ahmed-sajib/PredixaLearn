/** @ts-check */

/**
 * Select the user-facing output in a stable order shared by Home and History.
 * @param {any} value
 * @param {string} [workflow]
 * @returns {{text: string, format: "markdown" | "text" | "json"}}
 */
export function primaryOutput(value, workflow = "") {
  if (workflow === "text" || workflow === "text_recognition") {
    if (typeof value?.full_text === "string" && value.full_text.trim()) {
      return { text: value.full_text, format: "text" };
    }
  }
  if (workflow === "table" || workflow === "table_extraction") {
    return { text: JSON.stringify(value, null, 2), format: "json" };
  }
  if (typeof value?.markdown === "string" && value.markdown.trim()) {
    return { text: value.markdown, format: "markdown" };
  }
  if (typeof value?.full_text === "string" && value.full_text.trim()) {
    return { text: value.full_text, format: "text" };
  }
  return { text: JSON.stringify(value, null, 2), format: "json" };
}

/**
 * Render Markdown locally, stripping active content and remote resources.
 * @param {string} markdown
 * @returns {DocumentFragment | null}
 */
export function safeMarkdown(markdown) {
  if (!window.marked || !window.DOMPurify) return null;
  const parsed = window.marked.parse(markdown, { gfm: true, breaks: true });
  const clean = window.DOMPurify.sanitize(parsed, {
    FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form", "img", "svg"],
    FORBID_ATTR: ["style", "src", "srcset", "onerror", "onclick"],
  });
  const template = document.createElement("template");
  template.innerHTML = clean;
  template.content.querySelectorAll("a").forEach((link) => {
    const href = link.getAttribute("href") || "";
    if (!(href.startsWith("#") || href.startsWith("/api/v1/history/"))) {
      link.replaceWith(document.createTextNode(link.textContent || href));
    }
  });
  return template.content;
}
