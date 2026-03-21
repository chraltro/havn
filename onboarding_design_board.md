

# havn Onboarding Design Board Meeting

## Full Discussion Transcript

---

### OPENING

**Freya:** Alright, let's get into this. We have 10 onboarding cards and the goal is to make each one pull its weight. Before we go card by card, I want to set the frame: our activation metric is "user runs their first pipeline and sees data flow through the DAG." Everything should push toward that. If a card doesn't move the user closer to understanding or doing something, it gets cut or merged.

**Maren:** Agreed on the goal, but I want to add a nuance. The onboarding should feel like the product is revealing itself to you — not like you're reading documentation. Each card should make the user feel something: curiosity, understanding, confidence. The current cards read a bit like feature announcements.

**Lars:** I'll be honest — when I try a new data tool, I want to know three things in under two minutes: what's the mental model, how do I define transforms, and how do I run them. If the onboarding can answer those three things and show me real results, I'm sold. Everything else I can discover later.

**Kenji:** From an implementation perspective, I want to flag up front: we need to think about the empty-project state. A fresh `havn init` project has sample files and some seed data, right? We should design assuming that scaffold exists. Also, the canvas DAG can't be manipulated from outside easily — we can trigger a "fit to view" and select a node programmatically, but we can't do rich hover animations on individual nodes from the onboarding system.

**Freya:** Good. Let's also acknowledge: the current scaffold from `havn init` creates sample ingest, bronze/silver/gold transforms, and some seed data. So there IS something to show. Let's use that.

---

### CARD 1: WELCOME

**Current:** "Your self-hosted data platform. Everything runs on your machine, in one file. No cloud, no vendor lock-in. Data in safe waters."
**Current navigation:** None

**Maren:** The Welcome card has the hardest job — it needs to set tone without being skippable. The current copy is decent but it reads like a tagline, not an invitation. I'd rewrite it to feel more personal. Something like: "Welcome to havn. Your data warehouse runs right here — one file, your machine, nothing leaves." Then a softer second line: "Let's take a quick look around. This will take about two minutes."

**Lars:** I like the "two minutes" framing. When I see onboarding I immediately think "how long is this going to take?" Telling me up front respects my time. But — "one file" is a strong hook for data engineers. We know what that means. Don't bury it.

**Freya:** I'd add the card count somewhere. "1 of 10" or the dot indicators. Users need to know the scope. Also, the current card says "Data in safe waters" — that's the brand tagline and I think it works as a closer, not an opener. Move it to the last card.

**Kenji:** For the background, we should show the Overview dashboard. It's the landing page anyway, so no navigation needed. But we could do something subtle — when this card appears, the dashboard stats could animate in with a staggered fade. Counters rolling up from zero. It gives life to the background without the card having to do anything.

**Maren:** Love that. The card itself should fade in with a slight upward drift — maybe 12px over 300ms with an ease-out. Not a bounce, not a slide from the side. Gentle arrival. And the harbour illustration on this card should be the most complete one — the full harbour with boats, buildings, the water. It sets the visual metaphor.

**Lars:** One thing — "self-hosted data platform" might not land for everyone. Some people might think it's just a BI tool or a database GUI. Can we be more specific? "Your SQL data warehouse" or "Your analytics warehouse"?

**Freya:** How about: "Welcome to havn — your local data warehouse." Then: "Everything runs on your machine. One DuckDB file holds your entire warehouse — schemas, transforms, history. No cloud required." That's specific enough for Lars, accessible enough for others.

**Maren:** I'd trim it. The card is small — we have maybe 3-4 lines before it feels dense. Let me propose:

> **Welcome to havn**
> Your entire data warehouse lives in one file on your machine. No cloud, no accounts, no data leaving your network.
> This tour takes about two minutes. Let's look around.

**Lars:** That's clean. I know what it is, I know the value prop, I know the time commitment.

**Kenji:** Should we have any glossary terms on this card?

**Maren:** No. Card one should be zero friction. No underlines, no hover targets. Just read and click Next.

**Freya:** Agreed. No glossary on Welcome. Let's move on.

---

### CARD 2: YOUR WAREHOUSE

**Current:** About DuckDB single file, schema layers. Navigate: Tables (auto-selects first table)
**Current copy mentions:** DuckDB, warehouse.duckdb, schema layers

**Lars:** This is where the mental model needs to land. The key insight for a data engineer is: "one DuckDB file, four schemas that represent your data maturity layers." If I get that, I get the whole product.

**Maren:** The current card navigates to Tables and auto-selects the first table. That's good — the user sees real data in the background. But I think we should be more intentional about WHICH table. Auto-select something in `gold` — it's the "finished product" layer. Show them the destination first, then later explain how data gets there.

**Kenji:** I can auto-select a specific table. The scaffold creates `gold.order_summary` or similar, right? We can target that specifically, with a fallback to the first table in any schema if gold is empty.

**Lars:** I want to see the schema sidebar in the Tables panel — the tree of landing/bronze/silver/gold with tables under each. That's the "aha" for the warehouse structure. Can we make sure the schema tree is expanded?

**Kenji:** Yes, we can expand all schema nodes programmatically when this card is active.

**Maren:** For the copy, the current version talks about DuckDB as a technology. Most users don't care about the engine name yet. They care about the concept. I'd rewrite:

> **Your Warehouse**
> All your data lives in a single file — organized into layers. Each layer refines the data further: {landing|where raw data arrives} → {bronze|cleaned and typed} → {silver|business logic applied} → {gold|ready to use}.

**Lars:** I'd push back slightly. Data engineers DO care that it's DuckDB — that's a credibility signal. It tells me this is built on something real, not a toy. Maybe mention it but don't lead with it.

**Freya:** Compromise: mention DuckDB but frame it as a feature, not the headline. Like: "Powered by {DuckDB|A fast embedded analytics database} under the hood."

**Maren:** Okay, revised:

> **Your Warehouse**
> All your data lives in one file, organized into layers that refine it step by step: {landing|Raw data, as-is from the source} → {bronze|Cleaned and typed} → {silver|Business logic applied} → {gold|Ready for dashboards and analysis}.
> Powered by {DuckDB|A fast, embedded analytics database. No server needed.} — fast, embedded, no server needed.

**Kenji:** That's two glossary terms — landing, DuckDB. I think that's the right density for this card. More than three glossary terms per card gets noisy with the dotted underlines.

**Freya:** The flow arrow notation (→) in the text — can we actually render that as a visual? Even a tiny horizontal flow diagram?

