import type { GatewayEvent } from '@hermes/shared'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { useMessageStream } from '@/app/session/hooks/use-message-stream'
import { useSessionStateCache } from '@/app/session/hooks/use-session-state-cache'
import { stubThreadEnvironment, ThreadRuntime } from '@/components/assistant-ui/test-utils'
import { Thread } from '@/components/assistant-ui/thread'
import { getLatestSessionMessages } from '@/hermes'
import { chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { toRuntimeMessage } from '@/lib/chat-runtime'
import { resetLiveSync } from '@/store/live-sync'
import {
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setMessages,
  setSelectedStoredSessionId
} from '@/store/session'
import { $sessionStates, clearAllSessionStates } from '@/store/session-states'
import { clearSessionTodos } from '@/store/todos'
import type { SessionMessage } from '@/types/hermes'

import { reconcileActiveTranscript, reconcileFetchedTranscript } from './use-background-sync'

vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn()
}))

const RUNTIME = 'latest-tool-turn-runtime'
const STORED = 'latest-tool-turn-stored'
const USER_TEXT = "what's the latest on our open PRs?"
const TOOL_ID = 'toolu_01Fnj8z56mNndL4zBbDyJ6wf'

const FINAL = `As of **10:54 PM tonight**, two of our Rakazo PRs are still open. Neither is waiting on us; both are waiting for the maintainer to merge them.

| PR | What it does | Status |
|---|---|---|
| [#1142](https://github.com/elie222/rakazo/pull/1142) | Agent-to-agent replies no longer show you an unread badge (an on/off setting, off by default) | Mergeable, all review threads resolved. The tests, builds and browser tests passed. Vercel shows red because the preview needs the maintainer's authorization; it isn't a problem in our code. |
| [#1115](https://github.com/elie222/rakazo/pull/1115) | Lets you set keep-awake per computer | Mergeable, every check passed, no open threads. Nothing has changed on it since early this morning. |

Nothing new has merged since this afternoon's check, so our other 12 PRs are still merged. Both of these features already run on your setup no matter when the maintainer merges them.`

const noop = async () => undefined

// Exact active SessionDB row addresses, provider tool id, prompt and final text
// from session 20261001_153851_5c1231. The DB contains one active, un-compacted
// copy of each row. This is a passing control replay, not the observed failure:
// the actual Desktop ChatMessage snapshot and gateway-frame trace were not captured.
// Tool output/arguments are inert placeholders: this replay never executes the
// captured command and reconciliation keys on its durable id.
const storedRows: SessionMessage[] = [
  { id: 517247, role: 'user', content: USER_TEXT, timestamp: 1790999678.5568187 },
  {
    id: 517250,
    role: 'assistant',
    content: '',
    timestamp: 1790999690.407239,
    tool_calls: [
      {
        id: TOOL_ID,
        call_id: TOOL_ID,
        response_item_id: `fc_${TOOL_ID}`,
        type: 'function',
        function: { name: 'terminal', arguments: JSON.stringify({ command: 'date -Iseconds; gh pr list …' }) }
      }
    ]
  },
  {
    id: 517252,
    role: 'tool',
    content: JSON.stringify({ output: 'stored terminal output', exit_code: 0, error: null }),
    timestamp: 1790999696.2129767,
    tool_call_id: TOOL_ID,
    tool_name: 'terminal'
  },
  { id: 517253, role: 'assistant', content: FINAL, timestamp: 1790999704.658709 }
]

let cache: ReturnType<typeof useSessionStateCache>
let stream: ReturnType<typeof useMessageStream>
let refresh: () => Promise<void>

function Harness() {
  const busyRef = useRef(false)
  const queryClient = useRef(new QueryClient()).current
  const requestSequenceRef = useRef(0)
  const signatureRef = useRef(new Map<string, string>())
  cache = useSessionStateCache({
    activeSessionId: RUNTIME,
    selectedStoredSessionId: STORED,
    busyRef,
    setBusy,
    setAwaitingResponse,
    setMessages
  })
  stream = useMessageStream({
    ...cache,
    queryClient,
    hydrateFromStoredSession: noop,
    refreshHermesConfig: noop,
    refreshSessions: noop
  })
  refresh = () =>
    reconcileActiveTranscript({
      activeSessionIdRef: cache.activeSessionIdRef,
      selectedStoredSessionIdRef: cache.selectedStoredSessionIdRef,
      updateSessionState: cache.updateSessionState,
      busyRef,
      requestSequenceRef,
      signatureRef,
      resolveSession: () => ({ profile: 'default' })
    })

  return null
}

