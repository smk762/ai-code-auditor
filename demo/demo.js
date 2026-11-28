// The run page's outcome filter. Without JS every pack is listed and the buttons stay hidden.
(() => {
  const bar = document.querySelector(".filters");
  if (!bar) return;
  const groups = document.querySelectorAll(".group[data-outcome]");
  bar.hidden = false;
  bar.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-filter]");
    if (!btn) return;
    const want = btn.dataset.filter;
    for (const b of bar.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b === btn));
    for (const g of groups) g.hidden = Boolean(want) && g.dataset.outcome !== want;
  });
})();