**Kenji:** Inside the card? That's tight on space with the illustration. But I could do a simple inline flow with colored dots or pills: `landing → bronze → silver → gold` where each word has the color of its schema. We already have schema colors in the DAG.

**Maren:** Yes! Colored pills inline. That's a micro-interaction that teaches the color coding they'll see everywhere else in the product. Brilliant.

**Lars:** One concern: on a fresh project, the Tables panel might not have much data. The scaffold seeds some rows, right?

**Kenji:** The scaffold creates sample tables with maybe 20-50 rows. Enough to show a real table grid. If for some reason there's nothing, we should show the table browser with the schema tree still visible, and maybe a subtle empty state: "Run your pipeline to populate these tables."

**Freya:** Good. Edge case handled. Let's lock this one.

---

### CARD 3: THE DATA FLOW

**Current:** landing→bronze→silver→gold layers. Navigate: Editor (highlights sidebar)

**Maren:** Wait — card 2 already covers the layer concept with the flow arrows. Card 3 is currently redundant. If we taught landing→bronze→silver→gold on card 2, what new information does card 3 add?

**Lars:** The distinction should be: card 2 is "here's what's IN the warehouse" (the schemas, the data), card 3 is "here's how data MOVES through it" (the pipeline concept: ingest scripts → SQL transforms → export scripts).

**Freya:** That's a good split. Card 2 is the destination (warehouse structure), card 3 is the journey (pipeline). But the current card 3 title "The Data Flow" is vague. And navigating to the Editor with a sidebar highlight doesn't really show a "flow."

**Kenji:** What if card 3 navigated to the DAG instead? The DAG literally shows the data flow as a graph. That's the most visual representation we have.

**Lars:** But we have a dedicated DAG card later (card 7). We'd be stepping on it.

**Maren:** I think we should merge this concept. Cut "The Data Flow" as a standalone card. Fold the pipeline concept into a renamed card that's about "how you build transforms" — which is card 4. The flow from ingest→transform→export is better explained when you're looking at actual SQL files.

**Freya:** I'm inclined to agree. Let me count what we'd have: Welcome, Warehouse, [cut], Writing Transforms... that jumps too fast. We need something between "here's the warehouse" and "here's how you write SQL."

**Lars:** What about a card specifically about the file structure? "Your project has folders: ingest/, transform/, export/. Files in these folders ARE your pipeline." That's the key mental model for anyone coming from dbt or Airflow — the project IS the code.

**Maren:** That's good. Navigate to the Editor, expand the file tree, and let them see the folder structure. Title: "Your Project" or "Project Structure."

**Kenji:** I can expand the file tree nodes programmatically and highlight the key folders. The sidebar file tree already has expand/collapse state I can control.

**Freya:** New card 3 proposal:

> **Your Project**
> Your pipeline is defined by files in your project folder. {Ingest scripts|Python scripts that pull data from sources into landing/} bring data in, {SQL transforms|.sql files that define how data moves between layers} refine it, and {export scripts|Python scripts that push gold data to external targets} send it out.

**Lars:** And in the background, the file tree is expanded showing `ingest/`, `transform/bronze/`, `transform/silver/`, `transform/gold/`, `export/`. The user literally sees the structure we're describing.

**Maren:** Can we highlight those folders with a subtle glow or color emphasis? Not the pulsing ring — something gentler. Maybe the folder icons get a brief color flash in the schema colors.

**Kenji:** I can add a CSS class to specific tree nodes temporarily. A gentle background color highlight that fades after a moment — like the folder row gets a soft blue background for 2 seconds and then fades. Technically straightforward.

**Freya:** Three glossary terms here. That's our max per card, right Kenji?

**Kenji:** I'd say three is fine. Four starts to make the text look like a Wikipedia article with all the dotted underlines.

**Maren:** Agreed. Three max per card.

---

### CARD 4: WRITING TRANSFORMS

**Current:** SQL files as models, auto-dependency detection. Navigate: Editor (auto-opens first bronze .sql file)

**Lars:** This is the most important card for data engineers. This is where I decide if the tool is worth my time. Show me a real SQL file, show me the config comments, show me that it's just SQL. No Jinja. No templating language I have to learn. Just SQL.

**Maren:** The current behavior — auto-opening a bronze SQL file — is good. But we should be deliberate about which file. Open a silver model that has a `-- depends_on:` line and a `-- config:` line. Those two comments ARE the product's interface for SQL models. If the user sees and understands those two lines, they get it.

**Kenji:** I can open a specific file. The scaffold creates something like `transform/silver/order_metrics.sql` that has both config and depends_on. I'll target that, with a fallback to the first .sql file in transform/.

**Lars:** The copy should explicitly call out that there's no templating. That's a differentiator from dbt. Something like: "No Jinja, no templating language. Just SQL with two comment lines."

**Freya:** But we shouldn't define ourselves by what we're NOT. Frame it positively: "Pure SQL. Two special comments tell havn what it needs to know."

**Maren:** Revised copy:

> **Writing Transforms**
> Every .sql file in transform/ becomes a model. Two comment lines are all havn needs — {-- config|Sets materialization (table or view) and target schema} to set options, and {-- depends_on|Declares which upstream models this one reads from} to declare dependencies. The rest is pure SQL.

**Lars:** That's strong. When I see this card with the actual SQL file open in Monaco behind it, showing those exact comment lines — that's the "aha moment." I immediately understand the authoring experience.

**Kenji:** Can we do something to draw the eye to those comment lines in the editor? Monaco supports decorations — I could add a subtle highlight (background color) on the first two lines of the opened file while this card is active.

**Maren:** Yes! A very soft highlight — like the same yellow you'd see in a diff for "changed lines," but more subtle. Applied to lines 1-2 where the config and depends_on comments live. Remove the highlight when the user moves to the next card.

**Freya:** I love the Monaco decoration idea. That's the kind of thing that makes this feel premium. The card POINTS to real code, and the code subtly glows in response.

**Lars:** One thing — should we mention auto-dependency extraction? The CLAUDE.md says if `-- depends_on:` is missing, havn uses sqlglot AST to find FROM/JOIN refs. That's a cool feature.

**Freya:** Too much detail for onboarding. That's a discovery moment for later. Keep this card focused on the explicit interface.

**Maren:** Agreed. Save it for tooltips or docs.

---

### CARD 5: RUNNING PIPELINES

**Current:** Run button, ingest→transform→export. Navigate: Overview (highlights Run button)

**Freya:** This is the ACTION card. Everything before was "here's what exists." This card says "here's how you make it go." I want this card to be the most energizing one.

