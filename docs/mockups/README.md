# havn UX mockups

Static HTML concepts for a reworked web UI. Open any file in a browser. Each one has a
**Design notes** button (bottom right) that shows numbered pins and the reasoning behind them.
They use the real `havn-dark` / `havn-light` tokens from `frontend/src/themes.js` and follow the
OS light/dark setting.

| Mockup | Problem it addresses |
| --- | --- |
| [01 · Harbour Home](01-harbour-home.html) | 5 sections × 17 sub-tabs is a lot to navigate, and Overview shows state without saying what to do next. Proposes a 5-destination rail, an object-search omnibox, a loud env pill, and a ranked "Needs attention" queue. |
| [02 · Model Workbench](02-model-workbench.html) | Fixing one model means hopping between Editor, Query, Quality, DAG and Runs. Proposes one screen per model: a lineage strip, inline assertion failures, a Preview/Checks/Columns/Runs inspector, and a blast-radius action bar. |
| [03 · Change Review](03-change-review.html) | Diff, Git, Quality, Masking and environments are separate tabs with no "ship it" flow. Proposes a dev → prod promotion page: impact subgraph, data and schema diff, metric delta, and an explicit gate with a promote plan. |

The three share one shell (rail + omnibox + env pill), so they can be read as one
direction, but each also stands on its own and could ship separately. Suggested order by
value per effort: 02 → 01 → 03.
