import { fmtAge } from "../fmt.js";

export function renderNews(panel) {
  const body = document.querySelector("#panel-news .panel-body");
  body.innerHTML = panel.items.map((n) =>
    `<div class="news-item">
       <a href="${n.url}" target="_blank" rel="noopener">${n.headline}</a>
       <div class="news-meta">${n.feed} · ${fmtAge(n.published_at)}</div>
     </div>`
  ).join("");
}