**Maren:** Currently it highlights the Run button and goes to Overview. I think that's the wrong destination. The Run button is actually more prominent in the Develop section header or the Overview quick actions. Let me think...

**Kenji:** The Run button in Overview is in the quick actions area. We have a pulsing ring animation on it currently. That's correct — the pulse draws the eye.

**Lars:** What I want to know on this card: "Click Run, it executes ingest → transform → export in order, you see live output." That's it. Don't explain the internals. Just tell me the one action and what happens.

**Maren:** The card should navigate to Overview, highlight the Run button, but also — can we show the output panel? Even if it's empty, having it visible primes the user for what happens when they click Run.

**Kenji:** The output panel at the bottom of Overview shows pipeline output. I can ensure it's visible (expanded, not collapsed) when this card is active.

**Freya:** Copy proposal:

> **Running Pipelines**
> Hit the Run button to execute your entire pipeline — {ingest|Scripts that load raw data from sources}, then {transforms|SQL models that refine data layer by layer}, then {exports|Scripts that send finished data to external systems}. Output streams live so you can watch every step.

**Lars:** "Output streams live" — I like that. SSE streaming is a genuine feature. Seeing live output instead of waiting for a spinner is a real improvement over most tools.

**Maren:** For the micro-interaction: the pulsing ring on the Run button is fine, but can we make it pulse in the brand accent color (the harbour blue/teal) rather than a generic blue? It should feel intentional, not like a default Bootstrap glow.

**Kenji:** Absolutely. The pulse is a CSS animation — I can set the color to whatever we want.

**Freya:** Should this be a "try it yourself" moment? Should we actually invite them to click Run?

**Lars:** On this card? They're still in the tour. If they click Run mid-tour, does the tour persist while the pipeline runs in the background?

**Kenji:** The tour is a floating panel. If they click behind it, the UI responds normally. So yes, they could click Run and the pipeline would start while the tour is still showing. The output panel would start streaming behind the card.

**Maren:** That's actually beautiful. We don't need to explicitly say "try it now" — just make the Run button accessible. If they click it, great. The pipeline runs and they see real output. If they don't, they continue the tour and run it later.

**Freya:** But we should at least plant the seed. Add a subtle line: "Go ahead — try it now, or continue the tour." Give them permission.

**Lars:** I like that. It's not demanding, just inviting.

**Maren:** Final copy:

> **Running Pipelines**
> Hit Run to execute your full pipeline — {ingest|Scripts that pull raw data from sources}, then {transforms|SQL models that refine data layer by layer}, then {exports|Scripts that push finished data out}. Output streams live so you see every step as it happens.
> Go ahead — try it now, or keep exploring.

**Everyone:** Good.

---

### CARD 6: EXPLORING DATA

**Current:** SQL queries, table browsing, charts, Explain. Navigate: Query panel

**Lars:** This is the "playground" card. After learning what the warehouse is and how to build it, now you get to query it. The Query panel with Monaco and autocomplete is genuinely good — this should sell itself.

**Maren:** Navigate to the Query panel — agreed. But can we pre-populate the query editor with a sample query? Something that would return real results from the scaffold data?

**Kenji:** Yes. I can set the editor content programmatically. Something like `SELECT * FROM gold.order_summary LIMIT 10` — a query that works against the scaffold data.

**Lars:** Even better: pre-populate AND auto-run it. When the card appears, the query executes and results show in the results grid below. The user sees a working query with real results without lifting a finger.

**Kenji:** That's feasible. I trigger the query execution when the card activates. There might be a brief loading state, but the scaffold tables are small — it'll be under 100ms.

**Maren:** That's the premium feeling. The card appears, and behind it, a query runs and real data appears. The user sees havn actually working. No empty states, no placeholder text.

**Freya:** This is potentially the "aha moment" for some users. They see real query results from their warehouse without having done anything.

