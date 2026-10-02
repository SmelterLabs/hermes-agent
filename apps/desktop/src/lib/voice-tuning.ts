/**
 * User-tunable voice-loop thresholds, seeded from config.yaml by
 * store/voice-prefs (`applyVoiceTuningFromConfig`) on config load. Defaults
 * match the previously hardcoded constants in lib/voice-barge-in.
 *
 * - `voice.barge_in_playback_floor` — minimum trigger level while TTS plays, on
 *   the 0..1 byte-domain scale the monitor uses (0.14 ≈ int16 RMS 1500, the
 *   Python constant). A quiet mic — e.g. a condenser at conservative gain, where
 *   normal speech sits near −30 dBFS ≈ 0.07 — cannot reach 0.14 without shouting;
 *   lower it (0.08–0.10) or raise the mic gain.
 *
 * Lives in its own module (not lib/voice-barge-in) so hooks can import it while
 * tests mock the monitor module with a bare factory.
 */

const TRIGGER_CEILING_LEVEL = 0.37 // must match lib/voice-barge-in

export const DEFAULT_PLAYBACK_MIN_TRIGGER_LEVEL = 0.14

export const bargeInTuning = {
  playbackMinTriggerLevel: DEFAULT_PLAYBACK_MIN_TRIGGER_LEVEL
}

export function configureBargeIn(next: Partial<typeof bargeInTuning>): void {
  if (
    typeof next.playbackMinTriggerLevel === 'number' &&
    next.playbackMinTriggerLevel >= 0.02 &&
    next.playbackMinTriggerLevel <= TRIGGER_CEILING_LEVEL
  ) {
    bargeInTuning.playbackMinTriggerLevel = next.playbackMinTriggerLevel
  }
}