function send(type: GatewayEvent['type'], payload: GatewayEvent['payload'] = {}) {
  act(() => stream.handleGatewayEvent({ session_id: RUNTIME, type, payload }))
}

beforeEach(() => {
  vi.useFakeTimers()
  stubThreadEnvironment()
  vi.setSystemTime(1790999690407)
  vi.mocked(getLatestSessionMessages).mockReset()
  setActiveSessionId(RUNTIME)
  setSelectedStoredSessionId(STORED)
})

afterEach(() => {
  cleanup()
  clearSessionTodos(RUNTIME)
  clearAllSessionStates()
  resetLiveSync()
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  setBusy(false)
  setAwaitingResponse(false)
  setMessages([])
  localStorage.clear()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('does not append a replayed live tool turn beside its already loaded durable answer', async () => {
  render(<Harness />)
  act(() => cache.updateSessionState(RUNTIME, state => ({ ...state, messages: toChatMessages(storedRows) }), STORED))

  // A history page can already contain this completed turn when the attached
  // stream delivers its queued frames. The unique tool identity proves that
  // this is the loaded occurrence, not a genuinely new prompt/answer.
  send('message.start')
  send('tool.start', {
    name: 'terminal',
    tool_id: TOOL_ID,
    args: { command: 'date -Iseconds; gh pr list …', context: 'date -Iseconds + 7 commands' }
  })
  send('tool.complete', {
    name: 'terminal',
    tool_id: TOOL_ID,
    result: { output: 'stored terminal output', exit_code: 0, error: null },
    duration_s: 5.851993560791016
  })
  vi.setSystemTime(1790999704686)
  send('message.delta', { text: '\n\n' + FINAL })
  send('message.complete', { text: FINAL, duration_s: 26 })

  const messages = $sessionStates.get()[RUNTIME].messages
  const assistants = messages.filter(m => m.role === 'assistant')
  expect(assistants.map(m => ({ rowId: m.rowId, text: chatMessageText(m).trim() }))).toEqual([
    { rowId: 517253, text: FINAL }
  ])
  expect(assistants.flatMap(m => m.parts.filter(p => p.type === 'tool-call').map(p => p.toolCallId))).toEqual([TOOL_ID])
})

function assistantSummary(messages: ReturnType<typeof toChatMessages>) {
  return messages
    .filter(message => message.role === 'assistant')
    .map(message => ({
      id: message.id,
      rowId: message.rowId,
      pending: message.pending,
      interim: message.interim,
      text: chatMessageText(message),
      toolIds: message.parts.flatMap(part => (part.type === 'tool-call' ? [part.toolCallId] : []))
    }))
}

it.each([true, false])(
  'keeps the newest un-compacted tool turn singular after history reconciliation (receipt: %s)',
  async hasReceipt => {
    expect(FINAL).toHaveLength(916)
    render(<Harness />)
    act(() =>
      cache.updateSessionState(
        RUNTIME,
        state => ({ ...state, messages: toChatMessages(storedRows.slice(0, 1)) }),
        STORED
      )
    )

    // No live tool result appears in the screenshot's first card. Omitting
    // tool.complete models that clue, but the actual event trace is unavailable.
    // message.complete still settles final text; the stored snapshot has the result.
    send('message.start')
    send('tool.start', { name: 'terminal', tool_id: TOOL_ID, args: { command: 'date -Iseconds; gh pr list …' } })
    vi.setSystemTime(1790999704658)

    for (const chunk of FINAL.match(/[\s\S]{1,12}/g) ?? []) {
      send('message.delta', { text: chunk })
    }

    send('message.complete', {
      text: FINAL,
      ...(hasReceipt
        ? {
            persisted_turn: {
              row_ids: [517247, 517250, 517252, 517253],
              user_row_id: 517247,
              final_assistant_row_id: 517253,
              complete: true
            }
          }
        : {})
    })

    const beforeHistory = $sessionStates.get()[RUNTIME].messages
    expect(assistantSummary(beforeHistory)).toHaveLength(1)
    expect(assistantSummary(beforeHistory)[0]).toMatchObject({ text: FINAL, toolIds: [TOOL_ID] })

    vi.mocked(getLatestSessionMessages).mockResolvedValue({ session_id: STORED, messages: storedRows })
    await act(async () => {
      await refresh()
    })

    const afterHistory = $sessionStates.get()[RUNTIME].messages
    const summary = assistantSummary(afterHistory)
    const answerCopies = summary.filter(message => message.text === FINAL).length
    const toolCopies = summary.flatMap(message => message.toolIds).filter(id => id === TOOL_ID).length

    expect({ summary, answerCopies, toolCopies }).toEqual({
      summary: [expect.objectContaining({ rowId: 517253, text: FINAL, toolIds: [TOOL_ID] })],
      answerCopies: 1,
      toolCopies: 1
    })
  }
)

it('renders the latest turn once in Thread while history replaces the live projection', async () => {
  render(<Harness />)
  act(() =>
    cache.updateSessionState(RUNTIME, state => ({ ...state, messages: toChatMessages(storedRows.slice(0, 1)) }), STORED)
  )

  send('message.start')
  send('tool.start', { name: 'terminal', tool_id: TOOL_ID, args: { command: 'date -Iseconds; gh pr list …' } })
  vi.setSystemTime(1790999704658)

  for (const chunk of FINAL.match(/[\s\S]{1,12}/g) ?? []) {
    send('message.delta', { text: chunk })
  }

  send('message.complete', {
    text: FINAL,
    persisted_turn: {
      row_ids: [517247, 517250, 517252, 517253],
      user_row_id: 517247,
      final_assistant_row_id: 517253,
      complete: true
    }
  })

  const beforeHistory = $sessionStates.get()[RUNTIME].messages
  vi.useRealTimers()
  stubThreadEnvironment()

  const view = render(
    <ThreadRuntime messages={beforeHistory.map(toRuntimeMessage)}>
      <Thread />
    </ThreadRuntime>
  )

  await waitFor(() => expect(view.container.querySelectorAll('[data-role="assistant"]')).toHaveLength(1))
  expect(view.container.querySelectorAll('[data-slot="aui_msg-actions"]')).toHaveLength(1)
  expect(view.container.textContent).toContain('As of 10:54 PM tonight')

  vi.mocked(getLatestSessionMessages).mockResolvedValue({ session_id: STORED, messages: storedRows })
  await act(async () => {
    await refresh()
  })

  const afterHistory = $sessionStates.get()[RUNTIME].messages
  view.rerender(
    <ThreadRuntime messages={afterHistory.map(toRuntimeMessage)}>
      <Thread />
    </ThreadRuntime>
  )

  await waitFor(() => expect(view.container.querySelectorAll('[data-role="assistant"]')).toHaveLength(1))
  expect(view.container.querySelectorAll('[data-slot="aui_response-group"]')).toHaveLength(1)
  expect(view.container.querySelectorAll('[data-slot="aui_msg-actions"]')).toHaveLength(1)
  expect((view.container.textContent?.match(/As of 10:54 PM tonight/g) ?? []).length).toBe(1)
})

it('reconciles the committed DB fold against the still-pending exact live stream', async () => {
  render(<Harness />)
  act(() =>
    cache.updateSessionState(RUNTIME, state => ({ ...state, messages: toChatMessages(storedRows.slice(0, 1)) }), STORED)
  )

  send('message.start')
  send('tool.start', { name: 'terminal', tool_id: TOOL_ID, args: { command: 'date -Iseconds; gh pr list …' } })
  vi.setSystemTime(1790999704658)

  for (const chunk of FINAL.match(/[\s\S]{1,12}/g) ?? []) {
    send('message.delta', { text: chunk })
  }

  await act(async () => {
    await vi.advanceTimersByTimeAsync(100)
  })

  const pending = $sessionStates.get()[RUNTIME].messages
  expect(assistantSummary(pending)).toHaveLength(1)
  expect(assistantSummary(pending)[0]).toMatchObject({ pending: true, text: FINAL, toolIds: [TOOL_ID] })

  const reconciled = reconcileFetchedTranscript(toChatMessages(storedRows), pending)
  const summary = assistantSummary(reconciled)

  expect({ summary, answerCopies: summary.filter(message => message.text === FINAL).length }).toEqual({
    summary: [expect.objectContaining({ rowId: 517253, text: FINAL, toolIds: [TOOL_ID] })],
    answerCopies: 1
  })
})
