'use client'

import { type ToolCallMessagePartProps, useAuiState } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import { useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { ToolFallback } from '@/components/assistant-ui/tool/fallback'
import { $settledClarifyResults, sessionClarifyRequest } from '@/store/clarify'

import { selectMessageRunning } from '../tool/fallback-model'

import { readClarifyArgs } from './parse'
import { ClarifyToolPending } from './pending'
import { ClarifyToolSettled } from './settled'
import { useUndeliveredClarify } from './use-undelivered'

export const ClarifyTool = (props: ToolCallMessagePartProps) => {
  // Answered → settled Q&A (ToolFallback collapsed the answer away).
  if (props.result !== undefined) {
    return <ClarifyToolSettled {...props} />
  }

  return <ClarifyToolLive {...props} />
}

// The request each tool row asked, remembered past its clearing and past a
// remount (a session switch, a stopped turn): a skip or a typed answer settles
// it in the store before (or without) `tool.complete`. Keyed by session,
// message and call: some providers name every call "call_0", and a bare call id
// would show a later card the answer of an earlier one.
const requestIdByToolCall = new Map<string, string>()

function ClarifyToolLive(props: ToolCallMessagePartProps) {
  // The tool row is in whichever session's transcript rendered it — read THAT
  // session's clarify (primary or tile), not the globally-active one.
  const sessionId = useStore(useSessionView().$runtimeId)
  const $request = useMemo(() => sessionClarifyRequest(sessionId), [sessionId])
  const request = useStore($request)
  const fromArgs = useMemo(() => readClarifyArgs(props.args), [props.args])
  // Old transcript rows must never adopt a newer question from the same session.
  const matchingRequest =
    request &&
    request.questions.length === (fromArgs.questions?.length ?? 0) &&
    request.questions.every((question, index) => question.question.trim() === fromArgs.questions?.[index]?.question.trim())
      ? request
      : null

  const messageRunning = useAuiState(selectMessageRunning)
  const messageId = useAuiState(s => s.message.id)
  const rowKey = `${sessionId ?? ''}:${messageId}:${props.toolCallId}`
  // Answering clears the request a beat before `tool.complete` swaps in the
  // settled card. Latch submit so that gap doesn't demote; Stop also clears
  // the request and must still collapse an unanswered card.
  const [answered, setAnswered] = useState(false)
  const settledResults = useStore($settledClarifyResults)
  const requestId = requestIdByToolCall.get(rowKey)
  const settledResult = requestId && request?.requestId !== requestId ? settledResults[requestId] : undefined

  if (matchingRequest && !settledResult && matchingRequest.requestId !== requestId) {
    requestIdByToolCall.set(rowKey, matchingRequest.requestId)
  }

  const undelivered = useUndeliveredClarify(sessionId, messageRunning && !matchingRequest && !answered && !settledResult)

  if (settledResult) {
    return <ClarifyToolSettled {...props} result={settledResult} />
  }

  // Stopped mid-prompt with no result — don't leave a dead interactive panel.
  // `session.info` reports running=false while clarify is blocking, so the
  // running flag alone would remount the question as a tool row. Keep the
  // card while a request is open or this instance already submitted.
  if (!messageRunning && !matchingRequest && !answered) {
    return <ToolFallback {...props} />
  }

  return (
    <ClarifyToolPending
      fromArgs={fromArgs}
      key={matchingRequest ? `${matchingRequest.sessionId}\0${matchingRequest.requestId}` : 'preview'}
      onAnswered={() => setAnswered(true)}
      request={matchingRequest}
      undelivered={undelivered}
    />
  )
}
