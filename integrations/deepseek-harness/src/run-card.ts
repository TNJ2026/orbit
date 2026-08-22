import type { OrbitSessionEvent } from './types.js'

export interface OrbitRunCard { runId: string; sourcePosition: number; goal?: string; workflowId?: string; workflowVersion?: number; revision: number; status: string; artifactCount: number; updatedAt: string; terminal: boolean }
export type OrbitRunCardState = Record<string, OrbitRunCard>

export function reduceOrbitRunCards(state: OrbitRunCardState, event: OrbitSessionEvent): OrbitRunCardState {
  const previous = state[event.runId]
  if (previous !== undefined && previous.sourcePosition >= event.sourcePosition) return state
  const next: OrbitRunCard = event.type === 'orbit/run-started'
    ? { runId: event.runId, sourcePosition: event.sourcePosition, goal: event.goal, workflowId: event.workflowId, workflowVersion: event.workflowVersion, revision: event.revision, status: event.status, artifactCount: 0, updatedAt: event.createdAt, terminal: false }
    : { runId: event.runId, sourcePosition: event.sourcePosition, goal: previous?.goal, workflowId: previous?.workflowId, workflowVersion: previous?.workflowVersion, revision: event.revision, status: event.status, artifactCount: event.artifactCount, updatedAt: event.updatedAt, terminal: event.type === 'orbit/run-ended' }
  return { ...state, [event.runId]: next }
}
