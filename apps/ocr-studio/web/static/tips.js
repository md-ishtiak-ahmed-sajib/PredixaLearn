"use strict";

/** @type {any} */
const search = document.querySelector("#tips-search");
/** @type {any[]} */
const sections = Array.from(document.querySelectorAll(".help-section"));
const count = document.querySelector("#tips-count");
const empty = document.querySelector("#tips-empty");

function filterTips() {
  const query = search.value.trim().toLowerCase();
  let visible = 0;
  sections.forEach((section) => {
    const haystack = `${section.dataset.search || ""} ${section.textContent}`.toLowerCase();
    const match = !query || haystack.includes(query);
    section.classList.toggle("hidden", !match);
    if (match) visible += 1;
  });
  count.textContent = query ? `${visible} matching ${visible === 1 ? "topic" : "topics"}` : "";
  empty.classList.toggle("hidden", visible !== 0);
}

search.addEventListener("input", filterTips);