**Lars:** Especially if the pipeline already ran (from card 5's invitation). Then the data is fresh and real.

**Maren:** Copy:

> **Exploring Data**
> Write SQL, get results. The query editor has {autocomplete|Suggests table names, columns, and SQL keywords as you type} for every table, column, and function in your warehouse. Browse tables, build charts, or inspect {query plans|Shows how DuckDB will execute your SQL — useful for optimization}.

**Kenji:** Two glossary terms. Light. And the background shows a real query with real results. This card will look great.

**Freya:** Should we mention the Tables browser here? Or is that redundant with card 2?

**Lars:** Card 2 showed tables from the Tables panel. This card is about the Query panel. Different tab. I'd keep them separate — Tables is for browsing, Query is for writing SQL.

**Maren:** Agreed. Don't mention Tables here. Keep it focused on the SQL experience.

---

### CARD 7: THE DAG

**Current:** Dependency graph, change detection, rebuilds. Navigate: DAG

**Kenji:** The DAG is our most visual feature. Canvas-based, nodes represent models, edges show dependencies. When this card activates, I can navigate to the DAG panel and call "fit to view" to show the entire graph.

**Lars:** The DAG is where data engineers live. This is how I understand a project's complexity. But the current card talks about "change detection" and "rebuilds" — that's too much for an onboarding card. Just show the graph and explain what it represents.

**Maren:** I want this card to feel like a reveal moment. The DAG is visually striking — dozens of nodes connected by edges, colored by schema layer. When the user sees it, they should think "oh, this is serious software."

**Freya:** Can we animate the DAG rendering? Like, have the nodes appear with a brief staggered animation when the card activates?

**Kenji:** The DAG currently renders all at once. Adding a staggered entrance animation on the canvas is possible but non-trivial — it would need changes to the canvas rendering loop. I'd rather not introduce that complexity. What I CAN do is trigger a smooth "zoom to fit" animation that takes about 500ms, which gives a nice sense of the graph settling into view.

**Maren:** That's fine. The zoom-to-fit animation already gives movement. And the nodes are colored by schema, right?

**Kenji:** Yes — landing is gray, bronze is amber/brown, silver is blue-gray, gold is yellow/gold. The colors match the schema concept from card 2.

**Lars:** Callback to the colored pills from card 2! The user will recognize the colors.

**Maren:** Exactly. Visual consistency. Copy:

> **The DAG**
> Every model and its dependencies, visualized. Nodes are colored by layer — the same layers you saw in your warehouse. When you change a SQL file, havn knows exactly which {downstream models|Models that depend on the one you changed — they need rebuilding too} need rebuilding.

**Lars:** "The same layers you saw in your warehouse" — nice callback. And "downstream models" is a great glossary term because it's jargon that data engineers use but newcomers might not know.

**Freya:** Only one glossary term. Is that enough?

**Maren:** For this card, yes. The DAG is visual — let the graph do the talking. Less text is more.

**Kenji:** Should we highlight a specific node? I can programmatically select a node in the DAG, which gives it a selection ring.

**Lars:** Select a gold model — the "end" of the pipeline. Then the user sees the path from landing through bronze, silver, to gold. The graph tells the story.

**Kenji:** I'll select the gold node and optionally trace its upstream path with highlighted edges if that's feasible. At minimum, the node selection is easy.

**Maren:** If you can highlight the upstream path — edges in a brighter color leading to the selected gold node — that would be incredible. It literally shows the "data flow" concept.

**Kenji:** The DAG panel has a "lineage trace" feature that highlights upstream/downstream when you click a node. I can trigger that programmatically. So yes — select the gold node, trigger lineage trace, upstream path lights up.

**Freya:** That's our visual highlight moment. Lock it.

---

### CARD 8: DATA QUALITY

**Current:** Assertions, contracts, freshness, anomalies. Navigate: Quality

**Freya:** Here's where I start questioning whether this card earns its place. Data quality is important, but it's an advanced feature. A new user running through the tour for the first time might not have assertions set up yet. What do they see in the Quality panel?

**Kenji:** If no assertions have run, the Quality panel shows an empty state. No assertion results, no contract results, no anomalies. It would look blank.

**Lars:** That's a problem. Showing someone an empty panel during onboarding is deflating. But data quality IS a selling point — "this tool watches your data for you." We can't cut it.

**Maren:** What if the scaffold project includes a few sample assertions in the SQL files? Like `-- assert: row_count > 0` in one of the gold models? Then when the pipeline runs, there are actual assertion results to show.

**Lars:** The scaffold should definitely include that. A couple of assertions and maybe one contract YAML file. Then if the user ran the pipeline (card 5), the Quality panel has real data.

**Kenji:** That's a scaffold change, not an onboarding change. But assuming the scaffold has assertions, the Quality panel would show results after a pipeline run.

**Freya:** So this card's value depends on whether the user clicked Run on card 5. If they did, there's data. If they didn't, it's empty.

**Maren:** We can handle the empty case gracefully. If no assertion results exist, the card copy could shift to be more aspirational: "Add assertions to your SQL files to validate data automatically." But I'd rather design for the happy path where they did run the pipeline.

**Lars:** Copy for the happy path:

> **Data Quality**
> Add {assertions|Checks written as SQL comments, like: -- assert: row_count > 0} to your SQL files — havn validates them on every run. Set up {contracts|YAML files that define expected row counts, freshness, and column rules} for stricter guarantees. When something breaks, you'll know before your stakeholders do.

**Freya:** "Before your stakeholders do" — that's the emotional hook. Every data engineer has been burned by a silent pipeline failure.

**Maren:** Two glossary terms. Good density. For the background, navigate to the Quality panel. If there are assertion results, great. If not, the panel has its own empty state.

**Kenji:** I should note — the Quality panel is under Observe, not Explore. So navigating there means switching to the Observe section. The section change is fine, just want to make sure the onboarding navigation handles section switches smoothly.

**Maren:** Transitions between sections should use the same animation the app normally uses. The onboarding shouldn't feel different from normal navigation.

**Lars:** Agreed. No special treatment for section switches.

---

### CARD 9: CONNECT YOUR SOURCES

**Current:** 10+ connectors, auto-generated scripts. Navigate: Data Sources

**Freya:** This card is about future action — "here's how you'll bring your own data in." It's forward-looking, not demonstrating current state. I think it belongs near the end, which is where it is.

**Lars:** Data Sources panel shows the connector catalog, right? Postgres, MySQL, REST API, CSV, S3, etc. That's a good thing to show — it communicates breadth. "We integrate with things you already use."

**Kenji:** Data Sources is under Develop. When this card activates, I navigate to Develop > Data Sources, which shows the connector grid/list.

**Maren:** The connector grid is visual — icons for each connector type. That's a good background. It's colorful and communicates capability without the user having to read anything.

**Lars:** The copy should emphasize how easy it is. "Pick a connector, fill in the details, havn generates the ingest script for you." That's the magic — you don't write the ingest code, it's generated.

**Freya:** Copy:

> **Connect Your Sources**
> When you're ready for your own data, pick a {connector|Pre-built integrations for databases, APIs, files, and SaaS tools} — Postgres, MySQL, REST APIs, CSV, S3, and more. havn generates the ingest script for you. Just add credentials to your {.env file|A local file for secrets like passwords and API keys. Never committed to git.}.

**Maren:** Two glossary terms. ".env file" is a great one — people always wonder where secrets go.

**Lars:** "When you're ready for your own data" — good framing. It acknowledges they're looking at sample data and primes them for the next step.

**Kenji:** No interactive elements needed beyond showing the connector catalog. This is an informational card.

**Freya:** Should we highlight the "Add Connection" button or whatever the CTA is in Data Sources?

**Kenji:** There's a button or card-per-connector layout. I could highlight the grid area with a subtle border pulse.

**Maren:** Light touch. Just the navigation is enough. The grid of connector icons is visually compelling on its own.

---

### CARD 10: YOU'RE IN CONTROL

**Current:** Keyboard shortcuts, replay from Settings. Navigate: none

**Maren:** The last card needs to feel like a send-off, not a whimper. Currently it's about keyboard shortcuts and Settings. That's the most boring possible ending.

**Freya:** Agreed. The last card should create momentum. It should say "you're ready, go do something." Not "here's where the settings are."

**Lars:** What does the user need to hear last? "You understand the tool, here's your next step." The next step is: edit a SQL file, run the pipeline, see results. Or: connect your own data source.

**Maren:** I'd reframe this entirely. Title: "You're Ready" or "Set Sail" (keeping the harbour metaphor). The content should be a gentle push to action plus a reminder that help is available.

**Freya:** "Set Sail" is a bit twee. "You're Ready" is clean.

**Lars:** "Your harbour, your data." Quick, Nordic, done.

**Maren:** Copy:

> **You're Ready**
> Edit any SQL file to change your warehouse. Run the pipeline to rebuild. Query the results.
> Open the {command palette|Press Ctrl+K to quickly find files, tables, and commands} with Ctrl+K to find anything fast. Replay this tour anytime from Settings.
> Data in safe waters.

**Freya:** "Data in safe waters" as the closing line. The tagline lands better as a farewell than as an opening.

**Lars:** The three-verb opening is strong: "Edit. Run. Query." That's the core loop.

**Kenji:** For navigation — this card should go back to Overview or stay wherever the user is. I'd vote for navigating to the Editor, actually. Leave them in the place where they're most likely to start working: the code editor, with a file open, ready to edit.

**Maren:** That's smart. End in the editor. The file tree is visible, a SQL file is open, the user's cursor is ready. The tour dissolves and they're already in their workspace.

**Freya:** One glossary term — command palette. Perfect for the last card. It's a power-user feature that rewards discovery.

**Kenji:** When the card closes (user clicks "Done" or the final button), I want a clean exit animation. The card fades down and out — inverse of the entrance animation. Maybe 200ms. No confetti, no celebration screen. Just a clean departure.

**Maren:** Absolutely. The card leaves quietly and the user is just... in the app. Working. That's the ideal ending.

---

### OVERALL FLOW DISCUSSION

**Freya:** Alright, let's step back. We discussed cutting card 3 (The Data Flow) and replacing it with "Your Project" (about file structure). That gives us:

1. Welcome to havn
2. Your Warehouse
3. Your Project
4. Writing Transforms
5. Running Pipelines
6. Exploring Data
7. The DAG
8. Data Quality
9. Connect Your Sources
10. You're Ready

Is 10 the right number?

**Maren:** 10 is at the upper end. Industry best practice for product tours is 5-7 steps. But each of our cards is lightweight — short text, immediate visual payoff. If the average time per card is 10-15 seconds, the whole tour is under 3 minutes. That's acceptable.

**Lars:** I could see merging Data Quality and Connect Your Sources into one "What's Next" card, bringing us to 9. But they're genuinely different topics.

**Kenji:** 10 cards with good pacing is fine. The dot indicators show progress, and the user can skip at any time. I'd rather have 10 focused cards than 7 cramped ones.

**Freya:** Let's keep 10. But I want to discuss order. Currently the progression is: concept → concept → concept → concept → ACTION → explore → visualize → quality → connect → farewell. Four concepts before the first action is a lot.

**Maren:** What if we moved "Running Pipelines" earlier? Like card 3?

**Lars:** That would be: Welcome, Warehouse, Run Pipeline, Your Project, Writing Transforms... no, that's weird. You haven't seen the files yet and you're told to run them.

**Freya:** The current order is actually logical: here's what's in the warehouse, here's the file structure, here's how to write SQL, here's how to run it, here's how to explore results, here's the graph, here's quality, here's connectors, here's goodbye. It follows the natural learning path.

**Lars:** I agree. The order is sound. Don't reorder.

**Maren:** One tweak: swap cards 6 and 7. Go from "Running Pipelines" directly to "The DAG" — the DAG is the visualization of what just happened. Then "Exploring Data" (query runner) is a natural follow-up: "now poke around yourself." The DAG is more of a continuation of the pipeline concept, while the query panel is a new activity.

**Lars:** Good call. After running the pipeline, you want to see the graph of what ran. Then you want to query the results.

**Freya:** New order:

1. Welcome to havn
2. Your Warehouse
3. Your Project
4. Writing Transforms
5. Running Pipelines
6. The DAG
7. Exploring Data
8. Data Quality
9. Connect Your Sources
10. You're Ready

**Everyone:** Yes. Better.

**Freya:** What's the "aha moment"?

**Lars:** For me, it's card 4 — seeing real SQL in Monaco with the config comments. That's where I understand the authoring model.

**Maren:** For a less technical user, it's card 7 — querying data and seeing results. That's tangible.

**Freya:** I think the aha moment is actually the transition from card 5 to card 6. You hit Run, the pipeline starts (or you're told how), and then the DAG appears showing everything that just happened, with the lineage trace lit up. The combination of action + visualization is the peak.

**Kenji:** That means cards 5 and 6 need to be especially polished. The transition between them should feel seamless — the pipeline is still running (or just finished) and the DAG is the visual confirmation.

---

### INTERACTION DESIGN DISCUSSION

**Maren:** Let's talk about the card itself. Currently it's a floating panel in the bottom-right. I think that's the right default position, but it should be draggable. Users might want to move it to see something it's covering.

**Kenji:** Draggable is easy to implement. Click and drag on the header bar. I'd store the position in session state so it persists through card transitions but resets on next visit.

**Lars:** What about mobile / small screens? Does anyone use a data platform on mobile?

**Kenji:** Unlikely for havn. But on smaller desktop screens (1280px width), the card in the bottom-right might cover important UI. I'd make the card responsive — on screens below 1440px, reduce the card width from whatever it is to maybe 380px. Below 1280px, consider positioning it center-bottom instead of bottom-right.

**Maren:** Card dimensions: I'd say 460px wide, auto-height based on content. The illustration is 180px in the bottom-right corner of the card. Text wraps around or sits above it. Max height maybe 280px before we need to scroll, which we should avoid — if content is too long, cut it.

**Freya:** Transitions between cards — how should they feel?

**Maren:** Content crossfade. The card container stays in place, but the title, text, and illustration crossfade with a slight horizontal offset based on direction. Going forward: content slides left 20px and fades out, new content slides in from the right. Going backward: reverse. Duration: 250ms, ease-out. The dot indicators update immediately.

**Kenji:** That's clean to implement. CSS transitions on the content container with a direction state variable.

**Lars:** What about keyboard navigation? The current implementation supports arrow keys. That's good for power users.

**Kenji:** Left/Right arrows for prev/next, Escape to dismiss the tour. Those should be documented in accessibility terms but not shown in the UI (keyboard users will try them naturally).

**Maren:** Button states: "Previous" should be subtle (text button or ghost button), "Next" should be primary (filled, accent color). On the last card, "Next" becomes "Done" or "Start Building." The Skip button should be the most subtle — small text, low contrast. We want to make progression the easy path, skipping the conscious choice.

**Freya:** "Start Building" as the final button text. Love it.

**Kenji:** The Skip button — should it say "Skip Tour" or just "Skip"? And should it appear on every card?

**Freya:** "Skip tour" on every card. Consistent. Some users will want to skip from card 1, others from card 6.

**Maren:** Place "Skip tour" as a text link below the main buttons, not alongside them. It's an escape hatch, not a primary action.

**Kenji:** Now, the elephant in the room — empty project state. What if someone installs havn but doesn't run `havn init`? Or runs it with a custom project that has no sample data?

**Freya:** The onboarding should only trigger on projects created with `havn init`, which includes the scaffold. If someone starts havn with an empty directory or a custom project with no scaffold, we should either skip the tour or show a simplified version.

**Kenji:** We can detect the scaffold by checking if sample files exist (like `transform/silver/order_metrics.sql` or a `.havn-tour` marker file). If the scaffold isn't there, suppress the tour or show just cards 1, 4, and 10 (welcome, how transforms work, goodbye).

**Lars:** That's pragmatic. A user with their own project doesn't need the full tour — they probably already know what a data warehouse is.

**Maren:** Agreed. Full tour for scaffold projects only. For custom projects: a minimal 3-card "quick intro" — welcome, transforms syntax, keyboard shortcuts.

**Freya:** Last topic: should there be any "try it yourself" moments besides the card 5 Run invitation?

**Maren:** Card 7 (Exploring Data) could have one. The query is pre-populated and auto-run, but we could add: "Try editing the query — autocomplete will help." That invites interaction without requiring it.

**Lars:** And on card 4 (Writing Transforms), we could say "Try changing the SQL — havn will detect the change." But that might be too much during a tour.

**Freya:** Let's limit interactive invitations to two: card 5 (Run) and card 7 (Edit the query). Two moments of agency in a 10-card tour is the right ratio.

**Maren:** One more thing on premium feel — the harbour illustrations. Each card has a 180px isometric harbour scene. These should evolve across the tour. Card 1: empty harbour, calm water. Card 5: boats arriving. Card 10: full harbour, active port. The illustration tells its own story of building something.

**Kenji:** We already have the illustrations as PNGs. If they're designed as a progression, that's amazing. If they're random harbour scenes, let's at least make sure they thematically match the card content.

**Maren:** They should be a curated set. Not random. Each one slightly more "alive" than the previous.

**Freya:** That's a design deliverable outside this meeting, but yes — illustration progression is in the spec.

---

## FINAL SPECIFICATION

---

### Card 1: Welcome to havn

**Title:** Welcome to havn

**Copy:**
> Your entire data warehouse lives in one file on your machine. No cloud, no accounts, no data leaving your network.
> This tour takes about two minutes. Let's look around.

**Glossary terms:** None

**Navigation target:** None (stay on Overview)

**Auto-actions:**
- Dashboard stat counters animate in with staggered roll-up (0 → actual value, 400ms stagger per stat)

**Highlight targets:** None

**Animations/micro-interactions:**
- Card enters with 12px upward drift + fade-in, 300ms ease-out
- Dashboard stats roll up in background

**Background state:** Overview dashboard visible

**Illustration:** Full harbour overview — calm water, empty docks, the harbour at rest

**Edge cases:**
- If Overview has no stats yet (fresh project, never run), stats show zeros — that's fine, they still animate
- If user navigated away from Overview before tour starts, navigate back to Overview

**Buttons:** Next (primary), Skip tour (subtle text link below)

---

### Card 2: Your Warehouse

**Title:** Your Warehouse

**Copy:**
> All your data lives in one file, organized into layers that refine it step by step:
> `landing` → `bronze` → `silver` → `gold`
> Powered by {DuckDB|A fast, embedded analytics database. Runs in-process — no server needed.} — everything stays on your machine.

The layer names in the flow line render as inline colored pills matching schema colors (landing: gray, bronze: amber, silver: blue-gray, gold: yellow).

**Glossary terms:** DuckDB (1 term)

**Navigation target:** Explore > Tables

**Auto-actions:**
- Switch to Tables panel
- Expand all schema tree nodes (landing, bronze, silver, gold)
- Auto-select `gold.order_summary` (fallback: first table in gold, then first table in any schema)
- Table data grid populates with selected table's rows

**Highlight targets:** None (the expanded schema tree IS the visual teaching moment)

**Animations/micro-interactions:**
- Schema tree nodes expand with a brief stagger (50ms between each)
- Colored pills in card text use the same colors as schema tree icons

**Background state:** Tables panel with schema tree expanded, gold table selected, data visible in grid

**Illustration:** Harbour with four docks/piers of increasing size — representing the four layers

**Edge cases:**
- If no tables exist (never run pipeline), show empty schema tree with schemas still listed. Table grid shows empty state: "Run your pipeline to populate tables."
- If gold schema is empty, select the first available table in any schema

**Buttons:** Previous (ghost), Next (primary), Skip tour (text link)

---

### Card 3: Your Project

**Title:** Your Project

**Copy:**
> Your pipeline is defined by files. {Ingest scripts|Python files in ingest/ that pull raw data from external sources into landing/} bring data in, {SQL transforms|.sql files in transform/ that define how data moves between schema layers} shape it layer by layer, and {export scripts|Python files in export/ that send finished data to external systems} send it out. The file tree on the left is your pipeline.

**Glossary terms:** Ingest scripts, SQL transforms, export scripts (3 terms)

**Navigation target:** Develop > Editor

**Auto-actions:**
- Switch to Editor view
- Expand file tree root
- Expand `ingest/`, `transform/`, `transform/bronze/`, `transform/silver/`, `transform/gold/`, `export/` folders
- Do NOT open any file (keep editor empty or showing a welcome state) — the file tree IS the focus

**Highlight targets:**
- Briefly highlight (soft background color pulse, 2s fade) the folder rows for `ingest/`, `transform/`, `export/` in the file tree

**Animations/micro-interactions:**
- File tree folders expand with a staggered animation (100ms between each top-level folder)
- Folder highlight: soft teal/blue background on the folder row, fades out over 2 seconds

**Background state:** Editor view with expanded file tree, no file open in editor (or editor shows a minimal placeholder)

**Illustration:** Harbour with crates being sorted into different warehouse buildings

**Edge cases:**
- If project has no ingest/ or export/ folder (custom project), expand whatever folders exist
- If file tree is empty, card still shows — the text explains the structure conceptually

**Buttons:** Previous (ghost), Next (primary), Skip tour (text link)

---

### Card 4: Writing Transforms

**Title:** Writing Transforms

**Copy:**
> Every .sql file in transform/ becomes a model in your warehouse. Two comment lines are all havn needs:
> {-- config:|Sets materialization (table or view), target schema, and incremental strategy} for options, {-- depends_on:|Declares which upstream models this one reads from. havn uses this to build the execution order.} for dependencies.
> The rest is pure SQL — no templates, no special syntax.

**Glossary terms:** -- config:, -- depends_on: (2 terms)

**Navigation target:** Develop > Editor

**Auto-actions:**
- Open `transform/silver/order_metrics.sql` in Monaco editor (fallback: first .sql file in transform/ that has both config and depends_on comments; final fallback: any .sql file in transform/)
- Scroll editor to top
- Add Monaco decorations: soft yellow highlight (background color `rgba(255, 235, 150, 0.15)`) on lines containing `-- config:` and `-- depends_on:`

**Highlight targets:**
- Monaco line decorations on the config/depends_on comment lines (as described above)

**Animations/micro-interactions:**
- Monaco decorations appear with a 500ms fade-in after the file opens
- Decorations removed when user advances to next card

**Background state:** Editor with a silver SQL file open, config/depends_on lines subtly highlighted, file tree still visible on the left

**Illustration:** Harbour with a crane lifting and transforming cargo between docks

**Edge cases:**
- If no SQL files have config/depends_on comments, open any .sql file and skip the decorations
- If transform/ folder is empty, open any SQL file in the project; if none exist, show editor empty state and adjust copy to: "Create .sql files in transform/ to build your pipeline."

**Buttons:** Previous (ghost), Next (primary), Skip tour (text link)

---

### Card 5: Running Pipelines

**Title:** Running Pipelines

**Copy:**
> Hit Run to execute your full pipeline — {ingest|Python scripts that pull raw data from sources into landing/}, then {transforms|SQL models that process data through bronze → silver → gold}, then {exports|Python scripts that push finished data to dashboards, APIs, or files}. Output streams live so you see every step as it happens.
> Go ahead — try it now, or keep exploring.

**Glossary terms:** ingest, transforms, exports (3 terms)

**Navigation target:** Overview

**Auto-actions:**
- Switch to Overview
- Ensure output panel at bottom is expanded/visible (not collapsed)

**Highlight targets:**
- Pulsing ring animation on the Run button (quick actions area)
- Pulse color: brand accent (harbour teal, e.g., `#0d9488` or theme accent)
- Pulse: 2s cycle, subtle scale (1.0 → 1.05 → 1.0) with box-shadow glow

**Animations/micro-interactions:**
- Run button pulse starts when card appears, stops when user advances
- If user clicks Run, pipeline starts streaming in the output panel behind the card — no special handling needed, the app behaves normally

**Background state:** Overview with output panel visible, Run button pulsing

**Illustration:** Harbour with conveyor belts moving crates from ship to warehouse

**Edge cases:**
- If a pipeline is already running, suppress the pulse (don't invite a second run)
- If there are no ingest/transform/export files, the Run button might not work — show card anyway but remove the "try it now" line

**Buttons:** Previous (ghost), Next (primary), Skip tour (text link)

---

### Card 6: The DAG

**Title:** The DAG

**Copy:**
> Every model and its dependencies, visualized. Nodes are colored by layer — the same layers you saw in your warehouse. When you change a SQL file, havn knows exactly which {downstream models|Models that depend on the changed one and need to be rebuilt. havn rebuilds only what's necessary.} need rebuilding.

**Glossary terms:** downstream models (1 term)

**Navigation target:** Explore > DAG

**Auto-actions:**
- Switch to DAG panel
- Trigger "fit to view" (smooth zoom animation, ~500ms)
- After fit-to-view settles, select the primary gold node (e.g., `gold.order_summary`)
- Trigger upstream lineage trace on the selected node (highlights the path from source to gold)

**Highlight targets:**
- DAG handles its own visual highlighting via the lineage trace (upstream edges and nodes get brighter/bolder styling)

**Animations/micro-interactions:**
- Smooth zoom-to-fit animation when card activates (~500ms ease-out)
- Lineage trace highlights upstream path after node selection (~200ms delay after selection)

**Background state:** Full DAG visible, zoomed to fit, gold node selected with upstream lineage path highlighted

**Illustration:** Harbour map/chart showing shipping routes between ports

**Edge cases:**
- If DAG has no nodes (no transform files), show empty DAG canvas — card still explains the concept
- If gold node doesn't exist, select the last node in topological order
- If only one node exists, skip lineage trace (nothing to trace)

**Buttons:** Previous (ghost), Next (primary), Skip tour (text link)

---

### Card 7: Exploring Data

**Title:** Exploring Data

**Copy:**
> Write SQL, get answers. The editor has {autocomplete|Suggests table names, columns, functions, and keywords as you type. Powered by your warehouse schema.} for every table and column in your warehouse. Try editing the query below — or write your own.

**Glossary terms:** autocomplete (1 term)

**Navigation target:** Explore > Query

**Auto-actions:**
- Switch to Query panel
- Set editor content to: `SELECT * FROM gold.order_summary LIMIT 20`
- Auto-execute the query (trigger run)
- Results grid populates below editor

**Highlight targets:** None (the live query results ARE the highlight)

**Animations/micro-interactions:**
- Query executes ~200ms after card renders (brief loading state, then results appear)
- Results grid rows could fade in with a very subtle stagger (barely perceptible, 30ms per row)

**Background state:** Query panel with a pre-populated query that has already returned results. Editor on top, results grid below.

**Illustration:** Harbour control tower with a telescope / lookout — representing exploration

**Edge cases:**
- If `gold.order_summary` doesn't exist, try: `SELECT 'Hello from havn!' AS message, current_timestamp AS queried_at` as a safe fallback query
- If DuckDB connection fails, show query editor with the pre-populated query but no results — card still works conceptually
- If pipeline hasn't run and gold tables are empty, the query returns 0 rows — that's informative too ("run the pipeline first!")

**Buttons:** Previous (ghost), Next (primary), Skip tour (text link)

---

### Card 8: Data Quality

**Title:** Data Quality

**Copy:**
> Add {assertions|Checks embedded as SQL comments in your model files. Example: -- assert: row_count > 0. Validated automatically on every pipeline run.} to your SQL files — havn validates them every time the pipeline runs. Define {contracts|YAML files with rules like minimum row counts, freshness thresholds, and column constraints. Stored in contracts/.} for stricter guarantees across models. When something breaks, you'll know before your stakeholders do.

**Glossary terms:** assertions, contracts (2 terms)

**Navigation target:** Observe > Quality

**Auto-actions:**
- Switch to Quality panel
- If assertion results exist, show them (default view)
- If no results exist, panel shows its standard empty state

**Highlight targets:** None

**Animations/micro-interactions:** None beyond standard section transition

**Background state:** Quality panel showing assertion results (if pipeline has run) or empty state

**Illustration:** Harbour inspector checking cargo with a clipboard

**Edge cases:**
- If pipeline hasn't run: Quality panel is empty. Card copy still works — it describes what WILL happen.
- If scaffold doesn't include assertions (it should, but just in case): empty state is acceptable. The card plants the concept for later.

**Buttons:** Previous (ghost), Next (primary), Skip tour (text link)

---

### Card 9: Connect Your Sources

**Title:** Connect Your Sources

**Copy:**
> When you're ready for your own data, pick a {connector|Pre-built integrations for common data sources: databases like Postgres and MySQL, APIs like Stripe and HubSpot, files from S3 and GCS, and more.} — Postgres, MySQL, REST APIs, CSV, Google Sheets, and more. havn generates the ingest script for you. Just add your credentials to the {.env file|A local secrets file in your project root. Environment variables like DB_HOST and API_KEY go here. Never committed to version control.}.

**Glossary terms:** connector, .env file (2 terms)

**Navigation target:** Develop > Data Sources

**Auto-actions:**
- Switch to Data Sources panel
- Show connector catalog/grid

**Highlight targets:** None (the connector grid is visually rich on its own)

**Animations/micro-interactions:** None beyond standard section transition

**Background state:** Data Sources panel showing the grid of available connectors with their icons

**Illustration:** Harbour with different ships arriving from different directions (representing diverse data sources)

**Edge cases:**
- Data Sources panel always has content (it shows available connector types, not configured ones), so no empty state concern

**Buttons:** Previous (ghost), Next (primary), Skip tour (text link)

---

### Card 10: You're Ready

**Title:** You're Ready

**Copy:**
> Edit a SQL file. Run the pipeline. Query the results. That's the core loop.
> Press {Ctrl+K|Opens the command palette — search for files, tables, SQL models, and commands from anywhere in the app.} to find anything fast. Replay this tour anytime from Settings.
> Data in safe waters.

**Glossary terms:** Ctrl+K (1 term — technically a shortcut, styled as a keyboard shortcut badge rather than dotted underline)

**Navigation target:** Develop > Editor

**Auto-actions:**
- Switch to Editor
- Open the same silver SQL file from card 4 (bringing the user full circle)
- Place cursor at end of file (ready to edit)

**Highlight targets:** None

**Animations/micro-interactions:**
- "Done" button replaces "Next" — styled as primary with slightly more emphasis (e.g., "Start Building" text)
- On dismiss: card fades down 12px + fades out, 200ms ease-in
- No confetti. No modal. Clean exit.

**Background state:** Editor with SQL file open, file tree visible, cursor blinking. The user is in their workspace.

**Illustration:** Full harbour, bustling — ships loaded, cranes moving, the port alive. The culmination of the illustration progression.

**Edge cases:**
- If the SQL file from card 4 no longer exists (user deleted it during tour), open any .sql file; if none exist, show empty editor
- "Replay from Settings" text should be accurate — verify Settings has the replay button

**Buttons:** Previous (ghost), Start Building (primary, accent color), Skip tour (hidden on last card)

---

### GLOBAL SPECIFICATIONS

**Card container:**
- Width: 460px
- Max height: 300px (no scrolling — if content exceeds, trim copy)
- Border radius: 12px
- Background: solid white (light theme) / solid dark gray (dark theme), respects current theme
- Shadow: `0 8px 32px rgba(0,0,0,0.12)` (light) / `0 8px 32px rgba(0,0,0,0.4)` (dark)
- Position: fixed, bottom-right (24px from edges)
- Draggable by header area; position resets between sessions
- Z-index: above all app content but below modals

**Card layout (internal):**
- Top: Title (18px, semi-bold)
- Middle: Description text (14px, regular, line-height 1.5)
- Bottom-right: 180px illustration (PNG, positioned absolutely within card)
- Text content has right padding to avoid overlapping illustration (~200px right margin on last 2-3 lines)
- Bottom bar: dot indicators (center), Previous (left), Next/Done (right)
- "Skip tour" text link: below button bar, centered, 12px, 50% opacity

**Transitions between cards:**
- Content area (title + description + illustration) crossfades with directional offset
- Forward: old content slides left 20px + fades out; new content slides in from right 20px + fades in
- Backward: reverse direction
- Duration: 250ms, ease-out
- Dot indicators update immediately (no animation)
- Background navigation happens 100ms before content transition starts (so the background is settling while the card transitions)

**Keyboard navigation:**
- Right arrow / Down arrow: Next card
- Left arrow / Up arrow: Previous card
- Escape: Dismiss tour (same as Skip)
- Enter: Activate primary button (Next/Done)

**Glossary terms rendering:**
- Dotted underline (1px, 50% opacity of text color)
- On hover: tooltip appears above the term, 240px max width, 12px padding, same card background, smaller text (13px), 150ms fade-in
- Max 3 glossary terms per card
- Terms should explain concepts a new user might not know, not basic words

**Progress indicators:**
- 10 dots, horizontally centered in card footer
- Current dot: filled, accent color
- Other dots: outlined or low-opacity
- Dots are clickable (jump to that card)

**Responsive behavior:**
- Below 1440px viewport width: card width reduces to 400px
- Below 1280px viewport width: card repositions to center-bottom (centered horizontally, 24px from bottom)
- Below 1024px viewport width: card becomes full-width bottom sheet (border-radius only on top corners)

**Tour trigger conditions:**
- Show on first visit (tracked via localStorage key `havn-tour-completed`)
- Only show if scaffold files are detected (check for `transform/` folder with .sql files)
- Can be replayed from Settings (clears the localStorage key)
- If scaffold not detected: show abbreviated 3-card tour (cards 1, 4, 10 only, with adjusted copy)

**Illustration progression (creative direction for design):**
1. Calm harbour, empty docks, still water — potential
2. Four docks of increasing size — structure  
3. Crates being sorted between buildings — organization
4. Crane lifting cargo between docks — transformation
5. Conveyor belts, activity starting — movement
6. Bird's eye map with routes highlighted — connections
7. Control tower with telescope — observation
8. Inspector with clipboard — validation
9. Ships arriving from different directions — integration
10. Full bustling port, everything alive — completion

**Performance considerations (Kenji's notes):**
- All auto-actions (navigation, file opening, query execution) should have a 150ms delay after card transition completes, to avoid visual jank from simultaneous animations
- Monaco decorations are lightweight (CSS-only), no performance concern
- DAG fit-to-view uses requestAnimationFrame, naturally smooth
- Pre-populate query text synchronously (no async needed), execute query async
- File tree expansion uses existing state management, batch all expansions in one update to avoid cascading re-renders
