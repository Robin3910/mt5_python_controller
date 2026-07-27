# Dashboard Homepage Layout Redesign

**Date:** 2026-07-16  
**Scope:** Frontend-only redesign of the dashboard (`/`) to match the provided design mockup, using existing hub store data.  
**Out of scope:** Backend API changes, new P/L endpoints, topbar/nav changes, other pages.

## Decisions

| Topic | Choice |
|-------|--------|
| Approach | Single-file enhancement of `DashboardView.vue` + CSS in `styles.css` |
| Summary cards | 3 cards only: online nodes, total equity, total positions |
| Today's P/L card | **Not implemented** (no daily P/L in existing data) |
| Interactions | Full client-side: search, status filter, sort, pagination, view toggle, event tabs, mark-as-read |

## Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Summary: 在线节点 | 合计净值 | 合计持仓                       │
├────────────────────────────────────────────┬────────────────┤
│  Toolbar: search · status · sort · view    │  实时事件       │
│  节点列表 · N 个结果                         │  全部标记已读   │
│  ┌──────┐ ┌──────┐ ┌──────┐                 │  全部|风险|交易 │
│  │ Node │ │ Node │ │ Node │  (grid ~3 col) │  event list    │
│  └──────┘ └──────┘ └──────┘                 │  查看全部事件→  │
│  Pagination                                │                │
└────────────────────────────────────────────┴────────────────┘
```

- Preserve existing app shell (topbar, routes).
- Main column + right event rail; responsive collapse of grid columns on smaller widths.
- Visual language: existing dark glass tokens; add dashboard-specific classes only.

## Data Sources (existing only)

| UI element | Source |
|------------|--------|
| Node list | `hub.nodes` |
| Online/offline | `hub.statuses[id]` fallback `n.status` |
| Balance / equity / margin / positions | `hub.accounts[id]` |
| Online count / total equity | existing getters / computed |
| Total positions | sum of `accounts[*].positions.length` |
| Events | `hub.events` (`HubEvent`: `ts`, `text`, `kind`) |
| Close actions | existing `hub.closeNode` + confirm |

## Node Board Interactions

Local state on the page (not persisted):

- `q` — case-insensitive match against `name`, `node_id`, account/login, server
- `statusFilter` — `all` | `online` | `offline`; chip counts always from full `hub.nodes` by status (not reduced by search); pipeline order: status → search → sort → paginate
- `sortBy` — default `equity_desc`; also `equity_asc`, `name`
- `viewMode` — `grid` | `list`
- `page` / `pageSize = 9` — slice after filter+sort; reset page to 1 when `q` / `statusFilter` / `sortBy` changes

### Node card

- Header: status dot, name, online/offline tag
- Subline: `login @ server`
- Metrics: 余额 / 净值 / 占用保证金
- Positions preview: up to 3 rows (symbol, direction, volume, profit with green/red)
- Footer: link「查看全部 N 个持仓 →」→ `/nodes/:id`; danger button「平掉该节点全部」
- Per-ticket close kept on preview rows
- Overflow menu: optional single action「查看详情」→ same route (no empty menu)

Empty state: keep guidance to create nodes on「节点」page when `hub.nodes` is empty.

## Event Rail Interactions

- Tabs:
  - `全部` — all events
  - `风险` — `kind === 'warn'`
  - `交易` — `kind === 'ok'`
  - `info` events appear only under `全部`
- Read state: in-memory `Set` keyed by `ts + text` (session-only); unread visual emphasis;「全部标记已读」marks every event in `hub.events` as read
- Icon/color by kind: `ok` green, `warn` amber/red, `info` muted blue-gray
- Optional right-aligned delta: parse first `±number` from `text`; omit if none
- Footer link「查看全部事件 →」→ `/events`

## Files to Change

1. `frontend/src/views/DashboardView.vue` — layout, local state, computed pipelines, template
2. `frontend/src/styles.css` — dashboard toolbar, grid, node card refinements, event rail tabs

No store/API/type changes required unless a tiny helper is cleaner inline in the view.

## Error / Edge Handling

- Missing account snapshot: show `—` / `0` consistently with current `fmt` helpers
- Zero positions: muted「无持仓」; disable close-all
- Filter yields empty: show empty list message + keep toolbar
- Event parse failure for amounts: hide amount column for that row

## Testing (manual)

- [ ] Summary numbers match store aggregates
- [ ] Search / status / sort / pagination / view toggle behave and reset page correctly
- [ ] Node card links and close actions still work
- [ ] Event tabs filter; mark-all-read clears unread styling
- [ ] Responsive: grid collapses; event rail stacks or remains usable on narrow viewports
- [ ] No backend/network contract changes
