// BioFlow e2e test harness — Hermes desktop plugin (full page + sidebar nav).
// Single ESM file, loaded uncompiled: use jsx() calls, never JSX syntax.
import { ROUTES_AREA, SIDEBAR_NAV_AREA, useQuery, useMutation, useQueryClient } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let _ctx = null
const api = (path, opts) => _ctx.rest(path, opts)

const GLYPH = { passed: '\u2713', failed: '\u2717', running: '\u2026' }

function StatusBadge({ status }) {
  return jsx('span', {
    style: { fontWeight: 600, fontFamily: 'var(--font-mono, monospace)', fontSize: 12 },
    children: `${GLYPH[status] || '\u2026'} ${status}`,
  })
}

function StepRow({ step }) {
  return jsxs('div', {
    style: {
      padding: '3px 0', fontSize: 12,
      borderBottom: '1px solid var(--ui-stroke-secondary)',
      fontFamily: 'var(--font-mono, monospace)',
    },
    children: [
      jsx(StatusBadge, { status: step.status }),
      jsx('span', { style: { marginLeft: 8 }, children: `#${step.index} ${step.verb}` }),
      jsx('span', {
        style: { marginLeft: 8, color: 'var(--ui-text-quaternary)' },
        children: step.elapsed_ms != null ? `${step.elapsed_ms} ms` : '',
      }),
      step.error ? jsx('div', { style: { marginTop: 2, color: 'var(--ui-text-secondary)' }, children: step.error }) : null,
    ],
  })
}

function RunDetail({ run }) {
  if (!run) return jsx('div', { children: 'Loading\u2026' })
  return jsxs('div', {
    children: [
      jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center' }, children: [
        jsx('h3', { style: { margin: 0 }, children: `Run ${run.run_id.slice(0, 8)}` }),
        jsx(StatusBadge, { status: run.status }),
      ] }),
      run.error ? jsx('div', { style: { color: 'var(--ui-text-secondary)', marginTop: 4 }, children: run.error }) : null,
      ...(run.tests || []).map((t) =>
        jsxs('div', { key: t.name, children: [
          jsx('div', { style: { marginTop: 8, fontWeight: 600 }, children: `${t.name} \u2014 ${t.status}` }),
          ...(t.steps || []).map((s) => jsx(StepRow, { key: s.index, step: s })),
        ] }),
      ),
    ],
  })
}

function TestsPage() {
  const queryClient = useQueryClient()
  const [selectedRunId, setSelectedRunId] = useState(null)

  const testsQ = useQuery({ queryKey: ['bioflow-e2e', 'tests'], queryFn: () => api('/tests') })
  const runsQ = useQuery({ queryKey: ['bioflow-e2e', 'runs'], queryFn: () => api('/runs') })
  const runQ = useQuery({
    queryKey: ['bioflow-e2e', 'run', selectedRunId],
    queryFn: () => api('/runs/' + selectedRunId),
    enabled: !!selectedRunId,
    refetchInterval: 2000,
  })

  const runMutation = useMutation({
    mutationFn: (tests) => api('/runs', { method: 'POST', body: tests ? { tests } : {} }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['bioflow-e2e', 'runs'] })
      setSelectedRunId(data.run_id)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (runId) => api('/runs/' + runId, { method: 'DELETE' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bioflow-e2e', 'runs'] })
      if (selectedRunId) setSelectedRunId(null)
    },
  })

  const btn = {
    padding: '2px 10px', fontSize: 12, cursor: 'pointer', borderRadius: 4,
    border: '1px solid var(--ui-stroke-secondary)', background: 'transparent',
    color: 'var(--ui-text-primary)',
  }

  return jsxs('div', {
    style: { padding: 16, height: '100%', overflow: 'auto', color: 'var(--ui-text-primary)', fontSize: 13 },
    children: [
      jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center' }, children: [
        jsx('h2', { style: { flex: 1, margin: 0 }, children: 'BioFlow E2E Tests' }),
        jsx('button', { style: btn, onClick: () => runMutation.mutate(null), children: 'Run all' }),
      ] }),

      jsx('h3', { children: 'Tests' }),
      (testsQ.data || []).map((t) =>
        jsxs('div', { key: t.name, style: { display: 'flex', gap: 8, alignItems: 'center', padding: '3px 0' }, children: [
          jsx('span', { style: { flex: 1 }, children: t.name }),
          jsx('span', { style: { color: 'var(--ui-text-quaternary)', fontSize: 11 }, children: t.kind }),
          jsx('button', { style: btn, onClick: () => runMutation.mutate([t.name]), children: 'Run' }),
        ] }),
      ),

      jsx('h3', { style: { marginTop: 16 }, children: 'Runs' }),
      (runsQ.data || []).map((r) =>
        jsxs('div', { key: r.run_id, style: { display: 'flex', gap: 8, alignItems: 'center', padding: '3px 0' }, children: [
          jsx(StatusBadge, { status: r.status }),
          jsx('span', { style: { flex: 1, color: 'var(--ui-text-quaternary)', fontSize: 11 }, children: new Date(r.started_at).toLocaleString() }),
          jsx('button', { style: btn, onClick: () => setSelectedRunId(r.run_id), children: 'View' }),
          jsx('button', { style: btn, onClick: () => deleteMutation.mutate(r.run_id), children: 'Delete' }),
        ] }),
      ),

      selectedRunId ? jsx('div', { style: { marginTop: 16 }, children: jsx(RunDetail, { run: runQ.data }) }) : null,
    ],
  })
}

export default {
  id: 'bioflow-e2e',
  name: 'BioFlow E2E',
  register(ctx) {
    _ctx = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/bioflow-e2e' }, render: () => jsx(TestsPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/bioflow-e2e', label: 'E2E Tests', codicon: 'beaker' } },
    ])
  },
}
