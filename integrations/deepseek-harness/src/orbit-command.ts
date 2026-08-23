export const ORBIT_COMMAND = 'orbit'
export const ORBIT_SELECTION_PENDING_TEXT = 'Choose an Orbit Workflow to continue.'

export function orbitGoal(rawInput: string): string {
  return rawInput.trim()
}
