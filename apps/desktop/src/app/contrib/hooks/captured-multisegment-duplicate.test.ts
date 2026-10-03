import { expect, it } from 'vitest'

import { collapseDuplicateFinalIntoDurable } from '@/app/session/hooks/use-message-stream/collapse-duplicate-final'
import { reconcileDurableHistory } from '@/app/session/hooks/use-session-actions/utils'
import { type ChatMessage } from '@/lib/chat-messages'

// Reduced from vm-exact.json (2026-10-03): a multi-segment tool turn whose
// saved fold was loaded while the live interim + final segments stayed beside it.
const SEG1 = 'Worker 5 has no source-code changes. I haven’t deleted anything.'
const FINAL = '**The warning is real, but it isn’t work you’ve done.** Check and then remove Worker 5?'
const ids1 = ['c1', 'c2', 'c3']
const ids2 = ['c4', 'c5', 'c6']

const tool = (id: string, extra = {}) => ({
  type: 'tool-call' as const,
  toolCallId: id,
  toolName: 'read_file',
  args: {},
  result: 'ok',
  ...extra
})

const user: ChatMessage = {
  id: '1-user',
  role: 'user',
  rowId: 524535,
  parts: [{ type: 'text', text: 'Delete Worker 5?', sourceRowId: 524535 }]
}

const fold: ChatMessage = {
  id: '1791041753.3405626-76-assistant',
  role: 'assistant',
  rowId: 524552,
  durableComplete: true,
  serverRowSpan: 54,
  parts: [
    ...ids1.map(id => tool(id)),
    { type: 'text', text: SEG1, sourceRowId: 524552 },
    ...ids2.map(id => tool(id)),
    { type: 'text', text: FINAL, sourceRowId: 524679 }
  ]
}

const interim: ChatMessage = {
  id: 'assistant-stream-1791041708548-25',
  role: 'assistant',
  pending: false,
  interim: true,
  completedAt: 1791041752.62,
  parts: [...ids1.map(id => tool(id, { toolResultMetadata: { duration_s: 1.5 } })), { type: 'text', text: SEG1 }]
}

const final: ChatMessage = {
  id: 'assistant-stream-1791041753347-26',
  role: 'assistant',
  pending: false,
  interim: false,
  durationS: 303,
  durableComplete: true,
  rowId: 524679,
  persistedTurn: {
    row_ids: [524535, 524552, 524679],
    complete: true,
    user_row_id: 524535,
    final_assistant_row_id: 524679
  },
  parts: [
    ...ids2.map(id => tool(id, { toolResultMetadata: { duration_s: 0.6 } })),
    { type: 'text', text: '\n\n' + FINAL, sourceRowId: 524679 }
  ]
}

const assistants = (messages: ChatMessage[]) => messages.filter(m => m.role === 'assistant').map(m => m.id)

it('history reconciliation drops live segments covered by the saved fold', () => {
  expect(assistants(reconcileDurableHistory([user, fold], [user, fold, interim, final]))).toEqual([fold.id])
})

it('completion folds the final live segment and its sealed interim into the saved fold', () => {
  const pendingFinal = {
    ...final,
    pending: true,
    durableComplete: undefined,
    rowId: undefined,
    persistedTurn: undefined
  }

  const result = collapseDuplicateFinalIntoDurable([user, fold, interim, pendingFinal], 3, {
    finalText: FINAL,
    hasFailure: false,
    completeMessage: m => ({ ...m, pending: false, durationS: 303 })
  })

  expect(result && assistants(result.messages)).toEqual([fold.id])
  expect(result?.keptId).toBe(fold.id)
  expect(result?.messages[1].parts[0]).toMatchObject({ toolCallId: 'c1', toolResultMetadata: { duration_s: 1.5 } })
})

const complete = (messages: ChatMessage[], index: number) =>
  collapseDuplicateFinalIntoDurable(messages, index, {
    finalText: FINAL,
    hasFailure: false,
    completeMessage: m => ({ ...m, pending: false })
  })

const pendingFinal = { ...final, pending: true, durableComplete: undefined, rowId: undefined, persistedTurn: undefined }

it('keeps an interim segment the saved fold does not fully hold', () => {
  const extra = { ...interim, parts: [...interim.parts, tool('not-saved')] }
  const result = complete([user, fold, extra, pendingFinal], 3)

  expect(result && assistants(result.messages)).toEqual([fold.id, extra.id])
})

it('keeps an interim segment with prose the saved fold lacks', () => {
  const extra = {
    ...interim,
    parts: [...interim.parts.slice(0, 3), { type: 'text' as const, text: 'Different words.' }]
  }

  const result = complete([user, fold, extra, pendingFinal], 3)

  expect(result && assistants(result.messages)).toEqual([fold.id, extra.id])
})

it('does not fold across a later prompt', () => {
  const next = { ...user, id: '2-user', rowId: 530000 }

  expect(complete([user, fold, next, interim, pendingFinal], 4)).toBeNull()
})

it('a saved fold whose last answer differs is not a twin', () => {
  const other = {
    ...fold,
    parts: [...fold.parts.slice(0, -1), { type: 'text' as const, text: 'Another answer.', sourceRowId: 524679 }]
  }

  expect(complete([user, other, interim, pendingFinal], 3)).toBeNull()
})
