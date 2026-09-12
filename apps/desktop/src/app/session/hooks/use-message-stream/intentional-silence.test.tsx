import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { playCompletionSound } from '@/lib/completion-sound'
import { $clarifyRequests, setClarifyRequest } from '@/store/clarify'
import { dispatchNativeNotification } from '@/store/native-notifications'
import { notify } from '@/store/notifications'
import { flashPetActivity, markPetUnread } from '@/store/pet'
import { clearAllPrompts, sessionApprovalRequest, setApprovalRequest } from '@/store/prompts'

import { renderMessageStream } from './test-harness'

vi.mock('@/lib/completion-sound', () => ({ playCompletionSound: vi.fn() }))
vi.mock(import('@/store/native-notifications'), async importOriginal => ({
  ...(await importOriginal()),
  dispatchNativeNotification: vi.fn()
}))
vi.mock(import('@/store/notifications'), async importOriginal => ({
  ...(await importOriginal()),
  notify: vi.fn()
}))
vi.mock(import('@/store/pet'), async importOriginal => ({
  ...(await importOriginal()),
  flashPetActivity: vi.fn(),
  markPetUnread: vi.fn(),
  setPetActivity: vi.fn()
}))

afterEach(() => {
  cleanup()
  clearAllPrompts('s')
})
beforeEach(() => vi.clearAllMocks())

describe('intentional silent terminal event', () => {
  it.each(['none', 'empty', 'reasoning'])('settles without a bubble, hydration or notification (%s)', pending => {
    const state = createClientSessionState()
    state.busy = true
    state.turnLive = true
    state.turnStartedAt = 1
    state.needsInput = true
    state.messages = [{ id: 'u', role: 'user', parts: [{ type: 'text', text: 'NO_REPLY' }] }]

    if (pending !== 'none') {
      state.streamId = 'empty'
      state.messages.push({
        id: 'empty', role: 'assistant', pending: true,
        parts: pending === 'reasoning' ? [{ type: 'reasoning', text: 'Internal handling' }] : []
      })
    }

    setClarifyRequest({ sessionId: 's', requestId: 'q', question: 'old', choices: null, multiSelect: false })
    setApprovalRequest({ sessionId: 's', requestId: 'a', command: 'old', description: 'old' })
    const hydrate = vi.fn(async () => undefined)
    const h = renderMessageStream('s', { states: new Map([['s', state]]), hydrateFromStoredSession: hydrate })
    act(() => h.handleEvent({ type: 'message.complete', session_id: 's', payload: { text: '', status: 'complete', silent: true, usage: { output: 7 } } }))
    expect(h.state().messages).toEqual(state.messages.filter(m => m.role === 'user'))
    expect(h.state()).toMatchObject({ busy: false, turnLive: false, turnStartedAt: null, needsInput: false })
    expect($clarifyRequests.get().s).toBeUndefined()
    expect(sessionApprovalRequest('s').get()).toBeNull()
    expect(h.state().usage?.output).toBe(7)
    expect(hydrate).not.toHaveBeenCalled()
    expect(playCompletionSound).not.toHaveBeenCalled()
    expect(dispatchNativeNotification).not.toHaveBeenCalled()
    expect(flashPetActivity).not.toHaveBeenCalled()
    expect(markPetUnread).not.toHaveBeenCalled()
  })

  it('preserves and closes real interim/tool/error evidence', () => {
    const state = createClientSessionState()
    state.busy = true
    state.messages = [
      { id: 'old', role: 'assistant', parts: [{ type: 'text', text: 'Earlier answer' }] },
      { id: 'interim', role: 'assistant', pending: true, parts: [{ type: 'text', text: 'Real progress' }] },
      { id: 'error', role: 'assistant', pending: true, error: 'Real error', parts: [] }
    ]
    const h = renderMessageStream('s', { states: new Map([['s', state]]) })
    act(() => h.handleEvent({ type: 'tool.start', session_id: 's', payload: { tool_id: 't', name: 'read_file', args: {} } }))
    act(() => h.handleEvent({ type: 'tool.complete', session_id: 's', payload: { tool_id: 't', name: 'read_file', result: 'NO_REPLY' } }))
    act(() => h.handleEvent({ type: 'message.complete', session_id: 's', payload: { text: '', status: 'complete', silent: true } }))
    expect(h.state().messages.some(m => m.parts.some(p => p.type === 'text' && p.text === 'Real progress'))).toBe(true)
    expect(h.state().messages.some(m => m.error === 'Real error')).toBe(true)
    expect(h.state().messages.some(m => m.parts.some(p => p.type === 'tool-call'))).toBe(true)
    expect(h.state().messages.every(m => !m.pending)).toBe(true)
  })

  it.each([{ warning: 'History not saved' }, { partial: true }, { status: 'error', error: 'Real error' }])('never silences contradictory failure evidence: %s', extra => {
    const h = renderMessageStream('s')
    act(() => h.handleEvent({ type: 'message.complete', session_id: 's', payload: { text: 'NO_REPLY', status: 'complete', silent: true, ...extra } }))

    if ('error' in extra) {
      expect(h.state().messages.some(message => message.error === extra.error)).toBe(true)
    } else {
      expect(h.text()).toBe('NO_REPLY')
    }

    expect(playCompletionSound).toHaveBeenCalled()
    expect(dispatchNativeNotification).toHaveBeenCalled()

    if ('warning' in extra) {
      expect(notify).toHaveBeenCalledWith({ kind: 'warning', message: extra.warning })
    }
  })
})
