import { AssistantRuntimeProvider, ExportedMessageRepository, type ThreadMessage } from '@assistant-ui/react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { type ReactNode, useMemo } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { useIncrementalExternalStoreRuntime } from '@/lib/incremental-external-store-runtime'

import { assistantMessage, stubThreadEnvironment, userMessage } from '../test-utils'

import { Thread } from '.'

function Runtime({ children, messages }: { children: ReactNode; messages: ThreadMessage[] }) {
  const messageRepository = useMemo(() => ExportedMessageRepository.fromArray(messages), [messages])

  const runtime = useIncrementalExternalStoreRuntime({
    messageRepository,
    isRunning: messages.at(-1)?.status?.type === 'running',
    onNew: async () => {}
  })

  return <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
}

const answer = (id: string, text = 'The final answer'): ThreadMessage => ({
  ...assistantMessage(), id, content: [{ type: 'text', text }]
})

beforeEach(stubThreadEnvironment)
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('replaces a long transcript without old index scopes reading the replacement reply', async () => {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

  const messages = Array.from({ length: 15 }, (_, i) =>
    i % 2 ? answer(`answer-${i}`) : userMessage(`prompt-${i}`, `Question ${i}`)
  )

  const { container, rerender } = render(<Runtime messages={messages}><Thread /></Runtime>)

  await act(async () => {
    rerender(<Runtime messages={[answer('new-final')]}><Thread /></Runtime>)
  })

  await waitFor(() => expect(container.querySelectorAll('[data-role="assistant"]')).toHaveLength(1))
  expect(container.querySelector('[data-role="assistant"]')?.getAttribute('data-message-id')).toBe('new-final')
  expect(warn.mock.calls.filter(([value]) => String(value).includes('Clamped stale index'))).toHaveLength(0)
})

it('keeps one final through stream replacement, session switch, return and history prepend', async () => {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
  const prompt = userMessage('prompt', 'Check the pull requests.')
  const final = answer('durable-final', '12 of our 13 pull requests are merged.')
  const stream = { ...answer('assistant-stream-1', '12 of our 13'), status: { type: 'running' } } as ThreadMessage
  const { container, rerender } = render(<Runtime messages={[prompt, stream]}><Thread /></Runtime>)

  const steps = [
    [prompt, final],
    [userMessage('other-prompt', 'Other session'), answer('other-answer', 'Other answer')],
    [prompt, final],
    [userMessage('earlier-prompt', 'Earlier question'), answer('earlier-answer', 'Earlier answer'), prompt, final]
  ]

  for (const messages of steps) {
    await act(async () => {
      rerender(<Runtime messages={messages}><Thread /></Runtime>)
    })

    const expected = messages.filter(message => message.role === 'assistant').map(message => message.id)

    await waitFor(() => expect(
      [...container.querySelectorAll('[data-role="assistant"]')].map(node => node.getAttribute('data-message-id'))
    ).toEqual(expected))
  }

  expect(container.querySelectorAll('[data-message-id="durable-final"]')).toHaveLength(1)
  expect(warn.mock.calls.filter(([value]) => String(value).includes('Clamped stale index'))).toHaveLength(0)
})

it('preserves separate identical answers and message identities after reordering', async () => {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
  const first = [userMessage('question-a', 'Ask again'), answer('answer-a', 'Same answer')]
  const second = [userMessage('question-b', 'Ask again'), answer('answer-b', 'Same answer')]
  const { container, rerender } = render(<Runtime messages={[...first, ...second]}><Thread /></Runtime>)

  await act(async () => {
    rerender(<Runtime messages={[...second, ...first]}><Thread /></Runtime>)
  })
  await waitFor(() => expect(
    [...container.querySelectorAll('[data-role="assistant"]')].map(node => node.getAttribute('data-message-id'))
  ).toEqual(['answer-b', 'answer-a']))
  expect(warn.mock.calls.filter(([value]) => String(value).includes('Clamped stale index'))).toHaveLength(0)
})
