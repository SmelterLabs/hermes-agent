import { afterEach, describe, expect, it } from 'vitest'

import { applyVoiceTuningFromConfig } from '@/store/voice-prefs'

import { bargeInTuning, configureBargeIn } from './voice-tuning'

const defaults = { ...bargeInTuning }
afterEach(() => configureBargeIn(defaults))

describe('Desktop voice tuning', () => {
  it('keeps the accepted renderer fallback values', () => {
    expect(defaults).toEqual({ playbackMinTriggerLevel: 0.14 })
  })

  it('honors quiet-mic thresholds', () => {
    applyVoiceTuningFromConfig({ voice: {
      barge_in_threshold_multiplier: 3,
      barge_in_playback_floor: 0.09
    } })
    expect(bargeInTuning).toEqual({ playbackMinTriggerLevel: 0.09 })
  })

  it('honors backend defaults without substituting renderer fallbacks', () => {
    applyVoiceTuningFromConfig({ voice: {
      barge_in_threshold_multiplier: 3,
      barge_in_playback_floor: 0.14
    } })
    expect(bargeInTuning).toEqual({ playbackMinTriggerLevel: 0.14 })
  })

  it('rejects nonfinite and out-of-range values', () => {
    configureBargeIn({ playbackMinTriggerLevel: Infinity })
    expect(bargeInTuning).toEqual(defaults)
  })
})
