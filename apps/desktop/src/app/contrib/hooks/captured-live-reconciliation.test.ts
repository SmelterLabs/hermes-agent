import { expect, it } from 'vitest'

import { reconcileDurableHistory } from '@/app/session/hooks/use-session-actions/utils'
import { type ChatMessage, chatMessageText } from '@/lib/chat-messages'

// Reduced from live-exact.json, captured from the installed renderer while
// the duplicate was visible. Tool arguments/output are inert placeholders.
const text = 'As of **10:54 PM tonight**, two of our Rakazo PRs are still open.'

const user: ChatMessage = {
  id: '1790999678.5568187-116-user',
  role: 'user',
  rowId: 517247,
  timestamp: 1790999678.5568187,
  parts: [{ type: 'text', text: "what's the latest on our open PRs?", sourceRowId: 517247 }]
}

const saved: ChatMessage = {
  id: '1790999704.658709-119-assistant',
  role: 'assistant',
  durableComplete: true,
  timestamp: 1790999690.407239,
  rowId: 517253,
  serverRowSpan: 3,
  parts: [
    {
      type: 'tool-call',
      toolCallId: 'toolu_01Fnj8z56mNndL4zBbDyJ6wf',
      toolName: 'terminal',
      args: { command: 'inert', timeout: 90 },
      result: 'inert',
      timestamp: 1790999690.407239,
      completedAt: 1790999696.2129767
    },
    { type: 'text', text, sourceRowId: 517253, timestamp: 1790999704.658709 }
  ]
}

const live: ChatMessage = {
  id: 'assistant-stream-1790999690420-40',
  role: 'assistant',
  timestamp: 1790999690.419,
  pending: false,
  completedAt: 1790999704.686,
  interim: false,
  recovered: false,
  durationS: 26,
  durableComplete: false,
  parts: [
    {
      type: 'tool-call',
      toolCallId: 'toolu_01Fnj8z56mNndL4zBbDyJ6wf',
      toolName: 'terminal',
      args: { command: 'inert', timeout: 90, context: 'date -Iseconds + 7 commands' },
      result: 'inert',
      timestamp: 1790999690.419,
      completedAt: 1790999696.321,
      toolResultMetadata: { duration_s: 5.851993560791016 }
    },
    { type: 'text', text: '\n\n' + text, timestamp: 1790999704.551, completedAt: 1790999704.686 }
  ]
}

it.each([false, true])('reconciles the captured settled live copy (already duplicated: %s)', alreadyDuplicated => {
  const result = reconcileDurableHistory([user, saved], alreadyDuplicated ? [user, saved, live] : [user, live])
  const answers = result.filter(m => m.role === 'assistant')
  expect(answers.map(m => ({ id: m.id, text: chatMessageText(m).trim() }))).toEqual([{ id: saved.id, text }])
})
