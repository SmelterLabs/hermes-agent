import { expect, it } from 'vitest'

import { type ChatMessage, type ChatMessagePart, chatMessageText } from '@/lib/chat-messages'

import { reconcileFetchedTranscript } from './use-background-sync'

const text = (value: string, sourceRowId?: number): ChatMessagePart => ({
  type: 'text',
  text: value,
  ...(sourceRowId === undefined ? {} : { sourceRowId })
})

const tool = (toolCallId: string, withElapsedDuration: boolean): ChatMessagePart => ({
  type: 'tool-call',
  toolCallId,
  toolName: 'terminal',
  result: withElapsedDuration ? 'ok (2.1s)' : 'ok'
})

const user = (id: string, value: string, rowId: number): ChatMessage => ({
  id,
  role: 'user',
  parts: [text(value)],
  rowId
})

const liveAssistant = (id: string, parts: ChatMessagePart[], rowId: number): ChatMessage => ({
  id,
  role: 'assistant',
  parts,
  rowId,
  pending: false,
  durableComplete: false,
  persistedTurn: {
    row_ids: [454241, rowId],
    user_row_id: 454241,
    final_assistant_row_id: rowId,
    complete: false
  }
})

it('does not duplicate a whole live turn when compaction reissues its durable rows', () => {
  // The live side has one settled stream bubble: commentary/tool activity is
  // visible immediately, but tool rows do not yet carry hydrated elapsed data.
  // Its final row id belongs to an older physical copy of one logical turn.
  const previous = [
    user('old-prompt', 'Ok. Let’s do it.', 454241),
    liveAssistant(
      'assistant-stream-live',
      [
        text('I’ll build an outbound-only connection.'),
        tool('call-1', false),
        text('The dashboard generator rejected its own styling.'),
        tool('call-2', false),
        text('The integration inspection is running.')
      ],
      471402
    )
  ]

  // SessionDB's display read returns this logical turn once, choosing the
  // active compaction copy. `display_identity` and `message_uid` are the same
  // across the four physical copies, but the renderer currently receives only
  // the chosen row id (474524) in ChatMessage.
  const authoritative = [
    user('same-prompt', 'Ok. Let’s do it.', 454241),
    {
      id: 'assistant-history',
      role: 'assistant' as const,
      parts: [
        text('I’ll build an outbound-only connection.', 474524),
        tool('call-1', true),
        text('The dashboard generator rejected its own styling.', 474524),
        tool('call-2', true),
        text('The integration inspection is running.', 474524)
      ],
      rowId: 474524,
      pending: false
    }
  ] satisfies ChatMessage[]

  const reconciled = reconcileFetchedTranscript(authoritative, previous)
  const assistantTexts = reconciled.filter(message => message.role === 'assistant').map(chatMessageText)

  expect(assistantTexts).toEqual([
    'I’ll build an outbound-only connection.The dashboard generator rejected its own styling.The integration inspection is running.'
  ])
  expect(reconciled).toEqual(authoritative)
  expect(reconcileFetchedTranscript(authoritative, reconciled)).toEqual(authoritative)
})

it.each(['different tools', 'missing tool', 'text only', 'still streaming', 'different answer'])(
  'preserves a row-id-conflicting live reply with %s',
  variant => {
    const prompt = user('prompt', 'Run the check.', 454241)
    const local = liveAssistant('assistant-stream-live', [tool('call-1', false), text('Done.')], 471402)

    const stored: ChatMessage = {
      id: 'assistant-history',
      role: 'assistant',
      rowId: 474524,
      parts: [tool('call-1', true), text('Done.', 474524)]
    }

    if (variant === 'different tools') {
      stored.parts = [tool('other-call', true), text('Done.', 474524)]
    }

    if (variant === 'missing tool') {
      local.parts.unshift(tool('unpersisted-call', false))
    }

    if (variant === 'text only') {
      local.parts = [text('Done.')]
      stored.parts = [text('Done.', 474524)]
    }

    if (variant === 'still streaming') {
      local.pending = true
    }

    if (variant === 'different answer') {
      local.parts = [tool('call-1', false), text('New answer.')]
    }

    const reconciled = reconcileFetchedTranscript([prompt, stored], [prompt, local])

    expect(reconciled).toEqual([prompt, stored, local])
  }
)

it('does not consume an identical answer to a new prompt using an earlier turn’s tool fold', () => {
  const earlierPrompt = user('earlier-prompt', 'Run the check.', 454241)

  const earlierReply: ChatMessage = {
    id: 'earlier-reply',
    role: 'assistant',
    rowId: 474524,
    parts: [tool('call-1', true), text('Done.', 474524)]
  }

  const newPrompt = user('user-new', 'Run the check again.', 474525)
  const local = liveAssistant('assistant-stream-new', [tool('call-2', false), text('Done.')], 474526)
  const authoritative = [earlierPrompt, earlierReply, newPrompt]

  expect(reconcileFetchedTranscript(authoritative, [...authoritative, local])).toEqual([...authoritative, local])
})
