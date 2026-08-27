/** Teaching the Harness's Session store about Orbit's own events.
 *
 * A module augmentation names a package, so it can only live where that
 * package is a dependency. The shapes it refers to are host-agnostic and now
 * live in the shared core; this file is the one line of the pair that is about
 * DeepSeek-Harness, and it stays here for that reason alone.
 */

import type {
  OrbitRunStarted, OrbitRunCheckpoint, OrbitRunEnded,
} from '@orbit-runtime/integration-core'

declare module '@deepseek-ai/dsh-session/types' {
  interface SessionEventMap {
    'orbit/run-started': Omit<OrbitRunStarted, 'type'>
    'orbit/run-checkpoint': Omit<OrbitRunCheckpoint, 'type'>
    'orbit/run-ended': Omit<OrbitRunEnded, 'type'>
  }
}
