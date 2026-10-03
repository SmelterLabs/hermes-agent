import { expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import { collapseDuplicateFinalIntoDurable } from './collapse-duplicate-final'
import captured from './fixtures/duplicate-saved-live-capture.json'

it('collapses the actual installed-renderer capture without mutating it', () => {
  // The capture omits tool arguments/results, which this identity check never reads.
  const messages = captured.messages as unknown as ChatMessage[]
  const before = JSON.stringify(messages)

  const finalText = messages[1].parts
    .filter(p => p.type === 'text')
    .map(p => p.text)
    .join('')
    .trim()

  const result = collapseDuplicateFinalIntoDurable(messages, 2, {
    finalText,
    hasFailure: false,
    completeMessage: m => m
  })!

  expect(result.keptId).toBe('1790999704.658709-119-assistant')
  expect(result.messages).toHaveLength(2)
  expect(result.messages[1]).toMatchObject({ rowId: 517253, durableComplete: true, durationS: 26 })
  expect(result.messages[1].parts[0]).toMatchObject({
    toolCallId: 'toolu_01Fnj8z56mNndL4zBbDyJ6wf',
    toolResultMetadata: { duration_s: 5.851993560791016 }
  })
  expect(JSON.stringify(messages)).toBe(before)
})

const tool = (id: string) => ({
  type: 'tool-call' as const,
  toolCallId: id,
  toolName: 'terminal',
  args: {},
  result: 'ok'
})

const user: ChatMessage = { id: 'user', role: 'user', rowId: 10, parts: [{ type: 'text', text: 'Check' }] }

const durable: ChatMessage = {
  id: 'saved',
  role: 'assistant',
  rowId: 13,
  durableComplete: true,
  serverRowSpan: 3,
  parts: [tool('same-call'), { type: 'text', text: 'Done.', sourceRowId: 13 }]
}

const live: ChatMessage = {
  id: 'assistant-stream-live',
  role: 'assistant',
  pending: true,
  parts: [
    { ...tool('same-call'), toolResultMetadata: { duration_s: 5.9 } },
    { type: 'text', text: '\n\nDone.' }
  ]
}

const options = {
  finalText: 'Done.',
  hasFailure: false,
  completeMessage: (m: ChatMessage): ChatMessage => ({
    ...m,
    pending: false,
    interim: false,
    durationS: 26,
    completedAt: 123,
    durableComplete: false
  })
}

it('folds replay completion into its same-turn durable twin and retains live timing', () => {
  const result = collapseDuplicateFinalIntoDurable([user, durable, live], 2, options)!
  expect(result.keptId).toBe('saved')
  expect(result.messages).toHaveLength(2)
  expect(result.messages[1]).toMatchObject({
    id: 'saved',
    rowId: 13,
    durableComplete: true,
    serverRowSpan: 3,
    durationS: 26,
    completedAt: 123
  })
  expect(result.messages[1].parts[0]).toMatchObject({
    toolCallId: 'same-call',
    toolResultMetadata: { duration_s: 5.9 }
  })
  expect(result.messages[1].parts[1]).toMatchObject({ sourceRowId: 13 })
})

it.each([
  ['distinct tool identity', { ...live, parts: [tool('different-call'), { type: 'text' as const, text: 'Done.' }] }],
  ['new tool not in saved turn', { ...live, parts: [...live.parts, tool('extra-call')] }],
  [
    'additional live prose',
    { ...live, parts: [tool('same-call'), { type: 'text' as const, text: 'New detail. Done.' }] }
  ],
  ['conflicting saved identity', { ...live, rowId: 99 }],
  [
    'unrepresented reasoning',
    { ...live, parts: [...live.parts, { type: 'reasoning' as const, text: 'Additional reasoning.' }] }
  ]
])('preserves %s', (_, distinct) => {
  expect(collapseDuplicateFinalIntoDurable([user, durable, distinct as ChatMessage], 2, options)).toBeNull()
})

it.each([
  ['new prompt', { ...user, id: 'next-user', rowId: 20 }],
  ['hidden new prompt', { ...user, id: 'hidden-user', rowId: 20, hidden: true }]
])('does not match across a %s', (_, prompt) => {
  expect(collapseDuplicateFinalIntoDurable([user, durable, prompt, live], 3, options)).toBeNull()
})

it.each([
  ['tool-only persisted shell', { ...durable, durableComplete: false }],
  ['pending history projection', { ...durable, pending: true }],
  ['saved error', { ...durable, error: 'Failure' }],
  [
    'different saved answer',
    { ...durable, parts: [tool('same-call'), { type: 'text' as const, text: 'Another answer.' }] }
  ]
])('does not consume a live answer for %s', (_, saved) => {
  expect(collapseDuplicateFinalIntoDurable([user, saved as ChatMessage, live], 2, options)).toBeNull()
})

it('never hides a terminal failure or text-only identical reply', () => {
  expect(collapseDuplicateFinalIntoDurable([durable, live], 1, options)).toBeNull()
  expect(collapseDuplicateFinalIntoDurable([user, durable, live], 2, { ...options, hasFailure: true })).toBeNull()
  const textOnly = { ...live, parts: [{ type: 'text' as const, text: 'Done.' }] }
  expect(collapseDuplicateFinalIntoDurable([user, durable, textOnly], 2, options)).toBeNull()
})
